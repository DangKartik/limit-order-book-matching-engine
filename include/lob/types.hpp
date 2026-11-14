// Plain value types shared by the engine and everything that drives it.
#pragma once

#include <cstddef>
#include <cstdint>

namespace lob {

// Prices are integer ticks, never floating point. 0.1 + 0.2 != 0.3 in binary
// floating point, and a matching engine must compare prices for exact equality.
// A tick is the smallest price increment (e.g. 1 tick = $0.01); converting to a
// display price is the caller's job. See DESIGN_NOTES #1.
using Price = std::int64_t;

// Signed on purpose: with an unsigned type, `remaining - fill` silently wraps to
// a huge positive number if we ever get the arithmetic wrong. Signed lets us
// assert(qty >= 0) and lets UBSan catch overflow. See DESIGN_NOTES #2.
using Quantity = std::int64_t;

// Assigned by the engine, starting at 1 and increasing. 0 means "no order".
using OrderId = std::uint64_t;

// `enum class` (scoped enum): unlike a plain C enum it does not implicitly
// convert to int and its names do not leak into the enclosing namespace, so
// you must write Side::Buy. `: std::uint8_t` fixes its size to one byte.
enum class Side : std::uint8_t { Buy, Sell };

// One fill between a resting order (the maker) and an incoming order (the taker).
struct Trade {
    OrderId maker_id;   // the order that was resting in the book
    OrderId taker_id;   // the incoming order that crossed the spread
    Price price;        // always the maker's price (see DESIGN_NOTES #9)
    Quantity qty;
    Side taker_side;

    // C++20: the compiler writes member-by-member equality for us.
    // (Java would need a hand-written equals(); Python a dataclass.)
    bool operator==(const Trade&) const = default;
};

enum class RejectReason : std::uint8_t {
    None,
    InvalidQuantity,   // qty <= 0
    InvalidPrice,      // limit price <= 0
};

// What happened to an order submitted to the book.
struct OrderResult {
    OrderId id = 0;         // 0 if the order was rejected
    Quantity filled = 0;    // total quantity traded immediately
    Quantity resting = 0;   // quantity now sitting in the book (always 0 for market orders)
    RejectReason reject = RejectReason::None;

    bool operator==(const OrderResult&) const = default;
};

// Aggregated view of one price level, for depth snapshots.
struct LevelView {
    Price price;
    Quantity qty;              // total resting quantity at this price
    std::size_t order_count;   // number of resting orders at this price

    bool operator==(const LevelView&) const = default;
};

}  // namespace lob
