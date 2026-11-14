// Generates a synthetic, reproducible order stream for the benchmarks.
//
// The stream is produced ONCE, up front, and stored in a vector. Every book
// implementation then replays exactly the same operations, so a difference in
// timing can only come from the data structures, and the cost of generating
// random numbers is never inside a timed region.
#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <random>
#include <string>
#include <vector>

#include "lob/order_book.hpp"

namespace lob::bench {

enum class OpType : std::uint8_t { Limit, Market, Cancel };

struct Op {
    OpType type;
    Side side;        // Limit, Market
    Price price;      // Limit
    Quantity qty;     // Limit, Market
    OrderId id;       // Cancel
};

struct Scenario {
    std::string name;
    std::size_t target_depth;    // resting orders the stream tries to hold in the book
    double mean_offset_ticks;    // average distance of a passive order from the mid price
    std::size_t measured_ops = 2'000'000;
    std::uint64_t seed = 12345;
    Price min_price = 1;
    Price max_price = 20'000;
};

struct Stream {
    std::vector<Op> ops;
    std::size_t warmup_ops = 0;   // ops[0 .. warmup_ops) build the initial book and are not timed
    BookConfig book;
    std::size_t limits = 0, markets = 0, cancels = 0;   // counts within the measured part
};

// Model: a mid price doing a slow random walk. Limit orders arrive on both
// sides at a geometrically distributed distance from the mid (most liquidity
// sits near the touch), a tenth of them priced through the mid so they trade.
// Cancels pick a resting order uniformly at random. Market orders are small.
// When the book is below its target depth the mix leans towards adding orders,
// above it towards cancelling, so the depth stays near the target. The mix that
// results is printed by the harness rather than assumed.
//
// The generator runs a real book while it works. That is how it knows which
// orders are still resting (so cancels hit live orders, as they mostly do on a
// real venue) and what id the engine will assign to each order: ids are
// deterministic, so the replayed books assign the same ones.
inline Stream generate(const Scenario& scenario) {
    Stream stream;
    stream.book = BookConfig{.min_price = scenario.min_price, .max_price = scenario.max_price};

    std::mt19937_64 rng(scenario.seed);
    std::uniform_int_distribution<int> percent(0, 99);
    std::uniform_int_distribution<Quantity> limit_qty(1, 100);
    std::uniform_int_distribution<Quantity> market_qty(1, 200);
    std::uniform_int_distribution<Price> cross_by(0, 2);
    // geometric_distribution(p) has mean (1 - p) / p, so this p gives the requested mean.
    std::geometric_distribution<Price> offset(1.0 / (1.0 + scenario.mean_offset_ticks));

    MapListBook book(stream.book);
    std::vector<Trade> trades;
    std::vector<OrderId> maybe_resting;   // ids we rested and have not cancelled (some since filled)

    const Price centre = (scenario.min_price + scenario.max_price) / 2;
    const Price band = (scenario.max_price - scenario.min_price) / 4;
    Price mid = centre;

    const auto emit_limit = [&](bool allow_crossing) {
        const Side side = (percent(rng) < 50) ? Side::Buy : Side::Sell;
        Price price;
        if (allow_crossing && percent(rng) < 10) {
            price = (side == Side::Buy) ? mid + cross_by(rng) : mid - cross_by(rng);
        } else {
            const Price distance = 1 + offset(rng);
            price = (side == Side::Buy) ? mid - distance : mid + distance;
        }
        price = std::clamp(price, scenario.min_price, scenario.max_price);
        const Quantity qty = limit_qty(rng);

        trades.clear();
        const OrderResult result = book.add_limit(side, price, qty, trades);
        if (result.resting > 0) {
            maybe_resting.push_back(result.id);
        }
        stream.ops.push_back(Op{OpType::Limit, side, price, qty, 0});
    };

    // Returns false if no live order could be found to cancel.
    const auto emit_cancel = [&]() {
        while (!maybe_resting.empty()) {
            std::uniform_int_distribution<std::size_t> pick(0, maybe_resting.size() - 1);
            const std::size_t i = pick(rng);
            const OrderId id = maybe_resting[i];
            maybe_resting[i] = maybe_resting.back();   // swap-and-pop: O(1) removal, order not preserved
            maybe_resting.pop_back();
            if (book.cancel(id)) {                     // false means it was already filled; try another
                stream.ops.push_back(Op{OpType::Cancel, Side::Buy, 0, 0, id});
                return true;
            }
        }
        return false;
    };

    // Warm-up: fill the book to its target depth with passive orders.
    while (book.order_count() < scenario.target_depth) {
        emit_limit(/*allow_crossing=*/false);
    }
    stream.warmup_ops = stream.ops.size();

    // Measured part.
    while (stream.ops.size() < stream.warmup_ops + scenario.measured_ops) {
        if (percent(rng) < 2) {   // the mid moves one tick on about 2% of operations
            mid += (percent(rng) < 50) ? 1 : -1;
            mid = std::clamp(mid, centre - band, centre + band);
        }

        const bool below_target = book.order_count() < scenario.target_depth;
        // 5% are market orders; the rest is split between limits and cancels.
        // The split is deliberately lopsided in each direction because limit
        // orders also leave the book by being filled, not only by cancels.
        const int limit_share = below_target ? 75 : 35;
        const int roll = percent(rng);

        if (roll < 5) {
            const Side side = (percent(rng) < 50) ? Side::Buy : Side::Sell;
            const Quantity qty = market_qty(rng);
            trades.clear();
            book.add_market(side, qty, trades);
            stream.ops.push_back(Op{OpType::Market, side, 0, qty, 0});
            ++stream.markets;
        } else if (roll < 5 + limit_share || !emit_cancel()) {
            emit_limit(/*allow_crossing=*/true);
            ++stream.limits;
        } else {
            ++stream.cancels;
        }
    }
    return stream;
}

}  // namespace lob::bench
