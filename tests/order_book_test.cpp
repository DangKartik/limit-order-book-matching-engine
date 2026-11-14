// Hand-written scenario tests: each one pins down a single matching rule.
#include <gtest/gtest.h>

#include <vector>

#include "lob/order_book.hpp"

namespace {

using lob::LevelView;
using lob::OrderId;
using lob::OrderResult;
using lob::RejectReason;
using lob::Side;
using lob::Trade;

// A typed test fixture. GoogleTest compiles every TYPED_TEST below once per
// book type in BookTypes, so each rule is checked against all four
// implementations. It constructs a fresh fixture for every test, so each one
// starts with an empty book and an empty trade buffer.
//
// Inside a TYPED_TEST the fixture is a template base class, and C++ requires
// `this->` to reach members of a template base. That is why every test says
// this->book rather than just book.
template <typename Book>
class OrderBookTest : public ::testing::Test {
protected:
    Book book;
    std::vector<Trade> trades;

    OrderId buy(lob::Price price, lob::Quantity qty) {
        return book.add_limit(Side::Buy, price, qty, trades).id;
    }
    OrderId sell(lob::Price price, lob::Quantity qty) {
        return book.add_limit(Side::Sell, price, qty, trades).id;
    }
};

using BookTypes =
    ::testing::Types<lob::MapListBook, lob::MapPoolBook, lob::FlatListBook, lob::FlatPoolBook>;
TYPED_TEST_SUITE(OrderBookTest, BookTypes);

// --- Empty book and resting ------------------------------------------------

TYPED_TEST(OrderBookTest, EmptyBookHasNoBestPrices) {
    EXPECT_TRUE(this->book.empty());
    EXPECT_EQ(this->book.best_bid(), std::nullopt);
    EXPECT_EQ(this->book.best_ask(), std::nullopt);
    EXPECT_EQ(this->book.order_count(), 0u);
    EXPECT_TRUE(this->book.depth(Side::Buy, 5).empty());
}

TYPED_TEST(OrderBookTest, LimitOrderRestsWhenNothingToMatch) {
    const OrderResult r = this->book.add_limit(Side::Buy, 100, 10, this->trades);

    EXPECT_EQ(r, (OrderResult{.id = 1, .filled = 0, .resting = 10}));
    EXPECT_TRUE(this->trades.empty());
    EXPECT_EQ(this->book.best_bid(), 100);
    EXPECT_EQ(this->book.best_ask(), std::nullopt);
    EXPECT_EQ(this->book.quantity_at(Side::Buy, 100), 10);
    EXPECT_EQ(this->book.order_count(), 1u);
}

TYPED_TEST(OrderBookTest, BestPricesTrackTheMostAggressiveLevel) {
    this->buy(99, 1);
    this->buy(101, 1);
    this->buy(100, 1);
    this->sell(105, 1);
    this->sell(103, 1);
    this->sell(104, 1);

    EXPECT_EQ(this->book.best_bid(), 101);   // highest bid
    EXPECT_EQ(this->book.best_ask(), 103);   // lowest ask
}

TYPED_TEST(OrderBookTest, NonCrossingOrdersDoNotTrade) {
    this->buy(100, 10);
    this->sell(101, 10);

    EXPECT_TRUE(this->trades.empty());
    EXPECT_EQ(this->book.order_count(), 2u);
}

TYPED_TEST(OrderBookTest, OrderIdsIncreaseFromOne) {
    EXPECT_EQ(this->buy(100, 1), 1u);
    EXPECT_EQ(this->sell(200, 1), 2u);
    EXPECT_EQ(this->book.add_market(Side::Buy, 1, this->trades).id, 3u);
}

// --- Matching --------------------------------------------------------------

TYPED_TEST(OrderBookTest, ExactFillRemovesBothOrders) {
    const OrderId maker = this->sell(100, 10);
    const OrderResult r = this->book.add_limit(Side::Buy, 100, 10, this->trades);

    EXPECT_EQ(r.filled, 10);
    EXPECT_EQ(r.resting, 0);
    ASSERT_EQ(this->trades.size(), 1u);
    EXPECT_EQ(this->trades[0], (Trade{.maker_id = maker,
                                .taker_id = r.id,
                                .price = 100,
                                .qty = 10,
                                .taker_side = Side::Buy}));
    EXPECT_TRUE(this->book.empty());
    EXPECT_EQ(this->book.ask_level_count(), 0u);   // the emptied level is gone
}

TYPED_TEST(OrderBookTest, TradePrintsAtTheMakersPrice) {
    this->sell(100, 10);
    // The buyer was willing to pay 105 but gets the resting price of 100.
    this->book.add_limit(Side::Buy, 105, 10, this->trades);

    ASSERT_EQ(this->trades.size(), 1u);
    EXPECT_EQ(this->trades[0].price, 100);
}

TYPED_TEST(OrderBookTest, SellTakerTradesAtTheBidPrice) {
    const OrderId maker = this->buy(100, 10);
    const OrderResult r = this->book.add_limit(Side::Sell, 95, 4, this->trades);

    ASSERT_EQ(this->trades.size(), 1u);
    EXPECT_EQ(this->trades[0], (Trade{.maker_id = maker,
                                .taker_id = r.id,
                                .price = 100,
                                .qty = 4,
                                .taker_side = Side::Sell}));
    EXPECT_EQ(this->book.quantity_at(Side::Buy, 100), 6);
}

TYPED_TEST(OrderBookTest, PartiallyFilledTakerRestsTheRemainder) {
    this->sell(100, 4);
    const OrderResult r = this->book.add_limit(Side::Buy, 100, 10, this->trades);

    EXPECT_EQ(r.filled, 4);
    EXPECT_EQ(r.resting, 6);
    EXPECT_EQ(this->book.best_bid(), 100);
    EXPECT_EQ(this->book.quantity_at(Side::Buy, 100), 6);
    EXPECT_EQ(this->book.best_ask(), std::nullopt);
}

TYPED_TEST(OrderBookTest, PartiallyFilledMakerKeepsItsPlaceInTheQueue) {
    const OrderId first = this->sell(100, 10);
    const OrderId second = this->sell(100, 10);

    this->book.add_limit(Side::Buy, 100, 4, this->trades);   // takes 4 of `first`
    this->book.add_limit(Side::Buy, 100, 8, this->trades);   // must finish `first` (6) before touching `second` (2)

    ASSERT_EQ(this->trades.size(), 3u);
    EXPECT_EQ(this->trades[0].maker_id, first);
    EXPECT_EQ(this->trades[0].qty, 4);
    EXPECT_EQ(this->trades[1].maker_id, first);
    EXPECT_EQ(this->trades[1].qty, 6);
    EXPECT_EQ(this->trades[2].maker_id, second);
    EXPECT_EQ(this->trades[2].qty, 2);
    EXPECT_EQ(this->book.quantity_at(Side::Sell, 100), 8);
}

TYPED_TEST(OrderBookTest, TimePriorityWithinALevelIsFifo) {
    const OrderId a = this->sell(100, 5);
    const OrderId b = this->sell(100, 5);
    const OrderId c = this->sell(100, 5);

    this->book.add_limit(Side::Buy, 100, 15, this->trades);

    ASSERT_EQ(this->trades.size(), 3u);
    EXPECT_EQ(this->trades[0].maker_id, a);
    EXPECT_EQ(this->trades[1].maker_id, b);
    EXPECT_EQ(this->trades[2].maker_id, c);
}

TYPED_TEST(OrderBookTest, BetterPriceBeatsEarlierTime) {
    const OrderId early_worse = this->sell(101, 5);
    const OrderId late_better = this->sell(100, 5);

    this->book.add_limit(Side::Buy, 101, 10, this->trades);

    ASSERT_EQ(this->trades.size(), 2u);
    EXPECT_EQ(this->trades[0].maker_id, late_better);
    EXPECT_EQ(this->trades[0].price, 100);
    EXPECT_EQ(this->trades[1].maker_id, early_worse);
    EXPECT_EQ(this->trades[1].price, 101);
}

TYPED_TEST(OrderBookTest, LimitOrderSweepsLevelsButStopsAtItsLimit) {
    this->sell(100, 5);
    this->sell(101, 5);
    this->sell(106, 5);

    const OrderResult r = this->book.add_limit(Side::Buy, 105, 12, this->trades);

    ASSERT_EQ(this->trades.size(), 2u);
    EXPECT_EQ(this->trades[0].price, 100);
    EXPECT_EQ(this->trades[1].price, 101);
    EXPECT_EQ(r.filled, 10);
    EXPECT_EQ(r.resting, 2);
    EXPECT_EQ(this->book.best_bid(), 105);   // remainder rests at the limit price
    EXPECT_EQ(this->book.best_ask(), 106);   // untouched: 106 is worse than the 105 limit
}

TYPED_TEST(OrderBookTest, SellSweepsBidsFromHighestDown) {
    this->buy(100, 5);
    this->buy(99, 5);
    this->buy(98, 5);

    const OrderResult r = this->book.add_limit(Side::Sell, 99, 20, this->trades);

    ASSERT_EQ(this->trades.size(), 2u);
    EXPECT_EQ(this->trades[0].price, 100);
    EXPECT_EQ(this->trades[1].price, 99);
    EXPECT_EQ(r.filled, 10);
    EXPECT_EQ(r.resting, 10);
    EXPECT_EQ(this->book.best_ask(), 99);
    EXPECT_EQ(this->book.best_bid(), 98);
}

TYPED_TEST(OrderBookTest, BookIsNeverLeftCrossed) {
    this->buy(100, 5);
    this->sell(100, 8);   // fills 5, rests 3 at 100

    EXPECT_EQ(this->book.best_bid(), std::nullopt);
    EXPECT_EQ(this->book.best_ask(), 100);
}

// --- Market orders ---------------------------------------------------------

TYPED_TEST(OrderBookTest, MarketOrderSweepsAcrossPrices) {
    this->sell(100, 5);
    this->sell(110, 5);
    this->sell(500, 5);

    const OrderResult r = this->book.add_market(Side::Buy, 12, this->trades);

    ASSERT_EQ(this->trades.size(), 3u);
    EXPECT_EQ(this->trades[0].price, 100);
    EXPECT_EQ(this->trades[1].price, 110);
    EXPECT_EQ(this->trades[2].price, 500);
    EXPECT_EQ(this->trades[2].qty, 2);
    EXPECT_EQ(r.filled, 12);
    EXPECT_EQ(r.resting, 0);
    EXPECT_EQ(this->book.quantity_at(Side::Sell, 500), 3);
}

TYPED_TEST(OrderBookTest, MarketOrderRemainderIsCancelledNotRested) {
    this->sell(100, 5);

    const OrderResult r = this->book.add_market(Side::Buy, 8, this->trades);

    EXPECT_EQ(r.filled, 5);
    EXPECT_EQ(r.resting, 0);
    EXPECT_TRUE(this->book.empty());                  // nothing rests on either side
    EXPECT_EQ(this->book.best_bid(), std::nullopt);
    EXPECT_FALSE(this->book.cancel(r.id));            // there is nothing left to cancel
}

TYPED_TEST(OrderBookTest, MarketOrderOnEmptyBookDoesNothing) {
    const OrderResult r = this->book.add_market(Side::Sell, 10, this->trades);

    EXPECT_NE(r.id, 0u);                        // accepted, just unfilled
    EXPECT_EQ(r.reject, RejectReason::None);
    EXPECT_EQ(r.filled, 0);
    EXPECT_TRUE(this->trades.empty());
    EXPECT_TRUE(this->book.empty());
}

TYPED_TEST(OrderBookTest, MarketSellHitsBids) {
    this->buy(100, 5);
    this->buy(90, 5);

    const OrderResult r = this->book.add_market(Side::Sell, 7, this->trades);

    ASSERT_EQ(this->trades.size(), 2u);
    EXPECT_EQ(this->trades[0].price, 100);
    EXPECT_EQ(this->trades[1].price, 90);
    EXPECT_EQ(this->trades[1].taker_side, Side::Sell);
    EXPECT_EQ(r.filled, 7);
    EXPECT_EQ(this->book.quantity_at(Side::Buy, 90), 3);
}

// --- Cancel ----------------------------------------------------------------

TYPED_TEST(OrderBookTest, CancelRemovesOrderAndItsLevel) {
    const OrderId id = this->buy(100, 10);

    EXPECT_TRUE(this->book.cancel(id));
    EXPECT_TRUE(this->book.empty());
    EXPECT_EQ(this->book.bid_level_count(), 0u);
    EXPECT_EQ(this->book.best_bid(), std::nullopt);
}

TYPED_TEST(OrderBookTest, CancelUnknownIdFails) {
    this->buy(100, 10);
    EXPECT_FALSE(this->book.cancel(999));
    EXPECT_FALSE(this->book.cancel(0));
    EXPECT_EQ(this->book.order_count(), 1u);
}

TYPED_TEST(OrderBookTest, CancelTwiceFailsTheSecondTime) {
    const OrderId id = this->buy(100, 10);
    EXPECT_TRUE(this->book.cancel(id));
    EXPECT_FALSE(this->book.cancel(id));
}

TYPED_TEST(OrderBookTest, CancelAfterFullFillFails) {
    const OrderId maker = this->sell(100, 10);
    this->book.add_market(Side::Buy, 10, this->trades);
    EXPECT_FALSE(this->book.cancel(maker));
}

TYPED_TEST(OrderBookTest, CancelFromMiddleOfQueuePreservesFifo) {
    const OrderId a = this->sell(100, 5);
    const OrderId b = this->sell(100, 5);
    const OrderId c = this->sell(100, 5);

    EXPECT_TRUE(this->book.cancel(b));
    EXPECT_EQ(this->book.quantity_at(Side::Sell, 100), 10);

    this->book.add_market(Side::Buy, 10, this->trades);
    ASSERT_EQ(this->trades.size(), 2u);
    EXPECT_EQ(this->trades[0].maker_id, a);
    EXPECT_EQ(this->trades[1].maker_id, c);
}

TYPED_TEST(OrderBookTest, CancelPartiallyFilledOrderRemovesOnlyTheRemainder) {
    const OrderId maker = this->sell(100, 10);
    this->sell(100, 7);
    this->book.add_market(Side::Buy, 4, this->trades);   // maker has 6 left

    EXPECT_TRUE(this->book.cancel(maker));
    EXPECT_EQ(this->book.quantity_at(Side::Sell, 100), 7);
    EXPECT_EQ(this->book.order_count(), 1u);
}

TYPED_TEST(OrderBookTest, CancelBestLevelExposesTheNextOne) {
    this->buy(99, 5);
    const OrderId best = this->buy(100, 5);

    EXPECT_TRUE(this->book.cancel(best));
    EXPECT_EQ(this->book.best_bid(), 99);
}

// --- Validation ------------------------------------------------------------

TYPED_TEST(OrderBookTest, RejectsNonPositiveQuantity) {
    EXPECT_EQ(this->book.add_limit(Side::Buy, 100, 0, this->trades).reject, RejectReason::InvalidQuantity);
    EXPECT_EQ(this->book.add_limit(Side::Buy, 100, -5, this->trades).reject, RejectReason::InvalidQuantity);
    EXPECT_EQ(this->book.add_market(Side::Sell, 0, this->trades).reject, RejectReason::InvalidQuantity);
    EXPECT_TRUE(this->book.empty());
}

TYPED_TEST(OrderBookTest, RejectsNonPositiveLimitPrice) {
    EXPECT_EQ(this->book.add_limit(Side::Buy, 0, 10, this->trades).reject, RejectReason::InvalidPrice);
    EXPECT_EQ(this->book.add_limit(Side::Sell, -1, 10, this->trades).reject, RejectReason::InvalidPrice);
    EXPECT_TRUE(this->book.empty());
}

TYPED_TEST(OrderBookTest, RejectedOrdersDoNotConsumeAnId) {
    const OrderResult rejected = this->book.add_limit(Side::Buy, 100, 0, this->trades);
    EXPECT_EQ(rejected.id, 0u);
    EXPECT_EQ(this->buy(100, 1), 1u);
}

// --- Trade buffer and depth ------------------------------------------------

TYPED_TEST(OrderBookTest, TradesAreAppendedToTheCallersBuffer) {
    this->sell(100, 1);
    this->sell(100, 1);

    this->book.add_market(Side::Buy, 1, this->trades);
    ASSERT_EQ(this->trades.size(), 1u);

    // The engine never clears the buffer: the second trade lands after the first.
    this->book.add_market(Side::Buy, 1, this->trades);
    ASSERT_EQ(this->trades.size(), 2u);
    EXPECT_EQ(this->trades[0].maker_id, 1u);
    EXPECT_EQ(this->trades[1].maker_id, 2u);
}

TYPED_TEST(OrderBookTest, DepthListsLevelsFromBestOutwards) {
    this->buy(100, 5);
    this->buy(100, 3);
    this->buy(99, 7);
    this->buy(98, 1);
    this->sell(101, 2);
    this->sell(103, 4);

    const std::vector<LevelView> bids = this->book.depth(Side::Buy, 2);
    ASSERT_EQ(bids.size(), 2u);   // capped at max_levels
    EXPECT_EQ(bids[0], (LevelView{.price = 100, .qty = 8, .order_count = 2}));
    EXPECT_EQ(bids[1], (LevelView{.price = 99, .qty = 7, .order_count = 1}));

    const std::vector<LevelView> asks = this->book.depth(Side::Sell, 10);
    ASSERT_EQ(asks.size(), 2u);
    EXPECT_EQ(asks[0], (LevelView{.price = 101, .qty = 2, .order_count = 1}));
    EXPECT_EQ(asks[1], (LevelView{.price = 103, .qty = 4, .order_count = 1}));
}

// --- Price range and best-price tracking -----------------------------------
// These matter most for the flat-array books, but every implementation must
// give the same answers.

TYPED_TEST(OrderBookTest, RejectsPricesOutsideTheConfiguredRange) {
    const lob::Price max_price = this->book.config().max_price;

    EXPECT_EQ(this->book.add_limit(Side::Buy, max_price + 1, 1, this->trades).reject,
              RejectReason::InvalidPrice);
    EXPECT_EQ(this->book.add_limit(Side::Sell, max_price, 1, this->trades).reject,
              RejectReason::None);   // the bounds themselves are valid
    EXPECT_EQ(this->book.add_limit(Side::Buy, 1, 1, this->trades).reject, RejectReason::None);
    EXPECT_EQ(this->book.best_ask(), max_price);
    EXPECT_EQ(this->book.best_bid(), 1);
    EXPECT_EQ(this->book.quantity_at(Side::Buy, max_price + 5), 0);   // out of range reads as empty
    EXPECT_EQ(this->book.quantity_at(Side::Buy, -3), 0);
}

TYPED_TEST(OrderBookTest, BestPriceSkipsAcrossAWideGap) {
    this->buy(5, 1);
    const OrderId near = this->buy(90'000, 1);
    this->sell(99'000, 1);
    const OrderId near_ask = this->sell(90'500, 1);

    EXPECT_TRUE(this->book.cancel(near));       // next bid is 89,995 ticks away
    EXPECT_EQ(this->book.best_bid(), 5);
    EXPECT_TRUE(this->book.cancel(near_ask));
    EXPECT_EQ(this->book.best_ask(), 99'000);
}

TYPED_TEST(OrderBookTest, SideCanEmptyAndRefill) {
    const OrderId a = this->buy(100, 1);
    EXPECT_TRUE(this->book.cancel(a));
    EXPECT_EQ(this->book.best_bid(), std::nullopt);

    this->buy(50, 2);                            // worse than the old best
    EXPECT_EQ(this->book.best_bid(), 50);
    this->buy(70, 2);
    EXPECT_EQ(this->book.best_bid(), 70);
    EXPECT_EQ(this->book.bid_level_count(), 2u);
}

TYPED_TEST(OrderBookTest, CancellingANonBestLevelLeavesBestAlone) {
    this->sell(100, 1);
    const OrderId far = this->sell(200, 1);
    EXPECT_TRUE(this->book.cancel(far));
    EXPECT_EQ(this->book.best_ask(), 100);
    EXPECT_EQ(this->book.ask_level_count(), 1u);
}

TYPED_TEST(OrderBookTest, DepthSkipsEmptyPricesBetweenLevels) {
    this->buy(100, 1);
    this->buy(40, 2);
    this->buy(7, 3);

    const std::vector<LevelView> bids = this->book.depth(Side::Buy, 10);
    ASSERT_EQ(bids.size(), 3u);
    EXPECT_EQ(bids[0].price, 100);
    EXPECT_EQ(bids[1].price, 40);
    EXPECT_EQ(bids[2].price, 7);
}

TYPED_TEST(OrderBookTest, InvalidConfigThrows) {
    EXPECT_THROW(TypeParam(lob::BookConfig{.min_price = 0, .max_price = 10}), std::invalid_argument);
    EXPECT_THROW(TypeParam(lob::BookConfig{.min_price = 10, .max_price = 9}), std::invalid_argument);
}

}  // namespace
