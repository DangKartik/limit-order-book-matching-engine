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
