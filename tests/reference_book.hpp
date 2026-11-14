// A deliberately naive order book used only as a test oracle.
//
// It keeps every resting order in one flat vector in arrival order and finds
// the best match by scanning all of them. That is far too slow for real use
// (O(n) per fill) but it is short enough to be checked by eye, and it shares
// no data structures or tricks with the real engine. If both agree on every
// trade over a long random order stream, a bug would have to exist in both,
// in the same way, to go unnoticed.
#pragma once

#include <cstddef>
#include <map>
#include <optional>
#include <vector>

#include "lob/types.hpp"

namespace lob::testing {

class ReferenceBook {
public:
    OrderResult add_limit(Side side, Price price, Quantity qty, std::vector<Trade>& trades) {
        if (price <= 0) {
            return OrderResult{.reject = RejectReason::InvalidPrice};
        }
        return submit(side, price, qty, /*is_market=*/false, trades);
    }

    OrderResult add_market(Side side, Quantity qty, std::vector<Trade>& trades) {
        return submit(side, 0, qty, /*is_market=*/true, trades);
    }

    bool cancel(OrderId id) {
        for (std::size_t i = 0; i < orders_.size(); ++i) {
            if (orders_[i].id == id) {
                erase_at(i);
                return true;
            }
        }
        return false;
    }

    std::optional<Price> best_bid() const { return best_price(Side::Buy); }
    std::optional<Price> best_ask() const { return best_price(Side::Sell); }
    std::size_t order_count() const { return orders_.size(); }

    std::vector<LevelView> depth(Side side, std::size_t max_levels) const {
        std::map<Price, LevelView> levels;   // ascending by price
        for (const RefOrder& o : orders_) {
            if (o.side != side) {
                continue;
            }
            LevelView& level = levels[o.price];
            level.price = o.price;
            level.qty += o.qty;
            level.order_count += 1;
        }
        std::vector<LevelView> out;
        if (side == Side::Sell) {
            for (auto it = levels.begin(); it != levels.end() && out.size() < max_levels; ++it) {
                out.push_back(it->second);
            }
        } else {
            for (auto it = levels.rbegin(); it != levels.rend() && out.size() < max_levels; ++it) {
                out.push_back(it->second);
            }
        }
        return out;
    }

private:
    struct RefOrder {
        OrderId id;
        Side side;
        Price price;
        Quantity qty;
    };

    // True if resting price `a` is more attractive to a taker than `b`.
    static bool better(Side resting_side, Price a, Price b) {
        return resting_side == Side::Sell ? a < b : a > b;
    }

    OrderResult submit(Side side, Price limit, Quantity qty, bool is_market,
                       std::vector<Trade>& trades) {
        if (qty <= 0) {
            return OrderResult{.reject = RejectReason::InvalidQuantity};
        }
        const OrderId id = next_id_++;
        const Side resting_side = (side == Side::Buy) ? Side::Sell : Side::Buy;
        Quantity remaining = qty;

        while (remaining > 0) {
            // Scan for the best-priced opposite order. Using a strict `better`
            // means that among equal prices the first one found wins, and the
            // vector is in arrival order, so that is the oldest: time priority.
            std::optional<std::size_t> best;
            for (std::size_t i = 0; i < orders_.size(); ++i) {
                const RefOrder& o = orders_[i];
                if (o.side != resting_side) {
                    continue;
                }
                const bool acceptable =
                    is_market || (side == Side::Buy ? o.price <= limit : o.price >= limit);
                if (!acceptable) {
                    continue;
                }
                if (!best || better(resting_side, o.price, orders_[*best].price)) {
                    best = i;
                }
            }
            if (!best) {
                break;
            }

            RefOrder& maker = orders_[*best];
            const Quantity fill = remaining < maker.qty ? remaining : maker.qty;
            trades.push_back(Trade{.maker_id = maker.id,
                                   .taker_id = id,
                                   .price = maker.price,
                                   .qty = fill,
                                   .taker_side = side});
            remaining -= fill;
            maker.qty -= fill;
            if (maker.qty == 0) {
                erase_at(*best);
            }
        }

        OrderResult result{.id = id, .filled = qty - remaining};
        if (remaining > 0 && !is_market) {
            orders_.push_back(RefOrder{.id = id, .side = side, .price = limit, .qty = remaining});
            result.resting = remaining;
        }
        return result;
    }

    std::optional<Price> best_price(Side side) const {
        std::optional<Price> best;
        for (const RefOrder& o : orders_) {
            if (o.side == side && (!best || better(side, o.price, *best))) {
                best = o.price;
            }
        }
        return best;
    }

    // vector::erase shifts later elements down, preserving arrival order.
    void erase_at(std::size_t i) {
        orders_.erase(orders_.begin() + static_cast<std::ptrdiff_t>(i));
    }

    std::vector<RefOrder> orders_;
    OrderId next_id_ = 1;
};

}  // namespace lob::testing
