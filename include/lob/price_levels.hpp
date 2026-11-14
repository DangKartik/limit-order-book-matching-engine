// One side of the book (all bid levels, or all ask levels), in two
// interchangeable implementations:
//
//   MapLevels   baseline      std::map keyed by price (a balanced tree)
//   FlatLevels  optimisation  std::vector indexed directly by tick
//
// Both expose the same member functions. A "Cursor" is whatever the container
// uses to refer to one level cheaply: a map iterator for MapLevels, an array
// index for FlatLevels. The book asks for a cursor once and then uses it for
// every follow-up step, so neither implementation repeats a lookup.
#pragma once

#include <algorithm>
#include <cstddef>
#include <map>
#include <vector>

#include "lob/types.hpp"

namespace lob {

// All orders resting at one price.
template <typename Queue>
struct Level {
    Queue queue;              // oldest order first
    Quantity total_qty = 0;   // cached sum of the remaining quantities in `queue`
};

// Orders prices so that the BEST price for a side comes first: highest for
// bids, lowest for asks. `kSide` is a non-type template parameter: a value,
// not a type, fixed at compile time. The comparison below therefore compiles
// to a plain `>` or `<` with no runtime branch on the side.
template <Side kSide>
struct BetterPrice {
    bool operator()(Price a, Price b) const { return kSide == Side::Buy ? a > b : a < b; }
};

// ---------------------------------------------------------------------------
// Baseline: std::map
// ---------------------------------------------------------------------------
template <typename Queue, Side kSide>
class MapLevels {
    using Map = std::map<Price, Level<Queue>, BetterPrice<kSide>>;

public:
    using Cursor = typename Map::iterator;

    // The map has no inherent price bounds; the book enforces the range. The
    // constructor takes the bounds only so both implementations are built the
    // same way.
    MapLevels(Price /*min_price*/, Price /*max_price*/) {}

    [[nodiscard]] bool empty() const { return levels_.empty(); }
    [[nodiscard]] std::size_t size() const { return levels_.size(); }

    // Precondition: !empty(). begin() is the best price thanks to BetterPrice.
    Cursor best() { return levels_.begin(); }

    // Precondition: a level exists at `price`. O(log levels).
    Cursor find(Price price) { return levels_.find(price); }

    // Finds the level at `price`, inserting an empty one if needed.
    // The caller must add an order to it straight away. O(log levels).
    Cursor get_or_create(Price price) { return levels_.try_emplace(price).first; }

    static Price price_of(Cursor cursor) { return cursor->first; }
    static Level<Queue>& level_of(Cursor cursor) { return cursor->second; }

    // Precondition: the level's queue is empty. Frees the tree node.
    void erase(Cursor cursor) { levels_.erase(cursor); }

    [[nodiscard]] Quantity quantity_at(Price price) const {
        const auto it = levels_.find(price);
        return it == levels_.end() ? 0 : it->second.total_qty;
    }

    // Calls fn(price, level) for up to `max_levels` levels, best price first.
    template <typename Fn>
    void for_each(std::size_t max_levels, Fn&& fn) const {
        std::size_t visited = 0;
        for (const auto& [price, level] : levels_) {
            if (visited++ == max_levels) {
                break;
            }
            fn(price, level);
        }
    }

private:
    Map levels_;
};

// ---------------------------------------------------------------------------
// Optimisation: flat array indexed by tick
// ---------------------------------------------------------------------------
// Every price in [min_price, max_price] has a permanent slot in one contiguous
// array, so finding a level is a subtraction and an array index instead of a
// tree walk, and creating or removing a level allocates nothing. The price is
// paid in memory (a slot for every tick, used or not) and in the scan needed to
// find the next best price when the best level empties. See DESIGN_NOTES #17.
template <typename Queue, Side kSide>
class FlatLevels {
public:
    using Cursor = std::size_t;   // index into levels_

    FlatLevels(Price min_price, Price max_price)
        : min_price_(min_price),
          levels_(static_cast<std::size_t>(max_price - min_price) + 1) {}

    [[nodiscard]] bool empty() const { return non_empty_ == 0; }
    [[nodiscard]] std::size_t size() const { return non_empty_; }

    // Precondition: !empty().
    Cursor best() { return best_; }

    // Precondition: min_price <= price <= max_price (the book validates this).
    Cursor find(Price price) { return static_cast<std::size_t>(price - min_price_); }

    // The caller must add an order to the level straight away: the slot is
    // counted as non-empty from this point on.
    Cursor get_or_create(Price price) {
        const Cursor cursor = find(price);
        if (levels_[cursor].queue.empty()) {
            ++non_empty_;
            if (non_empty_ == 1 || is_better(cursor, best_)) {
                best_ = cursor;
            }
        }
        return cursor;
    }

    [[nodiscard]] Price price_of(Cursor cursor) const {
        return min_price_ + static_cast<Price>(cursor);
    }
    Level<Queue>& level_of(Cursor cursor) { return levels_[cursor]; }

    // Precondition: the level's queue is empty. The slot itself stays put; we
    // only fix the bookkeeping.
    void erase(Cursor cursor) {
        --non_empty_;
        if (cursor != best_ || non_empty_ == 0) {
            return;   // best price unchanged, or the side is now empty
        }
        // The best level emptied: walk towards worse prices to the next slot
        // that has orders. It must exist because non_empty_ > 0 and every
        // other level is worse than the old best. This scan is the flat
        // array's weak spot: O(gap in ticks), where the map is O(1).
        do {
            best_ = step_to_worse(best_);
        } while (levels_[best_].queue.empty());
    }

    [[nodiscard]] Quantity quantity_at(Price price) const {
        const auto offset = price - min_price_;
        if (offset < 0 || static_cast<std::size_t>(offset) >= levels_.size()) {
            return 0;
        }
        return levels_[static_cast<std::size_t>(offset)].total_qty;
    }

    template <typename Fn>
    void for_each(std::size_t max_levels, Fn&& fn) const {
        std::size_t remaining = std::min(max_levels, non_empty_);
        Cursor cursor = best_;
        while (remaining > 0) {
            const Level<Queue>& level = levels_[cursor];
            if (!level.queue.empty()) {
                fn(price_of(cursor), level);
                --remaining;
            }
            // Only step while levels remain, so the cursor never walks off
            // either end of the array.
            if (remaining > 0) {
                cursor = step_to_worse(cursor);
            }
        }
    }

private:
    // `if constexpr` is resolved at compile time: the bid instantiation of this
    // class contains only the first branch, the ask instantiation only the second.
    static bool is_better(Cursor a, Cursor b) {
        if constexpr (kSide == Side::Buy) {
            return a > b;   // higher bid is better
        } else {
            return a < b;   // lower ask is better
        }
    }
    static Cursor step_to_worse(Cursor cursor) {
        if constexpr (kSide == Side::Buy) {
            return cursor - 1;
        } else {
            return cursor + 1;
        }
    }

    Price min_price_;
    std::vector<Level<Queue>> levels_;   // levels_[i] holds price min_price_ + i
    Cursor best_ = 0;                    // meaningful only while non_empty_ > 0
    std::size_t non_empty_ = 0;          // number of slots that currently hold orders
};

}  // namespace lob
