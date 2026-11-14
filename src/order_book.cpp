#include "lob/order_book.hpp"

#include <algorithm>
#include <cassert>
#include <iterator>
#include <limits>

namespace lob {

namespace {   // anonymous namespace: these helpers are private to this .cpp file

// Would a taker with this limit price trade against a resting level at `level_price`?
// A buyer accepts any ask at or below their limit; a seller any bid at or above.
bool crosses(Side taker_side, Price limit, Price level_price) {
    return taker_side == Side::Buy ? level_price <= limit : level_price >= limit;
}

// A market order is matched as a limit order with the most aggressive possible
// price, so it crosses every level. This keeps a single matching loop with no
// "is this a market order?" branch inside it. See DESIGN_NOTES #10.
Price market_limit(Side side) {
    return side == Side::Buy ? std::numeric_limits<Price>::max()
                             : std::numeric_limits<Price>::min();
}

}  // namespace

OrderResult OrderBook::add_limit(Side side, Price price, Quantity qty, std::vector<Trade>& trades) {
    if (price <= 0) {
        return OrderResult{.reject = RejectReason::InvalidPrice};
    }
    return submit(side, price, qty, /*rest_remainder=*/true, trades);
}

OrderResult OrderBook::add_market(Side side, Quantity qty, std::vector<Trade>& trades) {
    return submit(side, market_limit(side), qty, /*rest_remainder=*/false, trades);
}

OrderResult OrderBook::submit(Side side, Price limit, Quantity qty, bool rest_remainder,
                              std::vector<Trade>& trades) {
    if (qty <= 0) {
        // Rejected orders do not consume an id.
        return OrderResult{.reject = RejectReason::InvalidQuantity};
    }

    const OrderId id = next_id_++;

    // A buy matches against asks, a sell against bids. The two calls look the
    // same but instantiate match() for two different map types.
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
    // Otherwise (market order with an unfilled remainder) the remainder is
    // simply dropped: nothing was ever stored for it, so there is nothing to undo.
    return result;
}

// Trade `remaining` against the opposite side of the book, best price first and
// oldest order first within a price (price-time priority). Returns what is left.
template <typename SideMap>
Quantity OrderBook::match(SideMap& opposite, OrderId taker_id, Side taker_side, Price limit,
                          Quantity remaining, std::vector<Trade>& trades) {
    while (remaining > 0 && !opposite.empty()) {
        // begin() is the best price on this side because of the map's ordering.
        const auto level_it = opposite.begin();
        const Price level_price = level_it->first;
        if (!crosses(taker_side, limit, level_price)) {
            break;   // best price is not good enough, so no worse price can be either
        }

        // A reference, not a copy: `Level level = ...` would copy the whole list.
        Level& level = level_it->second;

        while (remaining > 0 && !level.orders.empty()) {
            Order& maker = level.orders.front();   // oldest order at this price
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
                // Order matters: pop_front() destroys the Order that `maker`
                // refers to, so read maker.id BEFORE popping. Using `maker`
                // after the pop would be a dangling reference: undefined
                // behaviour, and the compiler will not warn about it.
                index_.erase(maker.id);
                level.orders.pop_front();
            }
            // If the maker was only partly filled it stays at the front of the
            // queue: a partial fill does not cost an order its time priority.
        }

        if (level.orders.empty()) {
            // Empty levels are removed immediately so that begin() is always a
            // level with real liquidity. erase() invalidates `level_it` and
            // `level`; neither is used again before the next loop iteration
            // re-reads begin().
            opposite.erase(level_it);
        }
    }
    return remaining;
}

// Add an order to the back of the queue at its price, creating the level if needed.
template <typename SideMap>
void OrderBook::rest(SideMap& own_side, const Order& order) {
    // operator[] on a map inserts a default-constructed Level if the key is
    // absent (unlike Java's get() returning null or Python raising KeyError).
    // Here that is exactly the behaviour we want.
    Level& level = own_side[order.price];
    level.orders.push_back(order);
    level.total_qty += order.qty;

    // std::prev(end()) is an iterator to the element just pushed. It stays
    // valid until that element itself is erased, whatever else happens to the list.
    const auto [it, inserted] = index_.emplace(order.id, std::prev(level.orders.end()));
    (void)it;
    // assert() is checked in Debug builds and compiled out entirely in Release
    // (when NDEBUG is defined), so it documents invariants at no runtime cost.
    assert(inserted && "order ids are unique");
    (void)inserted;   // silences the unused-variable warning in Release builds
}

// Unlink a resting order from its level, removing the level if it becomes empty.
template <typename SideMap>
void OrderBook::remove_resting(SideMap& own_side, OrderIt order_it) {
    const auto level_it = own_side.find(order_it->price);   // O(log levels)
    assert(level_it != own_side.end() && "a resting order always has a level");

    Level& level = level_it->second;
    level.total_qty -= order_it->qty;
    level.orders.erase(order_it);   // O(1); `order_it` is invalid from here on

    if (level.orders.empty()) {
        own_side.erase(level_it);
    }
}

bool OrderBook::cancel(OrderId id) {
    const auto index_it = index_.find(id);
    if (index_it == index_.end()) {
        return false;   // never existed, already filled, or already cancelled
    }

    const OrderIt order_it = index_it->second;
    // Copy the side out before remove_resting() destroys the order.
    const Side side = order_it->side;
    if (side == Side::Buy) {
        remove_resting(bids_, order_it);
    } else {
        remove_resting(asks_, order_it);
    }
    index_.erase(index_it);
    return true;
}

std::optional<Price> OrderBook::best_bid() const {
    if (bids_.empty()) {
        return std::nullopt;
    }
    return bids_.begin()->first;
}

std::optional<Price> OrderBook::best_ask() const {
    if (asks_.empty()) {
        return std::nullopt;
    }
    return asks_.begin()->first;
}

Quantity OrderBook::quantity_at(Side side, Price price) const {
    // find(), not operator[]: operator[] would insert an empty level as a side
    // effect (and does not even compile on a const map).
    if (side == Side::Buy) {
        const auto it = bids_.find(price);
        return it == bids_.end() ? 0 : it->second.total_qty;
    }
    const auto it = asks_.find(price);
    return it == asks_.end() ? 0 : it->second.total_qty;
}

std::vector<LevelView> OrderBook::depth(Side side, std::size_t max_levels) const {
    std::vector<LevelView> out;

    // A generic lambda (`const auto&` parameter) is a local template: it lets
    // one piece of code walk either map type.
    const auto collect = [&out, max_levels](const auto& side_map) {
        out.reserve(std::min(max_levels, side_map.size()));
        for (const auto& [price, level] : side_map) {   // structured binding, best price first
            if (out.size() == max_levels) {
                break;
            }
            // std::list::size() is O(1) since C++11.
            out.push_back(LevelView{.price = price,
                                    .qty = level.total_qty,
                                    .order_count = level.orders.size()});
        }
    };

    if (side == Side::Buy) {
        collect(bids_);
    } else {
        collect(asks_);
    }
    return out;   // moved or elided, not copied (see "move semantics" in DESIGN_NOTES)
}

}  // namespace lob
