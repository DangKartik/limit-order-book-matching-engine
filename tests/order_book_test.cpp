// Hand-written scenario tests: each one pins down a single matching rule.
#include <gtest/gtest.h>

#include <vector>

#include "lob/order_book.hpp"

namespace {

using lob::LevelView;
using lob::OrderBook;
using lob::OrderId;
using lob::OrderResult;
using lob::RejectReason;
using lob::Side;
using lob::Trade;

// A test fixture: GoogleTest constructs a fresh one for every TEST_F, so each
// test starts with an empty book and an empty trade buffer.
class OrderBookTest : public ::testing::Test {
protected:
    OrderBook book;
    std::vector<Trade> trades;

    OrderId buy(lob::Price price, lob::Quantity qty) {
        return book.add_limit(Side::Buy, price, qty, trades).id;
    }
    OrderId sell(lob::Price price, lob::Quantity qty) {
        return book.add_limit(Side::Sell, price, qty, trades).id;
    }
};

// --- Empty book and resting ------------------------------------------------

TEST_F(OrderBookTest, EmptyBookHasNoBestPrices) {
    EXPECT_TRUE(book.empty());
    EXPECT_EQ(book.best_bid(), std::nullopt);
    EXPECT_EQ(book.best_ask(), std::nullopt);
    EXPECT_EQ(book.order_count(), 0u);
    EXPECT_TRUE(book.depth(Side::Buy, 5).empty());
}

TEST_F(OrderBookTest, LimitOrderRestsWhenNothingToMatch) {
    const OrderResult r = book.add_limit(Side::Buy, 100, 10, trades);

    EXPECT_EQ(r, (OrderResult{.id = 1, .filled = 0, .resting = 10}));
    EXPECT_TRUE(trades.empty());
    EXPECT_EQ(book.best_bid(), 100);
    EXPECT_EQ(book.best_ask(), std::nullopt);
    EXPECT_EQ(book.quantity_at(Side::Buy, 100), 10);
    EXPECT_EQ(book.order_count(), 1u);
}

TEST_F(OrderBookTest, BestPricesTrackTheMostAggressiveLevel) {
    buy(99, 1);
    buy(101, 1);
    buy(100, 1);
    sell(105, 1);
    sell(103, 1);
    sell(104, 1);

    EXPECT_EQ(book.best_bid(), 101);   // highest bid
    EXPECT_EQ(book.best_ask(), 103);   // lowest ask
}

TEST_F(OrderBookTest, NonCrossingOrdersDoNotTrade) {
    buy(100, 10);
    sell(101, 10);

    EXPECT_TRUE(trades.empty());
    EXPECT_EQ(book.order_count(), 2u);
}

TEST_F(OrderBookTest, OrderIdsIncreaseFromOne) {
    EXPECT_EQ(buy(100, 1), 1u);
    EXPECT_EQ(sell(200, 1), 2u);
    EXPECT_EQ(book.add_market(Side::Buy, 1, trades).id, 3u);
}

// --- Matching --------------------------------------------------------------

TEST_F(OrderBookTest, ExactFillRemovesBothOrders) {
    const OrderId maker = sell(100, 10);
    const OrderResult r = book.add_limit(Side::Buy, 100, 10, trades);

    EXPECT_EQ(r.filled, 10);
    EXPECT_EQ(r.resting, 0);
    ASSERT_EQ(trades.size(), 1u);
    EXPECT_EQ(trades[0], (Trade{.maker_id = maker,
                                .taker_id = r.id,
                                .price = 100,
                                .qty = 10,
                                .taker_side = Side::Buy}));
    EXPECT_TRUE(book.empty());
    EXPECT_EQ(book.ask_level_count(), 0u);   // the emptied level is gone
}

TEST_F(OrderBookTest, TradePrintsAtTheMakersPrice) {
    sell(100, 10);
    // The buyer was willing to pay 105 but gets the resting price of 100.
    book.add_limit(Side::Buy, 105, 10, trades);

    ASSERT_EQ(trades.size(), 1u);
    EXPECT_EQ(trades[0].price, 100);
}

TEST_F(OrderBookTest, SellTakerTradesAtTheBidPrice) {
    const OrderId maker = buy(100, 10);
    const OrderResult r = book.add_limit(Side::Sell, 95, 4, trades);

    ASSERT_EQ(trades.size(), 1u);
    EXPECT_EQ(trades[0], (Trade{.maker_id = maker,
                                .taker_id = r.id,
                                .price = 100,
                                .qty = 4,
                                .taker_side = Side::Sell}));
    EXPECT_EQ(book.quantity_at(Side::Buy, 100), 6);
}

TEST_F(OrderBookTest, PartiallyFilledTakerRestsTheRemainder) {
    sell(100, 4);
    const OrderResult r = book.add_limit(Side::Buy, 100, 10, trades);

    EXPECT_EQ(r.filled, 4);
    EXPECT_EQ(r.resting, 6);
    EXPECT_EQ(book.best_bid(), 100);
    EXPECT_EQ(book.quantity_at(Side::Buy, 100), 6);
    EXPECT_EQ(book.best_ask(), std::nullopt);
}

TEST_F(OrderBookTest, PartiallyFilledMakerKeepsItsPlaceInTheQueue) {
    const OrderId first = sell(100, 10);
    const OrderId second = sell(100, 10);

    book.add_limit(Side::Buy, 100, 4, trades);   // takes 4 of `first`
    book.add_limit(Side::Buy, 100, 8, trades);   // must finish `first` (6) before touching `second` (2)

    ASSERT_EQ(trades.size(), 3u);
    EXPECT_EQ(trades[0].maker_id, first);
    EXPECT_EQ(trades[0].qty, 4);
    EXPECT_EQ(trades[1].maker_id, first);
    EXPECT_EQ(trades[1].qty, 6);
    EXPECT_EQ(trades[2].maker_id, second);
    EXPECT_EQ(trades[2].qty, 2);
    EXPECT_EQ(book.quantity_at(Side::Sell, 100), 8);
}

TEST_F(OrderBookTest, TimePriorityWithinALevelIsFifo) {
    const OrderId a = sell(100, 5);
    const OrderId b = sell(100, 5);
    const OrderId c = sell(100, 5);

    book.add_limit(Side::Buy, 100, 15, trades);

    ASSERT_EQ(trades.size(), 3u);
    EXPECT_EQ(trades[0].maker_id, a);
    EXPECT_EQ(trades[1].maker_id, b);
    EXPECT_EQ(trades[2].maker_id, c);
}

TEST_F(OrderBookTest, BetterPriceBeatsEarlierTime) {
    const OrderId early_worse = sell(101, 5);
    const OrderId late_better = sell(100, 5);

    book.add_limit(Side::Buy, 101, 10, trades);

    ASSERT_EQ(trades.size(), 2u);
    EXPECT_EQ(trades[0].maker_id, late_better);
    EXPECT_EQ(trades[0].price, 100);
    EXPECT_EQ(trades[1].maker_id, early_worse);
    EXPECT_EQ(trades[1].price, 101);
}

TEST_F(OrderBookTest, LimitOrderSweepsLevelsButStopsAtItsLimit) {
    sell(100, 5);
    sell(101, 5);
    sell(106, 5);

    const OrderResult r = book.add_limit(Side::Buy, 105, 12, trades);

    ASSERT_EQ(trades.size(), 2u);
    EXPECT_EQ(trades[0].price, 100);
    EXPECT_EQ(trades[1].price, 101);
    EXPECT_EQ(r.filled, 10);
    EXPECT_EQ(r.resting, 2);
    EXPECT_EQ(book.best_bid(), 105);   // remainder rests at the limit price
    EXPECT_EQ(book.best_ask(), 106);   // untouched: 106 is worse than the 105 limit
}

TEST_F(OrderBookTest, SellSweepsBidsFromHighestDown) {
    buy(100, 5);
    buy(99, 5);
    buy(98, 5);

    const OrderResult r = book.add_limit(Side::Sell, 99, 20, trades);

    ASSERT_EQ(trades.size(), 2u);
    EXPECT_EQ(trades[0].price, 100);
    EXPECT_EQ(trades[1].price, 99);
    EXPECT_EQ(r.filled, 10);
    EXPECT_EQ(r.resting, 10);
    EXPECT_EQ(book.best_ask(), 99);
    EXPECT_EQ(book.best_bid(), 98);
}

TEST_F(OrderBookTest, BookIsNeverLeftCrossed) {
    buy(100, 5);
    sell(100, 8);   // fills 5, rests 3 at 100

    EXPECT_EQ(book.best_bid(), std::nullopt);
    EXPECT_EQ(book.best_ask(), 100);
}

// --- Market orders ---------------------------------------------------------

TEST_F(OrderBookTest, MarketOrderSweepsAcrossPrices) {
    sell(100, 5);
    sell(110, 5);
    sell(500, 5);

    const OrderResult r = book.add_market(Side::Buy, 12, trades);

    ASSERT_EQ(trades.size(), 3u);
    EXPECT_EQ(trades[0].price, 100);
    EXPECT_EQ(trades[1].price, 110);
    EXPECT_EQ(trades[2].price, 500);
    EXPECT_EQ(trades[2].qty, 2);
    EXPECT_EQ(r.filled, 12);
    EXPECT_EQ(r.resting, 0);
    EXPECT_EQ(book.quantity_at(Side::Sell, 500), 3);
}

TEST_F(OrderBookTest, MarketOrderRemainderIsCancelledNotRested) {
    sell(100, 5);

    const OrderResult r = book.add_market(Side::Buy, 8, trades);

    EXPECT_EQ(r.filled, 5);
    EXPECT_EQ(r.resting, 0);
    EXPECT_TRUE(book.empty());                  // nothing rests on either side
    EXPECT_EQ(book.best_bid(), std::nullopt);
    EXPECT_FALSE(book.cancel(r.id));            // there is nothing left to cancel
}

TEST_F(OrderBookTest, MarketOrderOnEmptyBookDoesNothing) {
    const OrderResult r = book.add_market(Side::Sell, 10, trades);

    EXPECT_NE(r.id, 0u);                        // accepted, just unfilled
    EXPECT_EQ(r.reject, RejectReason::None);
    EXPECT_EQ(r.filled, 0);
    EXPECT_TRUE(trades.empty());
    EXPECT_TRUE(book.empty());
}

TEST_F(OrderBookTest, MarketSellHitsBids) {
    buy(100, 5);
    buy(90, 5);

    const OrderResult r = book.add_market(Side::Sell, 7, trades);

    ASSERT_EQ(trades.size(), 2u);
    EXPECT_EQ(trades[0].price, 100);
    EXPECT_EQ(trades[1].price, 90);
    EXPECT_EQ(trades[1].taker_side, Side::Sell);
    EXPECT_EQ(r.filled, 7);
    EXPECT_EQ(book.quantity_at(Side::Buy, 90), 3);
}

// --- Cancel ----------------------------------------------------------------

TEST_F(OrderBookTest, CancelRemovesOrderAndItsLevel) {
    const OrderId id = buy(100, 10);

    EXPECT_TRUE(book.cancel(id));
    EXPECT_TRUE(book.empty());
    EXPECT_EQ(book.bid_level_count(), 0u);
    EXPECT_EQ(book.best_bid(), std::nullopt);
}

TEST_F(OrderBookTest, CancelUnknownIdFails) {
    buy(100, 10);
    EXPECT_FALSE(book.cancel(999));
    EXPECT_FALSE(book.cancel(0));
    EXPECT_EQ(book.order_count(), 1u);
}

TEST_F(OrderBookTest, CancelTwiceFailsTheSecondTime) {
    const OrderId id = buy(100, 10);
    EXPECT_TRUE(book.cancel(id));
    EXPECT_FALSE(book.cancel(id));
}

TEST_F(OrderBookTest, CancelAfterFullFillFails) {
    const OrderId maker = sell(100, 10);
    book.add_market(Side::Buy, 10, trades);
    EXPECT_FALSE(book.cancel(maker));
}

TEST_F(OrderBookTest, CancelFromMiddleOfQueuePreservesFifo) {
    const OrderId a = sell(100, 5);
    const OrderId b = sell(100, 5);
    const OrderId c = sell(100, 5);

    EXPECT_TRUE(book.cancel(b));
    EXPECT_EQ(book.quantity_at(Side::Sell, 100), 10);

    book.add_market(Side::Buy, 10, trades);
    ASSERT_EQ(trades.size(), 2u);
    EXPECT_EQ(trades[0].maker_id, a);
    EXPECT_EQ(trades[1].maker_id, c);
}

TEST_F(OrderBookTest, CancelPartiallyFilledOrderRemovesOnlyTheRemainder) {
    const OrderId maker = sell(100, 10);
    sell(100, 7);
    book.add_market(Side::Buy, 4, trades);   // maker has 6 left

    EXPECT_TRUE(book.cancel(maker));
    EXPECT_EQ(book.quantity_at(Side::Sell, 100), 7);
    EXPECT_EQ(book.order_count(), 1u);
}

TEST_F(OrderBookTest, CancelBestLevelExposesTheNextOne) {
    buy(99, 5);
    const OrderId best = buy(100, 5);

    EXPECT_TRUE(book.cancel(best));
    EXPECT_EQ(book.best_bid(), 99);
}

// --- Validation ------------------------------------------------------------

TEST_F(OrderBookTest, RejectsNonPositiveQuantity) {
    EXPECT_EQ(book.add_limit(Side::Buy, 100, 0, trades).reject, RejectReason::InvalidQuantity);
    EXPECT_EQ(book.add_limit(Side::Buy, 100, -5, trades).reject, RejectReason::InvalidQuantity);
    EXPECT_EQ(book.add_market(Side::Sell, 0, trades).reject, RejectReason::InvalidQuantity);
    EXPECT_TRUE(book.empty());
}

TEST_F(OrderBookTest, RejectsNonPositiveLimitPrice) {
    EXPECT_EQ(book.add_limit(Side::Buy, 0, 10, trades).reject, RejectReason::InvalidPrice);
    EXPECT_EQ(book.add_limit(Side::Sell, -1, 10, trades).reject, RejectReason::InvalidPrice);
    EXPECT_TRUE(book.empty());
}

TEST_F(OrderBookTest, RejectedOrdersDoNotConsumeAnId) {
    const OrderResult rejected = book.add_limit(Side::Buy, 100, 0, trades);
    EXPECT_EQ(rejected.id, 0u);
    EXPECT_EQ(buy(100, 1), 1u);
}

// --- Trade buffer and depth ------------------------------------------------

TEST_F(OrderBookTest, TradesAreAppendedToTheCallersBuffer) {
    sell(100, 1);
    sell(100, 1);

    book.add_market(Side::Buy, 1, trades);
    ASSERT_EQ(trades.size(), 1u);

    // The engine never clears the buffer: the second trade lands after the first.
    book.add_market(Side::Buy, 1, trades);
    ASSERT_EQ(trades.size(), 2u);
    EXPECT_EQ(trades[0].maker_id, 1u);
    EXPECT_EQ(trades[1].maker_id, 2u);
}

TEST_F(OrderBookTest, DepthListsLevelsFromBestOutwards) {
    buy(100, 5);
    buy(100, 3);
    buy(99, 7);
    buy(98, 1);
    sell(101, 2);
    sell(103, 4);

    const std::vector<LevelView> bids = book.depth(Side::Buy, 2);
    ASSERT_EQ(bids.size(), 2u);   // capped at max_levels
    EXPECT_EQ(bids[0], (LevelView{.price = 100, .qty = 8, .order_count = 2}));
    EXPECT_EQ(bids[1], (LevelView{.price = 99, .qty = 7, .order_count = 1}));

    const std::vector<LevelView> asks = book.depth(Side::Sell, 10);
    ASSERT_EQ(asks.size(), 2u);
    EXPECT_EQ(asks[0], (LevelView{.price = 101, .qty = 2, .order_count = 1}));
    EXPECT_EQ(asks[1], (LevelView{.price = 103, .qty = 4, .order_count = 1}));
}

}  // namespace
