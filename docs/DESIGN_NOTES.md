# Design notes

One entry per non-obvious decision: what was chosen, what the alternatives were,
and what the trade-off is. Entries are numbered so code comments can refer to
them (`See DESIGN_NOTES #4`). Part A is a C++ primer for someone coming from
Python and Java; Part B is the decisions.

No performance numbers appear in this file until stage 2 measures them. Where a
note says something is "slow" or "fast", it is a claim about complexity or memory
layout, to be tested by the benchmarks, not a measured result.

---

## Part A. C++ concepts used in stage 1 (for a Python/Java programmer)

### A1. Values, references and copies

In Java and Python a variable of class type is a reference to a heap object, and
assignment copies the reference. In C++ a variable *is* the object, and
assignment copies the whole object.

```cpp
Level  a = level_it->second;   // COPIES the level, including its entire list
Level& b = level_it->second;   // b is another name for the same level; no copy
```

That is why the matching loop writes `Level& level = ...` and `Order& maker = ...`.
Dropping the `&` would compile, run, and silently modify a temporary copy
instead of the book.

`const T&` means "a reference I promise not to modify through". It is the
default way to pass anything bigger than a few machine words without copying it.

### A2. RAII (Resource Acquisition Is Initialization)

C++ has no garbage collector. Instead, every object has a *destructor* that runs
automatically at a precisely defined moment: when a local variable goes out of
scope, or when the object that owns it is destroyed. A class that acquires a
resource (memory, a file, a lock) in its constructor and releases it in its
destructor can never leak it. That pattern is RAII.

`OrderBook` contains no `new`, no `delete` and no destructor of its own. Its
members (`std::map`, `std::list`, `std::unordered_map`) each free their own
memory in their destructors, and the compiler-generated `~OrderBook()` calls
them in reverse order of declaration. The closest Java analogue is
try-with-resources, and Python's is `with`, but in C++ it is the default for
every object rather than something you opt into.

### A3. Iterators and iterator invalidation

An iterator is a generalised pointer to an element of a container. `begin()`
points at the first element, `end()` points one past the last, and
`it->first` / `it->second` read a map entry's key and value.

The trap with no Java/Python equivalent: **modifying a container can invalidate
iterators and references into it, and using an invalidated one is undefined
behaviour**, not an exception. (Java throws `ConcurrentModificationException`;
C++ may crash, corrupt memory, or appear to work.) The rules differ per container:

| Container | Insert | Erase |
|---|---|---|
| `std::vector` | invalidates everything if it reallocates | invalidates the erased element and everything after it |
| `std::deque` | invalidates all iterators | invalidates all iterators (unless erasing at an end) |
| `std::list` | invalidates nothing | invalidates only the erased element |
| `std::map` | invalidates nothing | invalidates only the erased element |
| `std::unordered_map` | invalidates iterators if it rehashes (references to elements survive) | invalidates only the erased element |

The whole baseline design rests on the `std::list` row: `index_` stores a list
iterator for every resting order, and those iterators stay valid for as long as
the order rests, no matter how many other orders are added or removed around it.

Places in the code where invalidation is handled deliberately:

- `match()`: `index_.erase(maker.id)` is called **before** `level.orders.pop_front()`,
  because the pop destroys the `Order` that `maker` refers to.
- `match()`: after `opposite.erase(level_it)`, neither `level_it` nor `level` is
  touched again; the loop re-reads `begin()`.
- `cancel()`: `side` is copied out of the order before `remove_resting()` erases it.

### A4. Move semantics

Copying a `std::vector` duplicates its heap buffer. *Moving* it transfers
ownership of the buffer to the new object and leaves the source empty, at the
cost of copying three pointers. C++ moves automatically when the source is a
temporary or a local variable being returned.

In stage 1 this shows up in `depth()`, which builds a `std::vector<LevelView>`
locally and returns it by value. That looks expensive to a C programmer but is
not: the compiler either constructs the vector directly in the caller's storage
(copy elision, also called NRVO) or moves it. No element is copied.

Move semantics is also why `OrderBook` *deletes* its move operations; see #8.

### A5. Templates versus Java generics

Java generics are erased at runtime: one compiled `List` handles every element
type through references. C++ templates are *instantiated*: the compiler
generates a separate, fully specialised copy of the code for each type used.

`match<SideMap>()` is compiled twice, once for the bid map and once for the ask
map. Each copy calls that map's comparator directly, so it can be inlined; there
is no virtual dispatch. The cost is larger binaries and the usual rule that a
template's definition must be visible where it is used. The private templates
here are declared in the header and defined in `order_book.cpp`, which works
only because every use is inside that same file.

### A6. `const` member functions

`std::optional<Price> best_bid() const;` The trailing `const` promises the
function does not modify the book. The compiler enforces it, and it is what lets
a caller holding a `const OrderBook&` still ask read-only questions.

### A7. Undefined behaviour and why the sanitizers matter

Out-of-bounds access, use-after-free, dangling references and signed integer
overflow are *undefined behaviour*: the standard places no requirement on what
happens. The program might crash, or might pass every test and fail in
production. The build has an option, `-DLOB_SANITIZE=ON`, that compiles with
AddressSanitizer and UndefinedBehaviorSanitizer, which turn most of these into
an immediate, explained crash. The tests are run both ways: sanitized Debug to
prove correctness, Release to prove the optimised build behaves the same.

---

## Part B. Decisions

### #1. Prices are integer ticks (`int64_t`)

**Choice.** `Price` is a signed 64-bit count of ticks. The engine never sees a
decimal price.

**Alternatives.** `double`; a fixed-point decimal class.

**Trade-off.** Matching depends on exact price equality (two orders "at the same
price" share a queue) and on a strict ordering. Binary floating point cannot
represent most decimal fractions (`0.1 + 0.2 != 0.3`), so two prices that should
be equal can differ in the last bit and land in different levels. Integers
compare exactly, hash trivially, and can index an array directly, which stage 2
relies on. The cost is that the caller must convert between display prices and
ticks, and the tick size is fixed per instrument. Real exchange protocols
(for example Nasdaq ITCH) also send prices as scaled integers.

### #2. Quantities are signed

**Choice.** `Quantity` is `int64_t`, even though a valid quantity is never negative.

**Alternatives.** `uint64_t`.

**Trade-off.** With unsigned arithmetic, `remaining - fill` wraps to about
1.8e19 if a bug ever makes `fill > remaining`, and the result looks like a valid
quantity. With signed arithmetic the same bug produces a negative number that an
assert or a `qty <= 0` check catches, and UBSan reports signed overflow.
The cost is losing one bit of range, which is irrelevant at 64 bits.
`OrderId` stays unsigned because it is an identifier that is never subtracted.

### #3. Price levels: `std::map<Price, Level>` (baseline)

**Choice.** Each side is a `std::map` (a red-black tree) keyed by price. Bids use
`std::greater<Price>` so the map sorts descending; asks use the default
ascending order. On both sides `begin()` is the best price.

**Alternatives.**
- A sorted `std::vector` of levels: contiguous memory and fast iteration, but
  inserting or removing a level in the middle is O(levels).
- `std::unordered_map`: O(1) lookup by price but no ordering, so finding the
  best price after a level empties means scanning.
- A flat array indexed by tick: O(1) for everything, but needs a bounded price
  range. This is the stage 2 optimisation.
- One map type for both sides, reading bids from `rbegin()`: avoids the template,
  but erasing through a reverse iterator needs the awkward `std::next(it).base()`
  conversion and is easy to get wrong.

**Trade-off.** The map gives O(log L) insert, find and erase for L price levels,
O(1) access to the best price, and no limit on the price range. Its weakness is
memory layout: every level is a separately heap-allocated tree node, so walking
the tree chases pointers to unrelated addresses. It is the clearest correct
baseline, and the thing stage 2 measures against.

Because the comparator is a template argument, the bid map and ask map are
different types. The matching code is therefore a template over the map type
(see A5) rather than two copy-pasted functions.

### #4. Queue within a level: `std::list<Order>` (baseline)

**Choice.** Each level holds its orders in a `std::list`, oldest at the front.

**Alternatives.**
- `std::deque<Order>`: cache-friendlier, O(1) push-back and pop-front, but
  erasing from the middle is O(n) and invalidates every iterator, so cancel
  could not use stored handles.
- `std::vector<Order>` with tombstones (mark cancelled, skip when matching):
  contiguous, but the queue grows with dead entries and needs compaction.
- An intrusive doubly linked list over an object pool: the same O(1) operations
  with no per-order heap allocation. This is the stage 2 optimisation.

**Trade-off.** Matching needs FIFO at the front; cancel needs O(1) removal from
anywhere; the cancel index needs handles that stay valid. `std::list` is the
only standard container that gives all three (see the table in A3). The price
is one heap allocation and one deallocation per order, and nodes scattered
across the heap.

Each `Level` also caches `total_qty`, the sum of its orders' remaining
quantities. It costs one addition or subtraction on each change and makes
`quantity_at()` and `depth()` O(1) per level instead of a walk over the queue.
The invariant (`total_qty` equals the sum) is checked indirectly by the
differential test, which compares depth against the reference book.

### #5. Cancel lookup: `std::unordered_map<OrderId, list iterator>`

**Choice.** A hash map from order id to the order's iterator inside its level's
list. The stored `Order` carries its own `side` and `price`, so the iterator
alone is enough to find everything else.

**Alternatives.**
- Search the book for the id: O(total orders) per cancel. Cancels are a large
  share of real order flow, so this is not viable.
- `std::map<OrderId, ...>`: O(log n) and a heap node per entry, with no benefit
  since ids are never iterated in order.
- A `std::vector` indexed directly by id: possible because ids are dense and
  engine-assigned, and faster, but it grows forever unless ids are recycled.
- Also storing the level's map iterator in the handle, to skip the O(log L)
  `find()` in `remove_resting()`: the bid and ask iterators are different types,
  so the handle would need a `std::variant` or two fields. Not worth the
  complexity in the baseline.

**Trade-off.** Cancel costs one average-O(1) hash lookup, one O(log L) level
lookup and one O(1) list erase. The hash map adds its own node allocation per
resting order and its worst case is O(n) on a rehash or under heavy collisions.
A rehash moves buckets, not elements, and it does not disturb the list iterators
stored as values, because those point into the lists, not into the hash map.

### #6. Trades are appended to a caller-owned buffer

**Choice.** `add_limit` and `add_market` take a `std::vector<Trade>&` and
`push_back` each fill. The engine never clears it.

**Alternatives.**
- Return a `std::vector<Trade>` by value: the simplest signature, but it
  allocates on every order that trades.
- A callback (`std::function` or a template parameter) invoked per trade: no
  buffer, but it runs user code in the middle of matching, while the book is in
  an intermediate state, and `std::function` adds an indirect call.
- A fixed-size array: no allocation ever, but one order can generate an
  unbounded number of fills.

**Trade-off.** A vector that is `clear()`ed and reused keeps its capacity, so
after warm-up the steady state does no allocation at all. The caller decides
when to clear, which also lets it batch trades from several orders. The cost is
a slightly less convenient signature and a rule the caller has to know: trades
accumulate until you clear them.

### #7. The engine assigns order ids

**Choice.** Ids come from a counter starting at 1. Rejected orders get id 0 and
do not consume a number.

**Alternatives.** Caller-supplied ids, as in real exchange protocols where the
client names its own orders.

**Trade-off.** Engine-assigned ids are unique by construction, so there is no
duplicate-id check on the hot path and no error case to handle. The stream is
still reproducible, because the n-th accepted order always gets id n. The cost
is that the caller has to read the id from the result before it can cancel.

### #8. `OrderBook` is non-copyable and non-movable

**Choice.** The copy and move constructors and assignment operators are `= delete`.

**Alternatives.** Let the compiler generate them; or write a correct deep copy
that rebuilds the index.

**Trade-off.** `index_` holds iterators into lists owned by the same object. A
compiler-generated copy would copy the lists and copy the iterators, and the
copied iterators would still point into the *original* book's lists. Cancelling
through the copy would then corrupt the original. Deleting the operations turns
that latent bug into a compile error. A move would actually be safe for these
containers, but nothing needs it, and "you cannot copy or move it" is a simpler
rule to defend than "moves are fine, copies are not". Code that needs to pass a
book around takes a reference, or holds it through a `std::unique_ptr`.

### #9. Trades print at the maker's price

**Choice.** A trade's price is the resting order's price, never the incoming
order's limit.

**Trade-off.** This is the standard rule on continuous limit order markets. The
resting order was displayed first at a firm price; the incoming order agreed to
that price or better. A buy limit at 105 against a resting ask at 100 trades at
100, and the buyer keeps the 5-tick improvement. It also means a limit price is
a worst acceptable price, not the execution price.

### #10. Market orders are limit orders at the extreme price that never rest

**Choice.** `add_market` calls the same `submit()` path with the limit set to
`INT64_MAX` for a buy or `INT64_MIN` for a sell, and a flag that says the
remainder must not rest.

**Alternatives.** A separate matching loop for market orders; or an
`is_market` test inside the loop's crossing check.

**Trade-off.** One matching loop means one place for a priority bug to hide and
no extra branch per level. The sentinel price is only ever compared, never used
in arithmetic and never stored (the remainder is dropped, so no order is created
with it), so it cannot overflow or leak into the book. The reference book in the
tests deliberately does *not* use this trick; it has an explicit `is_market`
flag, so the two implementations do not share the assumption.

The unfilled remainder of a market order is cancelled. A market order has no
price at which it could rest, and letting it wait would mean it executes later
at whatever price appears, which is unbounded risk for the sender.

### #11. Empty levels are erased immediately

**Choice.** When the last order leaves a level, by fill or by cancel, the level
is removed from the map in the same operation.

**Alternatives.** Leave empty levels in place and skip them when matching.

**Trade-off.** Eager removal keeps the invariant "every level in the map has at
least one order", so `begin()` is always real liquidity and `best_bid()` is one
pointer read. The cost is a node deallocation, and a reallocation if the same
price is used again a moment later, which is common near the touch. Stage 2's
flat array removes that cost by making an empty level just a slot with zero
orders.

### #12. Input validation and what is not validated

**Choice.** `qty <= 0` and limit `price <= 0` are rejected with a reason code in
the result. Nothing throws.

**Alternatives.** Throw exceptions; use asserts only; return `std::expected`
(which is C++23, not C++20).

**Trade-off.** A rejection is a normal, expected outcome of order entry, not an
exceptional one, and a result code keeps the hot path free of exception
machinery. Internal invariants that can only break through a bug in the engine
itself use `assert`, which is checked in Debug builds and compiled out in
Release.

Not handled, by design, and listed here so the limits are explicit: there is no
self-trade prevention (two orders from the same participant can match each
other), no order modification (cancel and resubmit instead, which loses time
priority), no time-in-force flags (IOC, FOK), and no overflow check on a level's
total quantity (it would take more than 9e18 units at one price).

### #13. Testing strategy: scenario tests plus a differential test

**Choice.** Two layers. `order_book_test.cpp` has small hand-written scenarios,
one rule per test. `differential_test.cpp` drives the engine and a deliberately
naive `ReferenceBook` with the same seeded random stream (20,000 operations for
each of 5 seeds) and requires identical results, trades, best prices and order
counts after every operation, plus identical full depth every 100 operations.

**Alternatives.** Scenario tests only; or property-based testing with a library.

**Trade-off.** Scenario tests document the rules and fail with a readable
message, but only cover cases someone thought of. The differential test covers
interleavings nobody thought of, at the price of needing a second
implementation. The reference book is a flat vector scanned linearly: slow, but
short enough to check by eye, and sharing no data structure with the engine. The
test also asserts that the stream produced over 1,000 trades and over 1,000
successful cancels, so it cannot pass vacuously. Its limit is that both
implementations were written by the same person from the same understanding of
the rules: it catches implementation bugs, not a misunderstanding of the rules.
The scenario tests are what pin the rules themselves.

### #14. Build: warnings, sanitizers, default build type

**Choice.** CMake with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow` on our
own targets, an opt-in `LOB_SANITIZE` option, and `Release` as the default build
type. GoogleTest is fetched and pinned by `FetchContent`.

**Trade-off.** `-Wconversion` is noisy but catches silent narrowing between
`int64_t`, `size_t` and `int`, which matters in code that mixes quantities and
container sizes. Warnings are attached through an `INTERFACE` target so they
apply to our code and not to GoogleTest. Defaulting to Release prevents the most
common benchmarking mistake, which is timing an unoptimised build. FetchContent
needs network access on the first configure, in exchange for not requiring a
system-wide install.
