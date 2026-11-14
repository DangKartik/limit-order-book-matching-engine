// A single-instrument limit order book with price-time priority matching.
//
// Baseline implementation: std::map of price levels, std::list of orders per
// level, std::unordered_map for cancel lookup. Single-threaded, no I/O.
#pragma once

#include <cstddef>
#include <functional>
#include <list>
#include <map>
#include <optional>
#include <unordered_map>
#include <vector>

#include "lob/types.hpp"

namespace lob {

class OrderBook {
public:
    OrderBook() = default;

    // Non-copyable and non-movable. `index_` stores iterators that point into
    // the lists owned by THIS object; a compiler-generated copy would duplicate
    // the lists but leave the copied iterators pointing into the original book.
    // Deleting these turns that bug into a compile error. See DESIGN_NOTES #8.
    OrderBook(const OrderBook&) = delete;
    OrderBook& operator=(const OrderBook&) = delete;
    OrderBook(OrderBook&&) = delete;
    OrderBook& operator=(OrderBook&&) = delete;

    // Submit a limit order. It first trades against the opposite side at prices
    // equal to or better than `price`; any remainder rests in the book.
    //
    // Trades are APPENDED to `trades`; the buffer is owned by the caller and is
    // never cleared here, so the caller can reuse one vector for the whole
    // session and avoid a heap allocation per order. See DESIGN_NOTES #6.
    OrderResult add_limit(Side side, Price price, Quantity qty, std::vector<Trade>& trades);

    // Submit a market order. It trades against the opposite side at any price
    // until filled or the book is empty; any remainder is cancelled. A market
    // order never rests.
    OrderResult add_market(Side side, Quantity qty, std::vector<Trade>& trades);

    // Cancel a resting order. Returns false if the id is unknown, already fully
    // filled, or already cancelled.
    bool cancel(OrderId id);

    // --- Read-only queries -------------------------------------------------
    // [[nodiscard]] makes the compiler warn if a caller ignores the result.
    // std::optional<T> is "a T or nothing" (like Java's Optional / Python's
    // None) without a heap allocation and without a magic sentinel price.
    [[nodiscard]] std::optional<Price> best_bid() const;
    [[nodiscard]] std::optional<Price> best_ask() const;

    // Total resting quantity at one price on one side (0 if no such level).
    [[nodiscard]] Quantity quantity_at(Side side, Price price) const;

    // Up to `max_levels` levels from the best price outwards. Allocates a
    // vector, so it is for snapshots and tests, not for the matching hot path.
    [[nodiscard]] std::vector<LevelView> depth(Side side, std::size_t max_levels) const;

    [[nodiscard]] std::size_t order_count() const { return index_.size(); }
    [[nodiscard]] std::size_t bid_level_count() const { return bids_.size(); }
    [[nodiscard]] std::size_t ask_level_count() const { return asks_.size(); }
    [[nodiscard]] bool empty() const { return index_.empty(); }

private:
    struct Order {
        OrderId id;
        Price price;
        Quantity qty;   // remaining (unfilled) quantity
        Side side;
    };

    // All orders resting at one price, oldest first.
    struct Level {
        // std::list is a doubly linked list. Two properties matter here:
        //  1. erase() of any element is O(1) given its iterator (cancel).
        //  2. Iterators to other elements stay valid when you insert or erase,
        //     which is what lets `index_` hold iterators long-term.
        // std::vector and std::deque offer neither. See DESIGN_NOTES #4.
        std::list<Order> orders;
        Quantity total_qty = 0;   // cached sum of orders[i].qty, so depth queries are O(1) per level
    };

    using OrderIt = std::list<Order>::iterator;

    // std::map is a balanced binary search tree (red-black) that keeps its keys
    // sorted, so begin() is always the best price. The third template argument
    // is the ordering: bids sort DESCENDING (highest bid is best), asks use the
    // default ascending order (lowest ask is best). Because the comparator is
    // part of the type, BidMap and AskMap are two different types; the matching
    // code is a template so it can be written once for both. See DESIGN_NOTES #3.
    using BidMap = std::map<Price, Level, std::greater<Price>>;
    using AskMap = std::map<Price, Level>;

    // These member templates are declared here but defined in order_book.cpp.
    // That is normally a link error for templates, but it is fine when every
    // use is inside that same .cpp file, as is the case for private helpers.
    template <typename SideMap>
    Quantity match(SideMap& opposite, OrderId taker_id, Side taker_side, Price limit,
                   Quantity remaining, std::vector<Trade>& trades);

    template <typename SideMap>
    void rest(SideMap& own_side, const Order& order);

    template <typename SideMap>
    void remove_resting(SideMap& own_side, OrderIt order_it);

    OrderResult submit(Side side, Price limit, Quantity qty, bool rest_remainder,
                       std::vector<Trade>& trades);

    BidMap bids_;
    AskMap asks_;

    // Order id -> position of that order inside its level's list. Hash map, so
    // average O(1) lookup: this is what makes cancel fast. See DESIGN_NOTES #5.
    std::unordered_map<OrderId, OrderIt> index_;

    OrderId next_id_ = 1;
};

}  // namespace lob
