# Design notes

One entry per non-obvious decision: what was chosen, what the alternatives were,
and what the trade-off is. Entries are numbered so code comments can refer to
them (`See DESIGN_NOTES #4`). Part A is a C++ primer for someone coming from
Python and Java; Part B is the decisions.

Entries #1 to #14 were written in stage 1, before anything was measured: where
they say something is "slow" or "fast" it is a claim about complexity or memory
layout. Entries #15 onwards (stage 2) test those claims; every number there
comes from a Release build run on this machine and is saved in `results/`.

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

- `match()`: `index_.erase(maker.id)` is called **before** `level.queue.pop_front()`,
  because the pop destroys the `Order` that `maker` refers to.
- `match()`: after `opposite.erase(cursor)`, neither `cursor` nor `level` is
  touched again; the loop re-reads `best()`.
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

`match<Levels>()` is compiled twice per book type, once for the bid side and
once for the ask side. Each copy calls that side's code directly, so it can be
inlined; there is no virtual dispatch. The cost is larger binaries and the rule
that a template's definition must be visible where it is used, which is why the
engine is header-only from stage 2 on (see #15).

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

**Choice.** Each side is a `std::map` (a red-black tree) keyed by price, ordered
by a small comparator, `BetterPrice<kSide>`, that sorts bids descending and asks
ascending. On both sides `begin()` is the best price.

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

**Choice.** `qty <= 0` and a limit price outside the book's configured range
(`BookConfig`, by default 1 to 100,000 ticks) are rejected with a reason code in
the result. No order-entry call throws. The only exception in the engine is
`std::invalid_argument` from the constructor for an impossible price range.

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
From stage 2, both layers are typed tests that run against all four book
implementations (#15), and `order_pool_test.cpp` tests the pool and intrusive
queue directly.

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

---

## Stage 2: selectable implementations and benchmarks

### #15. One matching engine, four data-structure combinations, chosen at compile time

**Choice.** The book is a class template,
`BasicOrderBook<Queue, LevelsT>`. `Queue` is `ListQueue` or `IntrusiveQueue`;
`LevelsT` is `MapLevels` or `FlatLevels`. Four aliases name the combinations:
`MapListBook` (the stage 1 baseline), `MapPoolBook`, `FlatListBook`,
`FlatPoolBook`. The matching logic exists once.

**Alternatives.**
- Two unrelated classes, baseline and optimised: simple, but the matching code
  is duplicated, the copies can drift, and only "both optimisations together"
  can be measured.
- A common base class with virtual functions, chosen at runtime: the Java way.
  Every queue and level operation becomes an indirect call that cannot be
  inlined, which adds a cost to exactly the thing being measured.
- `#ifdef` switches: one variant per build, so the variants cannot be tested
  against each other in one binary.

**Trade-off.** Templates give *static polymorphism*: the compiler generates four
separate, fully inlined engines, so the abstraction costs nothing at runtime and
a 2x2 experiment can attribute a speed-up to one data structure. The costs are
real: the code is harder to read than stage 1's, error messages are worse, the
two policies must agree on an interface that nothing but compilation checks
(C++20 `concept`s could state it explicitly; not done here), and the engine has
to be header-only because a template's body must be visible where it is used.

C++ features this introduces:
- **Template template parameter** (`template <typename, Side> class LevelsT`):
  the argument is a template, not a type. The book instantiates it twice, as
  `LevelsT<Queue, Side::Buy>` and `LevelsT<Queue, Side::Sell>`.
- **Non-type template parameter** (`Side kSide`): a compile-time value. The bid
  and ask containers are different types with the price ordering baked in.
- **`if constexpr`**: a branch resolved at compile time; the untaken side is
  not even compiled into that instantiation.
- **Cursor**: each level container defines its own cheap "pointer to a level"
  (a map iterator, or an array index). The book fetches a cursor once and reuses
  it, so the map version does exactly one tree lookup per operation, the same as
  the stage 1 code. Without this the refactor would have quietly slowed the
  baseline and flattered the optimisations.

### #16. Queue optimisation: intrusive doubly linked list over an object pool

**Choice.** `IntrusiveQueue` links `OrderNode`s (an `Order` plus `prev`/`next`
pointers) that come from an `OrderPool`. The pool allocates nodes in chunks of
4,096 and keeps freed nodes on a LIFO free list for reuse.

**Alternatives.**
- `std::list` with a pooling allocator (`std::pmr::list` over a
  `std::pmr::unsynchronized_pool_resource`): less code, but the node layout and
  reuse policy stay hidden inside the library.
- A pool that is one `std::vector<OrderNode>` with 32-bit indices instead of
  pointers: smaller links, but growing the vector moves every node, which is a
  latency spike, and every access needs the pool to turn an index into an address.
- Never freeing: a bump allocator. Fastest, but memory grows without bound.

**Trade-off.** What the pool removes is the call into the general-purpose
allocator (`malloc`/`free`) on every order added and removed. `malloc` has to
handle any size from any thread; the pool handles one size from one thread, so
allocate and release are each a couple of pointer moves. Nodes from the same
chunk are adjacent in memory, and the LIFO free list hands back the most
recently touched node, which is likely still in cache. Chunks are owned by
`std::unique_ptr<OrderNode[]>`, so node addresses never change (growing the
vector of chunks moves only the owning pointers) and everything is freed by RAII
when the book is destroyed.

The costs: about 100 lines of hand-written pointer code where a bug is memory
corruption rather than an exception (hence `order_pool_test.cpp` and the
sanitizer build); memory is never returned to the system while the book lives,
so the pool stays at its high-water mark; and after heavy churn the free list
order is effectively random, so neighbours in a queue are not neighbours in
memory. The cancel index is still a `std::unordered_map`, which does its own
heap allocation per order, so this removes one of the two allocations per
resting order, not both.

### #17. Level optimisation: flat array indexed by tick

**Choice.** `FlatLevels` is a `std::vector<Level>` with one permanent slot for
every tick in `[min_price, max_price]`. It tracks the index of the best
non-empty level and a count of non-empty levels.

**Alternatives.**
- Keep the map but pool its nodes: removes the allocations, keeps the
  O(log L) pointer-chasing lookup.
- A sorted `std::vector` of only the non-empty levels: compact, but inserting a
  new level shifts the ones after it.
- The flat array plus a bitmap of non-empty slots, so the next best level is
  found with a count-trailing-zeros instruction instead of a slot-by-slot scan.
  This is the natural next step if the scan ever showed up in a profile.

**Trade-off.** Finding a level becomes `levels_[price - min_price]`: one
subtraction and one index, no tree walk, no comparisons, and no allocation when
a level appears or disappears. The costs:
- **Memory**: a slot for every tick whether used or not. A `Level` is 32 bytes
  here, so the benchmark range of 20,000 ticks costs 640 KB per side and the
  default 100,000-tick range costs 3.2 MB per side.
- **A bounded price range** fixed at construction. A real venue has price bands
  too, but if the price leaves the range the book cannot follow it. This is why
  `BookConfig` exists and why all four books enforce the same range.
- **The scan**: when the best level empties, the next one is found by walking
  slot by slot towards worse prices, which is O(gap in ticks); the map does it
  in O(1). I expected this to make the flat array lose on a sparse book. It did
  not (see #20), and that entry says why I think so.

### #18. A hand-written benchmark harness

**Choice.** `bench/bench_main.cpp` replays a pre-generated stream and does its
own timing. It makes two kinds of pass: a *throughput* pass with one timestamp
at each end, and a *latency* pass with a timestamp around every operation, from
which it sorts the samples and reads off percentiles by nearest rank.

**Alternatives.** Google Benchmark, which is the standard tool and handles
warm-up, repetition and statistics well.

**Trade-off.** Google Benchmark reports the mean time of a loop. A matching
engine is judged on its tail: the p99 and p99.9, where the slow operations are.
Seeing the tail needs a sample per operation and a replay of a specific stateful
stream, both of which fit awkwardly in that framework. Writing the harness also
means I can explain every number it prints. The price is that I own its
mistakes, so the harness is built to distrust itself:
- **The stream is generated once, outside any timed region**, and every
  implementation replays the same operations.
- **A checksum** (trade count, filled quantity, successful cancels, final order
  count) must be identical for all four implementations or the run aborts.
- **Runs are interleaved** (A B C D, A B C D, ...), not grouped, so thermal
  drift on a fanless laptop is spread over all implementations.
- **Throughput is the median of 7 runs**, with the minimum and maximum saved to
  the CSV so the spread is visible. In the saved run the spread within any one
  implementation was under 2%.
- **It measures its own clock** and prints the result. See the limits below.
- **It warns loudly if it is not a Release build.**

**Measurement limits, which apply to every latency number.**
- The clock on this machine ticks every **41.67 ns** (Apple Silicon's timer runs
  at 24 MHz). Every latency sample is therefore a multiple of about 42 ns: the
  values 41, 42, 83, 84, 125 in the tables are 1, 2 and 3 ticks, and the 1 ns
  wobble is rounding. Two implementations that both show "p50 = 42" may differ
  by a lot underneath. A typical operation here takes 30 to 75 ns, which is
  about one tick, so **the p50 column cannot rank the implementations; only the
  throughput-derived ns/op can.** The tail columns are coarse but meaningful.
- Each latency sample includes two clock reads, measured at about 15 to 24 ns
  per read across runs. The reads are not subtracted.
- The `max` column is a single worst sample out of 2,000,000 and mostly reflects
  the operating system (an interrupt or a context switch), not the book. It is
  about 14 to 19 microseconds for every implementation. Treat it as noise.
- macOS cannot pin a thread to a core. The harness requests the
  user-interactive QoS class, which prefers the performance cores, but that is a
  hint.
- Single machine, single compiler, synthetic flow.

### #19. The synthetic order stream

**Choice.** A mid price follows a slow random walk (one tick on about 2% of
operations). Limit orders arrive on both sides at a geometrically distributed
distance from the mid, with 10% priced through the mid so that they trade.
Cancels pick a live resting order uniformly at random. 5% of operations are
market orders. A simple controller leans towards adding orders when the book is
below its target depth and towards cancelling when above. Three scenarios vary
the shape of the book:

| Scenario | Resting orders | Mean distance from mid | Purpose |
|---|---|---|---|
| dense | 10,000 | 10 ticks | many orders on few levels |
| deep | 100,000 | 50 ticks | a large book that does not fit in the fast caches |
| sparse | 2,000 | 1,000 ticks | few orders on many far-apart levels: intended as the flat array's worst case |

**Alternatives.** Replaying real exchange data (for example Nasdaq ITCH), which
would be more convincing but needs a data licence or a large download and a
parser. Or a uniform random stream, which is easy but keeps the book either
empty or permanently crossed.

**Trade-off.** The stream is reproducible and its shape is controllable, which
is what a before/after comparison needs. It is not realistic: real flow is
bursty, cancels cluster near the best price, and order sizes are not uniform.
The mix that results (about 64% limit, 31% cancel, 5% market in the dense and
deep scenarios; 54/41/5 in sparse) is printed by the harness. It has a far
higher trade rate than a real market, where well over 90% of orders are
cancelled without trading. The results say how these data structures behave on
this stream, not how fast the engine would be on a real feed.

My first version of the controller was too weak: the dense book drained from
10,000 orders to 58 during the run, so it was benchmarking a nearly empty book.
The harness prints the final resting order count for exactly this reason.

### #20. Results

Release build (`-O3 -DNDEBUG`), Apple M5 MacBook Air, Apple clang 21,
2,000,000 measured operations per scenario, median of 7 runs. Raw output:
`results/bench_stage2.txt` and `results/bench_stage2.csv`.

Throughput in million operations per second (ns per operation in brackets):

| Scenario | map+list (baseline) | map+pool | flat+list | flat+pool | Speed-up, both |
|---|---|---|---|---|---|
| dense | 18.7 (53.4) | 22.5 (44.4) | 23.3 (42.9) | 31.7 (31.5) | 1.69x |
| deep | 13.7 (72.8) | 16.4 (61.1) | 17.9 (56.0) | 22.8 (43.8) | 1.66x |
| sparse | 13.2 (75.5) | 14.4 (69.7) | 26.8 (37.4) | 34.4 (29.1) | 2.60x |

Latency over all operations, in ns (multiples of the 42 ns clock tick):

| Scenario | Implementation | p50 | p99 | p99.9 |
|---|---|---|---|---|
| dense | map+list | 42 | 125 | 209 |
| dense | flat+pool | 41 | 84 | 167 |
| deep | map+list | 83 | 208 | 292 |
| deep | flat+pool | 42 | 167 | 250 |
| sparse | map+list | 83 | 167 | 292 |
| sparse | flat+pool | 41 | 125 | 167 |

What the numbers show:
- **Both optimisations help in every scenario, and they combine.** In dense, the
  pool alone gives 1.20x and the flat array alone 1.25x; together 1.69x, which
  is more than the product of the two (1.50x).
- **The flat array matters most when there are many levels.** In sparse, it
  alone gives 2.02x, because the map there holds on the order of a thousand
  levels per side and every add and cancel walks about ten tree nodes. In dense,
  the map is small and its top nodes stay in cache, so the gain is 1.25x.
- **The pool's gain is steadier**: 1.08x to 1.20x alone, 1.28x to 1.36x on top of
  the flat array. It is larger once the tree is gone because a fixed saving is
  a bigger share of a shorter operation.
- **A bigger book is slower for everyone.** Baseline goes from 53 to 73 ns per
  operation between dense and deep. The data no longer fits in the fast caches,
  and the hash map lookup for cancel, which all four share, misses more often.
- **The tail improves by about one clock tick**, for example p99.9 from 209 to
  167 ns in dense and 292 to 167 ns in sparse. The clock cannot resolve more.

What surprised me: I expected the flat array to lose in the sparse scenario
because of its scan for the next best level. It won by its largest margin
there. I have not profiled it, so this is reasoning, not measurement: the scan
runs only when the best level empties; it reads memory sequentially, which the
hardware prefetcher handles well; and with orders concentrated near the mid, the
next occupied slot is usually close even in this scenario. The map, meanwhile,
pays its tree walk on every add and cancel.
A workload that emptied the best level on most operations with gaps of many
thousands of ticks would reverse this, and the bitmap in #17 is the fix.

What this does not show: no profiler or hardware counters were used, so the
explanations above about caches and allocators are the standard account and
consistent with the numbers, but not directly observed. The remaining shared
cost is the `std::unordered_map` cancel index; replacing it (for example with a
vector indexed by order id) is the obvious next optimisation and is not done.

`OrderBook` remains an alias for the baseline. The Python bindings in stage 3
use `FlatPoolBook`, the fastest measured.

---

## Stage 3: Python bindings

### #21. pybind11, with the binding layer kept outside the engine

**Choice.** `bindings/pylob.cpp` exposes the engine as a Python module, `pylob`,
using pybind11 (fetched and pinned by CMake, like GoogleTest). The engine headers
do not mention Python. The module is built only with `-DLOB_BUILD_PYTHON=ON`
and is written into `python/`, where the Python code imports it.

**Alternatives.**
- `ctypes` or `cffi` over a C interface: no build-time dependency, but it needs
  a hand-written C wrapper and gives no automatic conversion of classes, enums,
  vectors or exceptions.
- Cython: a separate language to learn and maintain.
- Reimplementing the book in Python for the simulator: then the market maker
  would not be trading against the engine this project is about.
- Packaging with `pyproject.toml` and scikit-build-core so that `pip install .`
  works: the right thing for a library other people install; more machinery
  than a single-machine project needs. Not done.

**Trade-off.** pybind11 generates the conversion code from short declarations
and maps C++ concepts onto their Python equivalents: `std::optional` becomes
`None` or a value, `std::vector` becomes a list, `std::pair` a tuple,
`std::invalid_argument` a `ValueError`, and a wrong argument type a `TypeError`.
It is header-only but heavy on templates, so `pylob.cpp` is the slowest file in
the project to compile. The built module is tied to one Python version
(`pylob.cpython-314-darwin.so`).

Ownership across the boundary, which is the part with no Python analogue: the
Python object owns the C++ book, and the book is destroyed when Python's
reference count for it reaches zero. `Trade`, `OrderResult` and `LevelView` are
copied into new Python objects, so nothing Python holds points into memory the
engine might later free or reuse.

### #22. The Python API returns trades instead of taking a buffer

**Choice.** `book.add_limit(side, price, qty)` returns a tuple
`(OrderResult, list_of_trades)`. The wrapper struct `PyBook` owns one
`std::vector<Trade>` for the life of the book, clears it before each call, and
copies its contents into the returned list.

**Alternatives.** Mirror the C++ signature and make Python pass a buffer object
in; or keep the trades inside the wrapper and add a `book.last_trades()` call.

**Trade-off.** The caller-owned buffer exists to avoid allocation in C++ (#6).
Python allocates objects on every call regardless, so that discipline buys
nothing there and would make the API awkward. Returning the trades makes each
call self-contained, and a test checks that a list returned earlier is not
changed by a later call. The C++ side still allocates nothing per order.

Two books are bound: `pylob.OrderBook` is `FlatPoolBook` and
`pylob.BaselineOrderBook` is `MapListBook`. A Python test replays one random
stream through both and requires identical results.

**Measured cost.** `python/bench_bindings.py` times a Python loop that submits
1,000,000 limit orders and cancels some of them. On this machine one loop
iteration takes about 390 ns with `OrderBook` and about 410 ns with
`BaselineOrderBook` (best of 5 runs). The engine operation inside is tens of
nanoseconds (#20), so roughly nine tenths of the time is the Python interpreter
and the conversion layer, and the choice of data structure barely shows. The
bindings are for driving experiments, not for speed; anything latency-sensitive
belongs in C++.

The GIL (Python's global interpreter lock) is held for the whole of every call.
Releasing it would be pointless: each call is far shorter than the cost of
releasing and re-taking the lock, and the engine is single-threaded by design.

---

## Stage 4: market-making simulator

Every number in this section comes from `python/run_analysis.py` (fixed seeds;
the saved run took 95.2 s on this machine with 10 worker processes, recorded
under `timings_seconds`; the timing is the only thing that changes between
runs) and is stored in `results/mm/summary.json`; the full printed report is
`results/mm/report.txt` and the figures are `results/mm/fig*.png`. Units
throughout: prices in ticks, quantities in lots, P&L in ticks x lots. "s.e." is
the standard error of a mean over episodes.

### #23. An exogenous tape and common random numbers

**Choice.** `mm/flow.py` draws every background order of an episode up front
from one seed: arrival steps, sides, prices, sizes and cancellation times. The
tape does not depend on the book or on any strategy. `mm/simulate.py` replays
it through a fresh `pylob.OrderBook`, and the market maker's orders are added
to the same book. Two strategies run on the same seed therefore receive
exactly the same incoming orders; only the matching outcomes differ.

**Alternatives.**
- Reactive background traders that look at the book (for example, limit
  orders placed relative to the current best bid). More realistic, but then the
  flow depends on the market maker's quotes and two strategies no longer face
  the same market.
- Independent seeds per strategy. Simpler, but the comparison then carries the
  full episode-to-episode noise.

**Trade-off.** Common random numbers make the strategy comparison much
sharper. In #30 the symmetric strategy's P&L has a std of 790.9 across tapes,
while the per-tape difference between the two strategies has a std of 757.5:
the inventory strategy's own P&L is so stable (std 173.4) that most of the
difference is the symmetric strategy's luck, but the pairing still guarantees
that both saw identical flow. The cost is that the background never reacts to
the market maker. A cancelled order whose price the market maker has improved
is still cancelled on schedule, and no background trader ever reacts to the
market maker's quotes (#33).

### #24. Background orders are priced off a hidden fundamental

**Choice.** A fundamental price follows a Gaussian random walk (default std
0.4 ticks per step). Background limit orders are priced a geometrically
distributed number of ticks (mean 8) either side of the rounded fundamental.
Market orders arrive at 0.4 per step with a random side and a size uniform on
1..10. The market maker never sees the fundamental; it sees only the book.

**Alternatives.** Price limit orders relative to the book mid (then the book
has no anchor and the "true price" is undefined), or script adverse selection
directly (for example, make some market orders informed about the next move).

**Trade-off.** Anchoring to the fundamental gives a definite notion of value
to mark against (#27) and makes adverse selection emerge from matching rather
than from a rule: when the fundamental moves, new limit orders are priced
through quotes that are now stale and trade against them. Market orders,
by contrast, carry no information at all: their side is a coin flip. #31
shows that this split decides whether the market maker makes money. The
fundamental's increments are independent of everything that happened before,
which has a useful consequence: the inventory P&L marked at the fundamental has
zero expectation exactly, since inventory at the end of step s - 1 is
independent of the fundamental's move in step s.

The resulting background spread, measured while the market maker runs and
excluding its own orders, averages 2.921 ticks (s.e. 0.004) in the inventory
strategy's episodes and 2.875 (s.e. 0.003) in the symmetric strategy's.

### #25. How the market maker quotes

**Choice.** At the end of each step the market maker computes the model's bid
and ask around the mid of the *background* touch (the best prices of everyone
except itself), then:
1. rounds outwards (`floor` for the bid, `ceil` for the ask), so it never quotes
   tighter than the model asks for;
2. clamps each quote to stay passive (a bid at most one tick below the
   background best ask, and vice versa), so it is always the maker and never
   pays the spread;
3. leaves a quote alone if its target price is unchanged, even if partly filled,
   to keep its place in the queue at that price;
4. otherwise cancels and reposts. The new quotes first meet the next step's
   flow, which is priced off a fundamental that has already moved: a one-step
   reaction lag.

**Alternatives.** Quoting around the full-book mid (the market maker would then
chase its own orders once it is the best price); rounding to the nearest tick
(tighter, more fills, less edge per fill); allowing aggressive quotes that
cross the spread to dump inventory; requoting every order every step (simpler,
but always at the back of the queue).

**Trade-off.** Each choice is conservative and each costs fills. Outward
rounding makes the realised spread wider than the model's: the model asks for
2.9684 ticks at t = 0 and 2.8184 at t = T, but the inventory strategy's average
quoted spread is 3.872 ticks and the symmetric strategy's 3.481. A plausible
reading, consistent with these numbers but not separately tested: the symmetric
strategy is centred on a mid that is always a whole or half tick, so its
rounded quotes land on only a few distances, while the inventory skew moves the
centre to arbitrary fractions, and outward rounding of an arbitrary fraction
adds about one tick to the spread on average. Both are wider than the
background spread (#24), so the market maker often joins the background best
price rather than improving it, and then waits behind earlier orders. The
one-step lag is the whole latency model; there is no notion of being faster or
slower than anyone else.

### #26. Units and mapping the model's parameters onto steps and ticks

**Choice.** Time in the quoting formulas is measured in episodes (T = 1); a
quote posted at the end of measured step i has `time_left = (n - 1 - i) / n`,
so the last quote of the episode has zero time left and never trades. The
model's volatility is the fundamental's std over one episode,
`sigma = 0.4 * sqrt(2000) = 17.8885` ticks per square-root episode. The fill
curve is calibrated per step and per tick (#28), and k = 0.7094 per tick is
used directly. A (the fill intensity) does not appear in the quoting formulas
at all; it only matters for the pure-model comparison in #32, where the model
is run in step units instead (dt = 1 step, 2000 steps, sigma = 0.4 per step,
A per step). gamma times sigma squared times time is the same number in either
unit system, so gamma carries over unchanged.

gamma is chosen by a stated rule rather than tuned: holding one quote's worth
of inventory (5 lots) at the start of the episode shifts the reservation price
by 0.75 ticks, `5 * gamma * sigma^2 * 1 = 0.75`, which gives
gamma = 0.00046875 per tick per lot.

**Alternatives.** Time in steps throughout (then sigma would be 0.4 and T 2000;
equivalent, but the spread term is less obviously "per episode"); a gamma
taken from the paper (gamma has units of 1 / (tick x lot) and multiplies
sigma squared, so a value chosen for the paper's sigma = 2 means something
entirely different when sigma over an episode is 17.8885 ticks).

**Trade-off.** The skew rule is easy to state and to reason about in ticks,
but it is a choice, not an optimum; #33 sweeps it over two orders of
magnitude. Two small mismatches with the pure model are deliberate and
documented rather than fixed: the pure model's `time_left` at step i is
`(n - i)` steps, one step more than the book's, and its quotes are continuous
rather than rounded.

The market maker is given the true sigma. A real one would have to estimate
it, with error.

### #27. Mark-to-market and the exact P&L attribution

**Choice.** P&L is `cash + inventory * mid`, with the background mid as the
mark. Its change over a step splits exactly into
- *inventory P&L*: inventory carried in from the previous step times the move
  in the mid, and
- *spread P&L*: for each fill in the step, its edge against the step's closing
  mid, times its quantity.
Cash is an integer and every term is a multiple of 0.5, so in float64 the
identity holds with no rounding, and a test checks it bit for bit.
`mm/analysis.py` computes the same split against any price path, in
particular against the hidden fundamental.

**Alternatives.** Mark at the last trade price, or at the market maker's own
quote mid; attribute by round trips (FIFO matching of buys and sells).

**Trade-off.** Marking at the book mid is what a real desk could do, and the
identity makes every tick of P&L traceable. But the split is only as
meaningful as the mark. In this simulator the book mid is pushed around by the
very orders that trade with the market maker, so "inventory P&L" against the
mid partly measures how the mid moved *because of* those orders. #31 shows
that at the default volatility the mid-marked inventory P&L is positive
(146.4, s.e. 3.6, for the inventory strategy) while marked at the fundamental
it is zero within noise (-3.0, s.e. 3.5), and that at high volatility the mid
marking books adverse selection as inventory losses.

### #28. Calibrating the fill curve, and what the calibration ignores

**Choice.** With no market maker in the book, for every step and side the
simulator records how far from the start-of-step mid aggressive flow reached
(`reach_buy`, `reach_sell`). For a grid of distances delta, the per-step
probability that a quote at delta would have been reached is the fraction of
side-steps with reach >= delta, counting steps with no aggressive order (NaN)
as not reached. A straight line is fitted to log(probability) against delta
over delta in [1, 3] ticks, the range in which the market maker actually
quotes. 200 calibration episodes (seeds 10000 to 10199, disjoint from every
evaluation seed) give 800,000 side-steps, of which 0.2017 saw aggressive flow.

Result: A = 0.3734 per step and k = 0.7094 per tick, R^2 = 0.9866, largest
absolute log residual 0.073 within the fit range.

The curve is not a single exponential. The local slope between grid points
rises from 0.298 per tick (0.5 to 1 tick) to about 1 (for example 1.010
between 3.5 and 4), and the tail fit over [3, 8] gives k = 1.0019 with
R^2 = 0.9999. The flattening near the touch is expected: a quote inside the
background spread is reached by almost any aggressive order, so probability
stops growing as the quote moves closer.

**What the estimate ignores.**
- *Queue position.* "Reached" means aggressive flow traded at or beyond that
  price; a real quote there might have had other orders ahead of it.
- *The market maker's own effect.* With the market maker in the book, its
  quotes change which orders cross and where the mid is.
- *Half-tick mids.* Distances are multiples of 0.5 because the mid is a whole
  or half tick; whole and half distances come from books with different spread
  parity and are pooled.
- *Most steps have no reach.* Only 0.2017 of side-steps saw any aggressive
  order; the rest are NaN and count as "not reached", which is what a per-step
  probability needs, but it means the curve is a probability per step, not a
  Poisson intensity. At the probabilities involved (below 0.2) the two are
  close.
- *Circularity of the fit range.* The range [1, 3] is where the market maker
  quotes, which itself depends on k. Fitting the tail instead would give k
  near 1 and tighter quotes.

**Validation against reality.** Over the 1000 evaluation episodes of #30, the
market maker's realised per-step fill frequency, binned by its actual quote
distance from the mid, is below the fitted curve everywhere: the ratio
realised/fitted is 0.855 at 1 tick, 0.709 at 1.5, 0.614 at 2, 0.484 at 2.5 and
0.509 at 3 (inventory strategy; the symmetric strategy gives 0.711 at 1.5 and
0.631 at 2). The realised curve is also steeper: an exponential fitted to it on
the same bins has k = 0.9938 per tick (A = 0.409, R^2 = 0.992). The mismatch
is a finding, not a failure. The most likely cause is queue position: the
market maker's quotes are on average wider than the background spread (#25),
so they often sit behind background orders at the same price, and the further
out the quote, the more likely there is a queue ahead of it. Figure
`fig4_calibration.png` shows the three curves.

### #29. The pure model reproduces the paper, and one difference in the benchmark

**Choice.** `mm/pure_model.py` simulates the model with no book: fills are
Bernoulli with probability `A * exp(-k * delta) * dt` and the mid moves by
`+/- sigma * sqrt(dt)`. It is run with the paper's parameters (s0 = 100, T = 1,
sigma = 2, dt = 0.005, k = 1.5, A = 140) on 20,000 paths per row, and compared
with Tables 1 to 3 of Avellaneda and Stoikov (2008), Quantitative Finance 8(3),
pages 222 to 223, read from the author-hosted copy at
https://math.nyu.edu/~avellane/HighFrequencyTrading.pdf (1000 paths each).

| gamma | strategy | spread (ours / paper) | profit | std profit | std final q |
|---|---|---|---|---|---|
| 0.1 | inventory | 1.49 / 1.49 | 64.90 / 65.0 | 6.54 / 6.6 | 2.91 / 2.9 |
| 0.1 | symmetric | 1.49 / 1.49 | 67.96 / 68.4 | 13.12 / 12.7 | 8.42 / 8.4 |
| 0.01 | inventory | 1.35 / 1.35 | 68.43 / 68.6 | 8.96 / 8.7 | 5.17 / 5.1 |
| 0.01 | symmetric | 1.35 / 1.35 | 68.60 / 68.8 | 13.55 / 12.8 | 8.73 / 8.7 |
| 1.0 | inventory | 3.03 / 3.02 | 31.49 / 31.4 | 4.84 / 5.0 | 1.63 / 1.7 |
| 1.0 | symmetric | 3.03 / 3.02 | 43.99 / 44.0 | 9.60 / 11.0 | 5.83 / 5.1 |

Everything agrees to within the paper's sampling error except the gamma = 1
symmetric row. The paper's text explains why: its symmetric strategy quotes a
*constant* spread equal to the inventory strategy's time-average, whereas
`symmetric_quotes` in this project uses the inventory strategy's
*time-varying* spread, centred on the mid. At gamma = 1 the risk term
gamma * sigma^2 * (T - t) is the largest part of the spread and falls to zero
over the episode, so the two benchmarks differ most there. Running
the paper's constant-spread version (`pure_symmetric_constant_spread` in
`mm/analysis.py`) gives a std of profit of 10.65 and a std of final inventory
of 5.15, matching the paper's 11.0 and 5.1. At gamma = 0.1 the two versions
barely differ (13.26 vs 13.12 for std of profit).

**Alternatives.** Change `symmetric_quotes` to the constant-spread version.

**Trade-off.** The time-varying version is kept: it isolates the inventory
skew exactly, because both strategies quote the same spread at every instant.
It is not the paper's benchmark, though, and its docstring and the README say
so: "the same spread as the inventory strategy, centred on the mid".

### #30. Results: inventory strategy vs symmetric in the order-book simulator

1000 shared tapes (seeds 0 to 999), default flow, quote size 5,
gamma = 0.00046875, sigma = 17.8885, k = 0.7094.

| | inventory (Avellaneda-Stoikov) | symmetric |
|---|---|---|
| terminal P&L, mean (s.e.) | 1234.9 (5.5) | 1491.3 (25.0) |
| terminal P&L, std [95% bootstrap CI] | 173.4 [165.4, 181.3] | 790.9 [739.5, 839.5] |
| mean / std [95% CI] | 7.122 [6.81, 7.471] | 1.885 [1.76, 2.034] |
| 5th percentile of P&L [95% CI] | 963.0 [915.5, 981.9] | 229.4 [138.4, 364.5] |
| fraction of losing episodes | 0.0 | 0.031 |
| terminal inventory std (lots) | 16.1 | 64.1 |
| time-average abs(inventory), mean (s.e.) | 4.47 (0.02) | 33.78 (0.61) |
| max abs(inventory) per episode, mean (s.e.) | 22.3 (0.2) | 76.7 (1.0) |
| max abs(inventory), worst episode | 58 | 208 |
| fills per episode | 275.2 | 315.8 |
| filled lots per episode | 926.6 | 1043.8 |
| average quoted spread (ticks) | 3.872 | 3.481 |
| spread P&L vs book mid, mean (s.e.) | 1088.6 (4.1) | 1239.2 (4.3) |
| inventory P&L vs book mid, mean (s.e.) | 146.4 (3.6) | 252.1 (24.6) |

Paired on the same tape, inventory minus symmetric P&L is -256.4 (s.e. 24.0,
95% CI [-303.3, -209.4]); the inventory strategy does better on 0.309 of tapes.

Reading:
- **The skew trades mean for risk, as the theory says.** The inventory strategy
  earns less on average (the paired difference is clearly negative) but its
  P&L std is 173.4 against 790.9, its mean/std ratio is 7.122 against 1.885, its 5th percentile is 963.0 against 229.4,
  and it never lost money in 1000 episodes. The symmetric strategy earns more
  partly because it quotes a narrower average spread (#25) and fills more, and
  partly because it carries large inventory in a market where carrying
  inventory happened to pay (next point).
- **Positive inventory P&L is a marking effect, not skill.** Both strategies
  show positive mid-marked inventory P&L. #31 traces this to temporary price
  impact and reversion of the book mid; marked at the fundamental it vanishes.
- **This is the same ordering as in the paper** (the symmetric strategy has the
  higher mean and much higher variance), but the magnitudes differ completely
  from what the pure model predicts for this market (#32).

### #31. Adverse selection: why the market maker wins here, and where it stops

A surprise flagged during development: inventory P&L marked at
the book mid averages positive for both strategies, which suggests little or no
net adverse selection. The proposed but unverified explanation was temporary
price impact of uninformed market orders followed by reversion of the book mid
towards the fundamental. The evidence below confirms it, with one refinement:
adverse selection does exist, from a small minority of fills, and is outweighed
at the default volatility.

**Method.** For each fill at step s, with sign +1 for the market maker's buys
and -1 for its sells, the *markout* at horizon h is
`sign * (price[s + h] - price[s])`, quantity-weighted, using either the book mid
or the fundamental as the price. Positive means the price moved in the market
maker's favour after it traded. Each fill's edge against the fundamental also
splits exactly into three signed pieces: its quoted edge against the mid the
quote was set against (the mid at the end of step s - 1), plus the move of the
book mid during the fill step, plus the gap between the fundamental and the mid
at the close of the fill step. Each fill is also classified from the tape by
what could have taken it: a market order, a background limit order priced
through the quote ("crossing"), or "either" when both were present in that
step.

**Default volatility (0.4 ticks per step), inventory strategy, 1000 episodes:**

| taker | share of lots | quoted edge | mid move in fill step | fundamental minus mid | mid markout h = 1, 5, 20, 100 |
|---|---|---|---|---|---|
| all | 1.0000 | 1.708 | -0.534 | 0.162 | 0.029, 0.090, 0.130, 0.154 |
| market order | 0.9113 | 1.731 | -0.474 | 0.380 | 0.046, 0.160, 0.294, 0.369 |
| crossing limit | 0.0678 | 1.466 | -1.150 | -2.089 | -0.141, -0.606, -1.538, -2.049 |
| either | 0.0209 | 1.508 | -1.139 | -2.046 | -0.185, -0.721, -1.585, -1.995 |

Fundamental markouts are zero to within 0.005 ticks at every horizon for all
fills (0.000, 0.000, -0.001, -0.003), as they must be for a random walk. The
symmetric strategy shows the same pattern (market-order share 0.9272, mid
markout at h = 100 of 0.412 for those fills and -2.199 for crossing limit
orders).

What this shows:
- **Market orders (0.9113 of lots) are uninformed and push the mid.** When a
  market sell hits the bid, it also consumes background bids, so the book mid
  falls during the step (-0.474 ticks on average, signed against the market
  maker). The fundamental has not moved, so at the close of the step it sits
  0.380 ticks on the market maker's side of the mid. Over the next 100 steps
  the mid drifts back by 0.369 ticks: almost exactly the gap. That reversion
  is the positive mid-marked inventory P&L.
- **Crossing limit orders (0.0678 of lots) are informed, in the only sense the
  simulator allows.** They exist because the fundamental has moved through the
  market maker's stale quote. At the close of the step the fundamental is 2.089
  ticks against the market maker, and over 100 steps the book mid follows it
  (-2.049). That is genuine adverse selection, caused by the one-step lag.
- **Marked at the fundamental, all the profit is spread.** Spread P&L at the
  fundamental is 1238.3 (s.e. 4.9) and inventory P&L -3.0 (s.e. 3.5) for the
  inventory strategy; 1481.5 (s.e. 5.7) and 10.3 (s.e. 24.7) for the
  symmetric strategy. The mid-marked split (1088.6 and 146.4) moves profit
  from "spread" to "inventory" because it measures the edge against a mid that
  the trade itself has just displaced.

**Volatility sweep.** FlowConfig.sigma is varied with everything else in the
flow fixed. For each value, k is re-calibrated from the book with no market
maker (same seeds and fit range as #28) and ASParams.sigma is set to
sigma * sqrt(2000); gamma is held at 0.00046875, as a fixed property of the
trader. 400 shared tapes per point (seeds 0 to 399, so the 0.4 row differs
slightly from #30).

| sigma (ticks/step) | k | inventory P&L mean (s.e.) | symmetric P&L mean (s.e.) | lots taken by crossing or "either" orders, inventory | inventory P&L at fundamental: spread, inventory |
|---|---|---|---|---|---|
| 0.2 | 0.7383 | 1438.6 (10.2) | 1528.6 (21.8) | 0.0194 | 1441.0, -0.0 |
| 0.4 | 0.7094 | 1235.4 (9.3) | 1491.0 (39.2) | 0.0896 | 1232.8, 3.6 |
| 0.8 | 0.5980 | 799.3 (9.4) | 1126.0 (80.0) | 0.2344 | 782.0, 14.2 |
| 1.2 | 0.4945 | 222.2 (10.7) | 655.8 (119.5) | 0.3245 | 211.2, 3.0 |
| 1.4 | 0.4520 | -111.1 (13.1) | 25.9 (176.5) | 0.3569 | -129.0, 5.1 |
| 1.6 | 0.4132 | -422.7 (14.1) | -291.9 (225.1) | 0.3809 | -431.5, -4.6 |
| 3.2 | 0.2440 | -2986.2 (35.3) | -2612.0 (501.2) | 0.4864 | -3018.8, -27.9 |

- **The market maker stops making money between 1.2 and 1.4 ticks per step.**
  The inventory strategy's mean P&L is significantly positive at 1.2 and
  significantly negative at 1.4. The symmetric strategy is significantly
  positive up to 1.2, indistinguishable from zero at 1.4 and 1.6, and
  significantly negative at 3.2. As volatility rises, the share of lots taken
  by crossing limit orders grows from 0.0194 to 0.4864 and the mid markouts
  turn negative (inventory strategy, h = 100: 0.269 at 0.2, -0.158 at 0.8,
  -1.395 at 3.2).
- **Re-calibration widens the quotes but not enough.** k falls as the book
  spreads out, so the quoted spread grows (inventory strategy: 3.697 ticks at
  0.2, 16.283 at 3.2), but losses still grow.
- **Marked at the book mid, the losses look like inventory losses; at the
  fundamental they are spread losses.** At sigma 3.2 the symmetric strategy's
  mid-marked split is spread 3126.9, inventory -5738.9, while at the
  fundamental it is spread -3007.1, inventory 280.6. The money is lost at the
  moment of the fill, against a mid that has not yet caught up with the
  fundamental. A desk reading only mid-marked attribution would draw the wrong
  conclusion.
- **Robustness to the gamma convention.** Holding gamma * sigma^2 fixed instead
  (the same skew in ticks at every volatility) moves the break-even for the
  inventory strategy: still significantly positive at 1.4 (325.8, s.e. 38.2),
  indistinguishable from zero at 1.6 (-75.4, s.e. 49.6), negative at 3.2.

**The default configuration is a benign market for a market maker.** A share
of 0.9113 of its volume is with uninformed market orders, the fundamental moves 0.4 ticks
per step against an average quoted spread of 3.872 ticks, and the
price impact of its counterparties reverts in its favour. The positive results
in #30 are a property of this flow, not evidence that the strategy would make
money in a real market. Figures: `fig7_volatility_sweep.png`,
`fig8_markouts.png`.

### #32. The order-book simulator against the pure model with matched parameters

The pure model is run with dt = 1 step, 2000 steps, sigma = 0.4 per step, the
calibrated A = 0.3734 and k = 0.7094, fill size 5 and the same gamma, on 20,000
paths.

| | inventory: book | inventory: pure | symmetric: book | symmetric: pure |
|---|---|---|---|---|
| P&L mean | 1234.9 | 3638.8 | 1491.3 | 3880.5 |
| P&L std | 173.4 | 214.1 | 790.9 | 1347.8 |
| terminal inventory std | 16.1 | 24.7 | 64.1 | 107.4 |
| fills per episode | 275.2 | 567.2 | 315.8 | 535.1 |
| filled lots per episode | 926.6 | 2836.0 | 1043.8 | 2675.3 |
| P&L per filled lot | 1.333 | 1.283 | 1.429 | 1.451 |
| average quoted spread | 3.872 | 2.893 | 3.481 | 2.893 |

The pure model overstates P&L by a wide margin, and almost all of it is
volume: P&L per filled lot is close in the two (1.333 vs 1.283 for the
inventory strategy). Following the inventory strategy's per-side, per-step fill
probability down the chain:
- the pure model, quoting at its continuous distances: 0.1418;
- the same calibrated curve, evaluated at the distances the book market maker
  actually quoted after outward rounding and the passivity clamp: 0.0996;
- what the book market maker actually got: 0.0652 (the queue-position and
  own-impact effects of #28);
- and when it does fill, it gets 3.367 lots per fill on average instead of 5,
  because many fills are partial.

What the pure model leaves out, and where it shows:
- *Discrete ticks and conservative rounding*: wider quotes, fewer fills
  (0.1418 to 0.0996 above), and more edge per fill.
- *Queue position*: fewer fills than the reach curve implies (0.0996 to 0.0652).
- *Partial fills*: 3.367 lots per fill rather than 5.
- *One-step lag and price impact*: the pure model's mid is independent of
  fills, so it has neither the adverse selection from crossing limit orders
  nor the favourable reversion after market orders. At the default volatility
  the two appear to roughly cancel, which would explain why P&L per lot agrees;
  #31 shows they do not cancel at higher volatility.
- *Passivity clamp*: in the book a heavily skewed quote cannot cross the
  background touch; in the pure model it can sit at or through the mid, where
  the fill probability is simply clipped at 1.

Lower volume in the book also explains the lower inventory std. Figure:
`fig5_pure_vs_book.png`.

### #33. Risk aversion, the end of the episode, and limitations

**Risk-aversion sweep** (inventory strategy, 500 shared tapes, seeds 0 to 499;
"skew per quote" is the reservation-price shift for 5 lots at t = 0):

| gamma / default | skew per quote (ticks) | P&L mean (s.e.) | P&L std | mean / std | terminal inventory std |
|---|---|---|---|---|---|
| 0.1 | 0.075 | 1423.7 (15.4) | 343.6 | 4.144 | 32.2 |
| 0.2 | 0.15 | 1384.7 (12.8) | 285.5 | 4.851 | 26.0 |
| 0.5 | 0.375 | 1316.9 (9.9) | 222.2 | 5.927 | 19.4 |
| 1 | 0.75 | 1233.6 (8.2) | 182.5 | 6.758 | 16.0 |
| 2 | 1.5 | 1096.0 (6.9) | 154.1 | 7.110 | 13.4 |
| 5 | 3.75 | 757.4 (5.0) | 112.9 | 6.712 | 10.4 |
| 10 | 7.5 | 313.3 (4.0) | 90.4 | 3.467 | 8.9 |
| symmetric | 0 | 1481.9 (34.6) | 772.7 | 1.918 | 62.3 |

Mean and std both fall monotonically as gamma rises: a clean risk/return
frontier with the symmetric strategy at its high-risk end. The mean/std ratio
peaks at 2x the default (7.110), so the default rule is close to but not at the
best ratio on this grid. Figure: `fig6_gamma_frontier.png`.

**Inventory over time.** The second surprise flagged during development: the inventory
strategy's inventory std grows near the end of the episode. Confirmed. The
cross-sectional std of inventory across the 1000 tapes of #30 is 3.59 lots at
step 99, 4.66 at step 999, 6.29 at step 1499, 9.32 at step 1799 and 16.07 at
the last step; its mean over steps 500 to 1499 is 4.82. The cause is in the
formula: the skew is `q * gamma * sigma^2 * (T - t)`, which falls linearly to
zero (0.7125 ticks per quote at step 99, 0.075 at step 1799, 0 at the end), so
inventory control switches off as the horizon approaches and inventory
diffuses freely. The terminal inventory std quoted in #30 (16.1) is therefore
mostly a measure of the last few hundred steps; during the bulk of the episode
the strategy holds inventory several times tighter. The symmetric strategy's
std grows throughout, to 64.12. Figure: `fig3_inventory_std_over_time.png`.

**Limitations that matter for any conclusion about real trading.**
- *Uninformed flow.* Market orders carry no information; the only informed
  flow is limit orders reacting to a fundamental move. Real order flow contains
  traders who know more than the market maker. #31 shows how quickly results
  turn when even this mechanical form of information becomes common.
- *No fees or rebates.* Real maker rebates and taker fees would shift every
  P&L number; nothing here models them.
- *No latency model beyond one step.* The market maker always reacts exactly
  one step late, and nobody is faster.
- *A single market maker with no competition.* The background never improves
  on the market maker's quotes or competes for queue priority; real market
  makers compete on price and speed.
- *The market maker knows sigma.* It is given the true volatility; k is
  calibrated, but from a book without it.
- *A finite horizon that switches off inventory control at the end* (above). A
  real desk running continuously would not want this; the infinite-horizon
  variants of the model avoid it.
- *Synthetic everything.* The flow parameters were tuned to give a stable book
  and a few hundred fills per episode, not fitted to any real market. The
  numbers describe this simulator; they are not estimates of what the strategy
  would earn anywhere real.
