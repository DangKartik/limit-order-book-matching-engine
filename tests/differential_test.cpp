// Differential (model-based) test: feed the same random order stream to the
// real engine and to the naive ReferenceBook and require identical behaviour
// after every single operation. The hand-written tests check the rules I
// thought of; this one finds the interleavings I did not.
#include <gtest/gtest.h>

#include <cstdint>
#include <random>
#include <vector>

#include "lob/order_book.hpp"
#include "reference_book.hpp"

namespace {

using lob::OrderId;
using lob::OrderResult;
using lob::Price;
using lob::Quantity;
using lob::Side;
using lob::Trade;
using lob::testing::ReferenceBook;

// Runs one seeded random stream through `Book` and the reference book.
// A function template so the same body checks every implementation.
template <typename Book>
void run_differential(std::uint64_t seed) {
    constexpr int kOperations = 20'000;

    // A fixed seed makes the "random" stream identical on every run, so a
    // failure is reproducible. mt19937_64 is the standard Mersenne Twister.
    std::mt19937_64 rng(seed);

    // The book accepts prices 90..110. Prices are drawn from a slightly wider
    // band, so buys and sells overlap often (a wide band would mostly produce
    // orders that rest and never trade), the edges of the range are hit, and a
    // few orders fall outside it and must be rejected identically.
    const lob::BookConfig config{.min_price = 90, .max_price = 110};
    std::uniform_int_distribution<Price> price_dist(88, 112);
    std::uniform_int_distribution<Quantity> qty_dist(1, 20);
    std::uniform_int_distribution<int> percent(0, 99);

    Book book(config);
    ReferenceBook reference(config);
    std::vector<Trade> book_trades;
    std::vector<Trade> ref_trades;
    OrderId last_id = 0;
    std::size_t total_trades = 0;
    std::size_t successful_cancels = 0;

    for (int op = 0; op < kOperations; ++op) {
        // On failure, GoogleTest prints this so we know which operation diverged.
        SCOPED_TRACE(::testing::Message() << "seed " << seed << ", operation " << op);

        book_trades.clear();   // clear() keeps the capacity: no reallocation
        ref_trades.clear();

        const int roll = percent(rng);
        const Side side = (percent(rng) < 50) ? Side::Buy : Side::Sell;

        if (roll < 55) {   // 55%: limit order
            const Price price = price_dist(rng);
            const Quantity qty = qty_dist(rng);
            const OrderResult a = book.add_limit(side, price, qty, book_trades);
            const OrderResult b = reference.add_limit(side, price, qty, ref_trades);
            ASSERT_EQ(a, b);
            if (a.id != 0) {   // 0 means rejected (price out of range)
                last_id = a.id;
            }
        } else if (roll < 65) {   // 10%: market order
            const Quantity qty = qty_dist(rng);
            const OrderResult a = book.add_market(side, qty, book_trades);
            const OrderResult b = reference.add_market(side, qty, ref_trades);
            ASSERT_EQ(a, b);
            last_id = a.id;
        } else if (last_id > 0) {   // 35%: cancel
            // Pick any id issued so far, biased towards recent ones (which are
            // more likely to still be resting). Many picks will be dead ids,
            // which exercises the "cancel fails" path too.
            std::uniform_int_distribution<OrderId> recent(last_id > 50 ? last_id - 50 : 1, last_id);
            const OrderId target = recent(rng);
            const bool a = book.cancel(target);
            const bool b = reference.cancel(target);
            ASSERT_EQ(a, b);
            successful_cancels += a ? 1u : 0u;
        }

        ASSERT_EQ(book_trades, ref_trades);
        ASSERT_EQ(book.best_bid(), reference.best_bid());
        ASSERT_EQ(book.best_ask(), reference.best_ask());
        ASSERT_EQ(book.order_count(), reference.order_count());
        total_trades += book_trades.size();

        // The book must never be crossed: a bid at or above the best ask
        // should have traded instead of resting.
        if (book.best_bid() && book.best_ask()) {
            ASSERT_LT(*book.best_bid(), *book.best_ask());
        }

        // Full depth comparison is more expensive, so do it periodically.
        if (op % 100 == 0) {
            ASSERT_EQ(book.depth(Side::Buy, 1000), reference.depth(Side::Buy, 1000));
            ASSERT_EQ(book.depth(Side::Sell, 1000), reference.depth(Side::Sell, 1000));
        }
    }

    // Guard against a vacuous pass: if the stream never traded or cancelled,
    // "both books agree" would prove nothing.
    EXPECT_GT(total_trades, 1000u);
    EXPECT_GT(successful_cancels, 1000u);
}

template <typename Book>
class DifferentialTest : public ::testing::Test {};

using BookTypes =
    ::testing::Types<lob::MapListBook, lob::MapPoolBook, lob::FlatListBook, lob::FlatPoolBook>;
TYPED_TEST_SUITE(DifferentialTest, BookTypes);

TYPED_TEST(DifferentialTest, EngineMatchesReferenceOnRandomStreams) {
    for (const std::uint64_t seed : {1u, 2u, 3u, 42u, 20261114u}) {
        run_differential<TypeParam>(seed);
        // ASSERT_* only returns from the function it is in (run_differential),
        // so check here whether it failed and stop instead of piling on errors.
        if (::testing::Test::HasFatalFailure()) {
            return;
        }
    }
}

}  // namespace
