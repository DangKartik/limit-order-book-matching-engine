// A single-instrument limit order book with price-time priority matching.
// Single-threaded, no I/O.
//
// The matching logic is written once, as a class template, and compiled against
// a choice of data structures:
//
//                      queue within a level        price levels
//   MapListBook        std::list                   std::map         <- baseline
//   MapPoolBook        intrusive list + pool       std::map
//   FlatListBook       std::list                   flat array
//   FlatPoolBook       intrusive list + pool       flat array       <- both optimisations
//
// All four behave identically (the tests run every scenario against each one),
// so the benchmarks can attribute a speed difference to one data structure.
//
// This is a header-only library: a template's code must be visible wherever it
// is instantiated, so the member functions are defined in the class body here
// rather than in a .cpp file. The standard library is built the same way.
#pragma once

#include <algorithm>
#include <cassert>
#include <cstddef>
#include <limits>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <vector>

#include "lob/order_queue.hpp"
#include "lob/price_levels.hpp"
#include "lob/types.hpp"

namespace lob {

// `LevelsT` is a template template parameter: the argument is itself a template
// (MapLevels or FlatLevels), not a finished type. The book finishes it twice,
// once per side, below. See DESIGN_NOTES #15.
template <typename Queue, template <typename, Side> class LevelsT>
class BasicOrderBook {
public:
    // `explicit` stops the compiler from silently converting a BookConfig into
    // an OrderBook where a book is expected.
    explicit BasicOrderBook(BookConfig config = {})
        : config_(validated(config)),
          bids_(config.min_price, config.max_price),
          asks_(config.min_price, config.max_price) {}

    // Non-copyable and non-movable. `index_` stores handles that point into
    // storage owned by THIS object; a compiler-generated copy would duplicate
    // the storage but leave the copied handles pointing into the original
    // book. Deleting these turns that bug into a compile error. See DESIGN_NOTES #8.
    BasicOrderBook(const BasicOrderBook&) = delete;
    BasicOrderBook& operator=(const BasicOrderBook&) = delete;
    BasicOrderBook(BasicOrderBook&&) = delete;
    BasicOrderBook& operator=(BasicOrderBook&&) = delete;

    // Submit a limit order. It first trades against the opposite side at prices
    // equal to or better than `price`; any remainder rests in the book.
    //
    // Trades are APPENDED to `trades`; the buffer is owned by the caller and is
    // never cleared here, so the caller can reuse one vector for the whole
    // session and avoid a heap allocation per order. See DESIGN_NOTES #6.
    OrderResult add_limit(Side side, Price price, Quantity qty, std::vector<Trade>& trades) {
        if (price < config_.min_price || price > config_.max_price) {
            return OrderResult{.reject = RejectReason::InvalidPrice};
        }
        return submit(side, price, qty, /*rest_remainder=*/true, trades);
    }

    // Submit a market order. It trades against the opposite side at any price
    // until filled or the book is empty; any remainder is cancelled. A market
    // order never rests.
    OrderResult add_market(Side side, Quantity qty, std::vector<Trade>& trades) {
        // A market order is matched as a limit order with the most aggressive
        // possible price, so it crosses every level. This keeps one matching
        // loop with no "is this a market order?" branch. See DESIGN_NOTES #10.
        const Price limit = (side == Side::Buy) ? std::numeric_limits<Price>::max()
                                                : std::numeric_limits<Price>::min();
        return submit(side, limit, qty, /*rest_remainder=*/false, trades);
    }

    // Cancel a resting order. Returns false if the id is unknown, already fully
    // filled, or already cancelled.
    bool cancel(OrderId id) {
        const auto index_it = index_.find(id);
        if (index_it == index_.end()) {
            return false;
        }
        const Handle handle = index_it->second;
        // Copy the side out before remove_resting() destroys the order.
        const Side side = Queue::order(handle).side;
        if (side == Side::Buy) {
            remove_resting(bids_, handle);
        } else {
            remove_resting(asks_, handle);
        }
        index_.erase(index_it);
        return true;
    }

    // --- Read-only queries -------------------------------------------------
    // [[nodiscard]] makes the compiler warn if a caller ignores the result.
    // std::optional<T> is "a T or nothing" (like Java's Optional / Python's
    // None) without a heap allocation and without a magic sentinel price.
    [[nodiscard]] std::optional<Price> best_bid() const { return best_price(bids_); }
    [[nodiscard]] std::optional<Price> best_ask() const { return best_price(asks_); }

    // Total resting quantity at one price on one side (0 if no such level).
    [[nodiscard]] Quantity quantity_at(Side side, Price price) const {
        return side == Side::Buy ? bids_.quantity_at(price) : asks_.quantity_at(price);
    }

    // Up to `max_levels` levels from the best price outwards. Allocates a
    // vector, so it is for snapshots and tests, not for the matching hot path.
    [[nodiscard]] std::vector<LevelView> depth(Side side, std::size_t max_levels) const {
        std::vector<LevelView> out;
        // A generic lambda (`const auto&` parameters) is a local template.
        const auto append = [&out](Price price, const auto& level) {
            out.push_back(LevelView{.price = price,
                                    .qty = level.total_qty,
                                    .order_count = level.queue.size()});
        };
        if (side == Side::Buy) {
            out.reserve(std::min(max_levels, bids_.size()));
            bids_.for_each(max_levels, append);
        } else {
            out.reserve(std::min(max_levels, asks_.size()));
            asks_.for_each(max_levels, append);
        }
        return out;   // moved or elided, not copied (DESIGN_NOTES A4)
    }

    [[nodiscard]] std::size_t order_count() const { return index_.size(); }
    [[nodiscard]] std::size_t bid_level_count() const { return bids_.size(); }
    [[nodiscard]] std::size_t ask_level_count() const { return asks_.size(); }
    [[nodiscard]] bool empty() const { return index_.empty(); }
    [[nodiscard]] const BookConfig& config() const { return config_; }

private:
    // Bids and asks are two different types (different price ordering baked in
    // at compile time), which is why the helpers below are templates.
    using Bids = LevelsT<Queue, Side::Buy>;
    using Asks = LevelsT<Queue, Side::Sell>;
    using Handle = typename Queue::Handle;

    static BookConfig validated(BookConfig config) {
        // Throwing is fine here: construction is not on the hot path, and a
        // book with an inverted or non-positive range cannot work at all.
        if (config.min_price < 1 || config.max_price < config.min_price) {
            throw std::invalid_argument("BookConfig: need 1 <= min_price <= max_price");
        }
        return config;
    }

    // Would a taker with this limit trade against a resting level at `level_price`?
    // A buyer accepts any ask at or below their limit; a seller any bid at or above.
    static bool crosses(Side taker_side, Price limit, Price level_price) {
        return taker_side == Side::Buy ? level_price <= limit : level_price >= limit;
    }

    OrderResult submit(Side side, Price limit, Quantity qty, bool rest_remainder,
                       std::vector<Trade>& trades) {
        if (qty <= 0) {
            // Rejected orders do not consume an id.
            return OrderResult{.reject = RejectReason::InvalidQuantity};
        }

        const OrderId id = next_id_++;

        // A buy matches against asks, a sell against bids.
        const Quantity remaining = (side == Side::Buy)
                                       ? match(asks_, id, side, limit, qty, trades)
                                       : match(bids_, id, side, limit, qty, trades);

        OrderResult result{.id = id, .filled = qty - remaining};

        if (remaining > 0 && rest_remainder) {
            const Order order{.id = id, .price = limit, .qty = remaining, .side = side};
            if (side == Side::Buy) {
                rest(bids_, order);
            } else {
                rest(asks_, order);
            }
            result.resting = remaining;
        }
        // Otherwise (a market order with an unfilled remainder) the remainder is
        // simply dropped: nothing was ever stored for it, so there is nothing to undo.
        return result;
    }

    // Trade `remaining` against the opposite side, best price first and oldest
    // order first within a price (price-time priority). Returns what is left.
    template <typename Levels>
    Quantity match(Levels& opposite, OrderId taker_id, Side taker_side, Price limit,
                   Quantity remaining, std::vector<Trade>& trades) {
        while (remaining > 0 && !opposite.empty()) {
            const auto cursor = opposite.best();
            const Price level_price = opposite.price_of(cursor);
            if (!crosses(taker_side, limit, level_price)) {
                break;   // best price is not good enough, so no worse price can be either
            }

            // A reference, not a copy: `auto level = ...` would try to copy the queue.
            auto& level = opposite.level_of(cursor);

            while (remaining > 0 && !level.queue.empty()) {
                Order& maker = level.queue.front();   // oldest order at this price
                const Quantity fill = std::min(remaining, maker.qty);

                trades.push_back(Trade{.maker_id = maker.id,
                                       .taker_id = taker_id,
                                       .price = level_price,
                                       .qty = fill,
                                       .taker_side = taker_side});

                remaining -= fill;
                maker.qty -= fill;
                level.total_qty -= fill;

                if (maker.qty == 0) {
                    // Order matters: pop_front() destroys (or recycles) the
                    // Order that `maker` refers to, so read maker.id BEFORE
                    // popping. Using `maker` afterwards would be a dangling
                    // reference: undefined behaviour, with no compiler warning.
                    index_.erase(maker.id);
                    level.queue.pop_front(storage_);
                }
                // A maker that was only partly filled stays at the front: a
                // partial fill does not cost an order its time priority.
            }

            if (level.queue.empty()) {
                // Empty levels are retired immediately so that best() is always
                // a level with real liquidity. `cursor` and `level` are not
                // used again before the next iteration re-reads best().
                opposite.erase(cursor);
            }
        }
        return remaining;
    }

    // Add an order to the back of the queue at its price.
    template <typename Levels>
    void rest(Levels& own_side, const Order& order) {
        const auto cursor = own_side.get_or_create(order.price);
        auto& level = own_side.level_of(cursor);
        const Handle handle = level.queue.push_back(storage_, order);
        level.total_qty += order.qty;

        // The handle stays valid until that order itself is removed, whatever
        // else happens to the queue (DESIGN_NOTES A3).
        const bool inserted = index_.emplace(order.id, handle).second;
        // assert() is checked in Debug builds and compiled out in Release
        // (when NDEBUG is defined), so it documents invariants at no cost.
        assert(inserted && "order ids are unique");
        (void)inserted;   // silences the unused-variable warning in Release
    }

    // Unlink a resting order from its level, retiring the level if it empties.
    template <typename Levels>
    void remove_resting(Levels& own_side, Handle handle) {
        const Order& order = Queue::order(handle);
        const auto cursor = own_side.find(order.price);
        auto& level = own_side.level_of(cursor);
        level.total_qty -= order.qty;
        level.queue.erase(storage_, handle);   // `order` and `handle` are dead from here on
        if (level.queue.empty()) {
            own_side.erase(cursor);
        }
    }

    template <typename Levels>
    static std::optional<Price> best_price(const Levels& levels) {
        if (levels.empty()) {
            return std::nullopt;
        }
        // best() is non-const because it hands out a cursor that can modify the
        // level. const_cast removes const from our reference; that is safe only
        // because the object is not truly const and we do nothing but read a price.
        auto& mutable_levels = const_cast<Levels&>(levels);
        return mutable_levels.price_of(mutable_levels.best());
    }

    BookConfig config_;

    // Book-wide storage for the queues (the order pool, for IntrusiveQueue).
    // Members are destroyed in reverse order of declaration, so declaring this
    // before the levels means it outlives them.
    typename Queue::Storage storage_;

    Bids bids_;
    Asks asks_;

    // Order id -> handle to that order inside its level's queue. Average O(1)
    // lookup: this is what makes cancel fast. See DESIGN_NOTES #5.
    std::unordered_map<OrderId, Handle> index_;

    OrderId next_id_ = 1;
};

using MapListBook = BasicOrderBook<ListQueue, MapLevels>;        // baseline
using MapPoolBook = BasicOrderBook<IntrusiveQueue, MapLevels>;   // + intrusive list over a pool
using FlatListBook = BasicOrderBook<ListQueue, FlatLevels>;      // + flat array of levels
using FlatPoolBook = BasicOrderBook<IntrusiveQueue, FlatLevels>; // both optimisations

// The default book. Kept as the baseline until the benchmarks say otherwise.
using OrderBook = MapListBook;

}  // namespace lob
