// Benchmark harness: replays the same synthetic order stream through each book
// implementation and reports throughput and latency percentiles.
//
// Two separate kinds of pass, because measuring one disturbs the other:
//
//   Throughput pass  One timestamp before the measured stream and one after.
//                    Nothing else is added to the loop, so ops/second is as
//                    close to undisturbed as we can get.
//   Latency pass     A timestamp before and after EVERY operation. Each sample
//                    includes the cost of reading the clock, and the clock has
//                    a coarse tick on this hardware (both are measured and
//                    printed), but it is the only way to see the distribution
//                    and its tail rather than just the average.
//
// See DESIGN_NOTES #18 for why this is hand-written rather than Google Benchmark.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <string>
#include <vector>

#ifdef __APPLE__
#include <pthread.h>
#include <sys/sysctl.h>
#endif

#include "lob/order_book.hpp"
#include "order_stream.hpp"

namespace {

using lob::bench::Op;
using lob::bench::OpType;
using lob::bench::Scenario;
using lob::bench::Stream;

// steady_clock is monotonic: it never jumps backwards when the system time is
// adjusted, which system_clock can. Always use it for measuring durations.
using Clock = std::chrono::steady_clock;

std::int64_t ns_between(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
}

// A summary of everything a run did. Every implementation must produce the
// same one for the same stream; if not, we are not comparing like with like,
// and the harness aborts instead of printing numbers.
struct Checksum {
    std::uint64_t trades = 0;
    std::int64_t filled_qty = 0;
    std::uint64_t cancels_ok = 0;
    std::uint64_t final_orders = 0;
    bool operator==(const Checksum&) const = default;
};

// `inline` + template: the compiler sees the whole call chain for each Book
// type and can inline the book's code into the replay loop.
template <typename Book>
inline void apply(Book& book, const Op& op, std::vector<lob::Trade>& trades, Checksum& sum) {
    trades.clear();   // keeps capacity: after warm-up this never allocates
    switch (op.type) {
        case OpType::Limit:
            sum.filled_qty += book.add_limit(op.side, op.price, op.qty, trades).filled;
            break;
        case OpType::Market:
            sum.filled_qty += book.add_market(op.side, op.qty, trades).filled;
            break;
        case OpType::Cancel:
            sum.cancels_ok += book.cancel(op.id) ? 1u : 0u;
            break;
    }
    // Using the results stops the optimiser from deleting the work as dead code.
    sum.trades += trades.size();
}

struct RunResult {
    double seconds = 0;   // wall time of the measured part
    Checksum sum;
};

// Replays the stream through a fresh book. If `samples` is non-null this is a
// latency pass and it receives one duration (ns) per measured operation.
template <typename Book>
RunResult run(const Stream& stream, std::vector<std::uint32_t>* samples) {
    Book book(stream.book);
    std::vector<lob::Trade> trades;
    trades.reserve(4096);

    Checksum warmup_sum;
    for (std::size_t i = 0; i < stream.warmup_ops; ++i) {
        apply(book, stream.ops[i], trades, warmup_sum);
    }

    RunResult result;
    const std::size_t begin = stream.warmup_ops;
    const std::size_t end = stream.ops.size();

    if (samples == nullptr) {
        const auto t0 = Clock::now();
        for (std::size_t i = begin; i < end; ++i) {
            apply(book, stream.ops[i], trades, result.sum);
        }
        const auto t1 = Clock::now();
        result.seconds = static_cast<double>(ns_between(t0, t1)) * 1e-9;
    } else {
        samples->assign(end - begin, 0);   // allocate before timing, not during
        const auto start = Clock::now();
        for (std::size_t i = begin; i < end; ++i) {
            const auto t0 = Clock::now();
            apply(book, stream.ops[i], trades, result.sum);
            const auto t1 = Clock::now();
            (*samples)[i - begin] = static_cast<std::uint32_t>(
                std::min<std::int64_t>(ns_between(t0, t1), UINT32_MAX));
        }
        result.seconds = static_cast<double>(ns_between(start, Clock::now())) * 1e-9;
    }
    result.sum.final_orders = book.order_count();
    return result;
}

// One row per implementation. std::function type-erases the template so the
// four differently-typed run<Book> functions can sit in one vector.
struct Variant {
    std::string name;
    std::string levels;
    std::string queue;
    std::function<RunResult(const Stream&, std::vector<std::uint32_t>*)> run;
};

template <typename Book>
Variant make_variant(std::string name, std::string levels, std::string queue) {
    return Variant{std::move(name), std::move(levels), std::move(queue),
                   [](const Stream& s, std::vector<std::uint32_t>* out) { return run<Book>(s, out); }};
}

// Nearest-rank percentile of an ascending-sorted sample: the smallest value
// such that at least p% of samples are <= it. No interpolation, so every
// reported number is a latency that was actually observed.
std::uint32_t percentile(const std::vector<std::uint32_t>& sorted, double p) {
    if (sorted.empty()) {
        return 0;
    }
    const auto rank = static_cast<std::size_t>(std::ceil(p / 100.0 * static_cast<double>(sorted.size())));
    return sorted[std::max<std::size_t>(rank, 1) - 1];
}

struct Percentiles {
    std::uint32_t p50, p90, p99, p999, max;
    std::size_t count;
};

Percentiles summarise(std::vector<std::uint32_t>& samples) {
    std::sort(samples.begin(), samples.end());
    return Percentiles{percentile(samples, 50),  percentile(samples, 90),
                       percentile(samples, 99),  percentile(samples, 99.9),
                       samples.empty() ? 0 : samples.back(), samples.size()};
}

double median(std::vector<double> v) {   // by value: we sort a private copy
    std::sort(v.begin(), v.end());
    return v[v.size() / 2];
}

// --- Measuring the measuring instrument --------------------------------------

struct ClockInfo {
    std::int64_t tick_ns;        // smallest non-zero step the clock ever reports
    double call_cost_ns;         // average cost of one Clock::now()
};

ClockInfo measure_clock() {
    constexpr int kCalls = 2'000'000;
    std::int64_t smallest = INT64_MAX;
    const auto begin = Clock::now();
    auto prev = begin;
    for (int i = 0; i < kCalls; ++i) {
        const auto now = Clock::now();
        const std::int64_t d = ns_between(prev, now);
        if (d > 0 && d < smallest) {
            smallest = d;
        }
        prev = now;
    }
    const double total = static_cast<double>(ns_between(begin, prev));
    return ClockInfo{smallest, total / kCalls};
}

std::string cpu_name() {
#ifdef __APPLE__
    char buffer[128] = {};
    std::size_t size = sizeof(buffer);
    if (sysctlbyname("machdep.cpu.brand_string", buffer, &size, nullptr, 0) == 0) {
        return buffer;
    }
#endif
    return "unknown";
}

void prefer_performance_cores() {
#ifdef __APPLE__
    // Apple Silicon has fast "performance" and slow "efficiency" cores, and
    // macOS offers no way to pin a thread to one. The strongest hint available
    // is the highest quality-of-service class, which the scheduler runs on
    // performance cores when it can.
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
#endif
}

struct Options {
    std::size_t ops = 2'000'000;
    int repeats = 7;
    std::string csv;
    std::string only;   // run just this scenario, if set
};

Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        const auto value = [&]() -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "missing value for %s\n", arg.c_str());
                std::exit(2);
            }
            return argv[++i];
        };
        if (arg == "--ops") {
            o.ops = std::stoull(value());
        } else if (arg == "--repeats") {
            o.repeats = std::stoi(value());
        } else if (arg == "--csv") {
            o.csv = value();
        } else if (arg == "--scenario") {
            o.only = value();
        } else {
            std::fprintf(stderr,
                         "usage: lob_bench [--ops N] [--repeats R] [--csv FILE] [--scenario NAME]\n");
            std::exit(2);
        }
    }
    return o;
}

}  // namespace

int main(int argc, char** argv) {
    const Options options = parse(argc, argv);
    prefer_performance_cores();

#ifdef NDEBUG
    const char* build = "Release";
#else
    const char* build = "Debug";
    std::fprintf(stderr, "WARNING: this is not a Release build. The numbers below are meaningless.\n");
#endif

    const ClockInfo clock = measure_clock();
    std::printf("cpu            : %s\n", cpu_name().c_str());
    std::printf("compiler       : %s\n", __VERSION__);
    std::printf("build          : %s\n", build);
    std::printf("clock tick     : %lld ns (latency samples are multiples of this)\n",
                static_cast<long long>(clock.tick_ns));
    std::printf("clock call cost: %.1f ns (two calls are inside every latency sample)\n",
                clock.call_cost_ns);
    std::printf("measured ops   : %zu per scenario, throughput = median of %d runs\n\n", options.ops,
                options.repeats);

    const std::vector<Variant> variants = {
        make_variant<lob::MapListBook>("map+list (baseline)", "map", "list"),
        make_variant<lob::MapPoolBook>("map+pool", "map", "pool"),
        make_variant<lob::FlatListBook>("flat+list", "flat", "list"),
        make_variant<lob::FlatPoolBook>("flat+pool", "flat", "pool"),
    };

    // Three shapes of book, chosen to show where each data structure helps and
    // where it does not. See DESIGN_NOTES #19.
    std::vector<Scenario> scenarios = {
        {.name = "dense", .target_depth = 10'000, .mean_offset_ticks = 10},
        {.name = "deep", .target_depth = 100'000, .mean_offset_ticks = 50},
        {.name = "sparse", .target_depth = 2'000, .mean_offset_ticks = 1'000},
    };

    std::ofstream csv;
    if (!options.csv.empty()) {
        csv.open(options.csv);
        csv << "scenario,levels,queue,build,ops,repeats,clock_tick_ns,"
               "throughput_mops_median,throughput_mops_min,throughput_mops_max,mean_ns,"
               "p50_ns,p90_ns,p99_ns,p999_ns,max_ns,"
               "limit_p50_ns,limit_p99_ns,limit_p999_ns,"
               "cancel_p50_ns,cancel_p99_ns,cancel_p999_ns,"
               "market_p50_ns,market_p99_ns,market_p999_ns\n";
    }

    for (Scenario& scenario : scenarios) {
        if (!options.only.empty() && options.only != scenario.name) {
            continue;
        }
        scenario.measured_ops = options.ops;
        const Stream stream = lob::bench::generate(scenario);
        const std::size_t n = stream.ops.size() - stream.warmup_ops;

        std::printf("=== scenario: %s  (target depth %zu orders, mean offset %.0f ticks)\n",
                    scenario.name.c_str(), scenario.target_depth, scenario.mean_offset_ticks);
        std::printf("    mix: %.1f%% limit, %.1f%% cancel, %.1f%% market\n",
                    100.0 * static_cast<double>(stream.limits) / static_cast<double>(n),
                    100.0 * static_cast<double>(stream.cancels) / static_cast<double>(n),
                    100.0 * static_cast<double>(stream.markets) / static_cast<double>(n));

        // --- Throughput. Runs are interleaved (A B C D, A B C D, ...) rather
        // than grouped (A A A, B B B, ...), so slow drift such as thermal
        // throttling on a fanless laptop is spread across all implementations
        // instead of penalising whichever happens to run last.
        std::vector<std::vector<double>> mops(variants.size());
        std::vector<Checksum> sums(variants.size());
        for (int r = 0; r < options.repeats; ++r) {
            for (std::size_t v = 0; v < variants.size(); ++v) {
                const RunResult result = variants[v].run(stream, nullptr);
                mops[v].push_back(static_cast<double>(n) / result.seconds / 1e6);
                sums[v] = result.sum;
            }
        }
        for (std::size_t v = 1; v < variants.size(); ++v) {
            if (!(sums[v] == sums[0])) {
                std::fprintf(stderr, "FATAL: %s produced different results from the baseline\n",
                             variants[v].name.c_str());
                return 1;
            }
        }
        std::printf("    result of every implementation: %llu trades, %llu cancels, %llu orders left resting\n\n",
                    static_cast<unsigned long long>(sums[0].trades),
                    static_cast<unsigned long long>(sums[0].cancels_ok),
                    static_cast<unsigned long long>(sums[0].final_orders));

        std::printf("    %-20s %10s %8s | %6s %6s %6s %7s %9s   (latency per op, ns)\n", "implementation",
                    "M ops/s", "ns/op", "p50", "p90", "p99", "p99.9", "max");

        std::vector<std::string> type_lines;
        for (std::size_t v = 0; v < variants.size(); ++v) {
            // --- Latency pass.
            std::vector<std::uint32_t> samples;
            const RunResult result = variants[v].run(stream, &samples);
            if (!(result.sum == sums[0])) {
                std::fprintf(stderr, "FATAL: latency pass of %s diverged\n", variants[v].name.c_str());
                return 1;
            }

            // Split by operation type before sorting destroys the original order.
            std::vector<std::uint32_t> by_type[3];
            for (std::size_t i = 0; i < samples.size(); ++i) {
                by_type[static_cast<int>(stream.ops[stream.warmup_ops + i].type)].push_back(samples[i]);
            }
            const Percentiles all = summarise(samples);
            const Percentiles limit = summarise(by_type[static_cast<int>(OpType::Limit)]);
            const Percentiles market = summarise(by_type[static_cast<int>(OpType::Market)]);
            const Percentiles cancel = summarise(by_type[static_cast<int>(OpType::Cancel)]);

            const double med = median(mops[v]);
            const double lo = *std::min_element(mops[v].begin(), mops[v].end());
            const double hi = *std::max_element(mops[v].begin(), mops[v].end());
            const double mean_ns = 1e3 / med;   // from the throughput pass: no clock calls inside

            std::printf("    %-20s %10.2f %8.1f | %6u %6u %6u %7u %9u\n", variants[v].name.c_str(), med,
                        mean_ns, all.p50, all.p90, all.p99, all.p999, all.max);

            char line[256];
            std::snprintf(line, sizeof(line),
                          "    %-20s | %5u %5u %6u | %5u %5u %6u | %5u %5u %6u", variants[v].name.c_str(),
                          limit.p50, limit.p99, limit.p999, cancel.p50, cancel.p99, cancel.p999,
                          market.p50, market.p99, market.p999);
            type_lines.emplace_back(line);

            if (csv.is_open()) {
                csv << scenario.name << ',' << variants[v].levels << ',' << variants[v].queue << ','
                    << build << ',' << n << ',' << options.repeats << ',' << clock.tick_ns << ','
                    << med << ',' << lo << ',' << hi << ',' << mean_ns << ','
                    << all.p50 << ',' << all.p90 << ',' << all.p99 << ',' << all.p999 << ',' << all.max << ','
                    << limit.p50 << ',' << limit.p99 << ',' << limit.p999 << ','
                    << cancel.p50 << ',' << cancel.p99 << ',' << cancel.p999 << ','
                    << market.p50 << ',' << market.p99 << ',' << market.p999 << '\n';
            }
        }

        std::printf("\n    %-20s | %-18s | %-18s | %-18s\n", "by type (ns)", "limit p50/p99/p99.9",
                    "cancel p50/p99/p99.9", "market p50/p99/p99.9");
        for (const std::string& line : type_lines) {
            std::printf("%s\n", line.c_str());
        }
        std::printf("\n");
    }
    return 0;
}
