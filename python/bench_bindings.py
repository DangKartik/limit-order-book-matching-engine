"""Measure what one engine call costs when made from Python.

The C++ benchmark (bench/) measures the engine itself. This measures the
engine plus the Python call, argument conversion and result objects, which is
what the simulator in stage 4 actually pays per order.

Run:  .venv/bin/python python/bench_bindings.py
"""
import random
import time

import pylob
from pylob import Side


def run(book_cls, n=1_000_000, seed=1):
    rng = random.Random(seed)
    # Build the inputs first so the random number generator is not timed.
    ops = []
    for _ in range(n):
        side = Side.Buy if rng.random() < 0.5 else Side.Sell
        offset = rng.randint(1, 20)
        price = 10_000 - offset if side == Side.Buy else 10_000 + offset
        if rng.random() < 0.1:   # 10% cross the spread and trade
            price = 10_000 + offset if side == Side.Buy else 10_000 - offset
        ops.append((side, price, rng.randint(1, 100)))

    book = book_cls(min_price=1, max_price=20_000)
    add_limit, cancel = book.add_limit, book.cancel   # skip the attribute lookup in the loop
    trades = 0
    start = time.perf_counter()
    for i, (side, price, qty) in enumerate(ops):
        result, fills = add_limit(side, price, qty)
        trades += len(fills)
        if i % 2 and result.resting:   # also exercise cancel: every other order, if it rested
            cancel(result.id)
    elapsed = time.perf_counter() - start
    return elapsed, trades, len(book)


if __name__ == "__main__":
    for cls in (pylob.OrderBook, pylob.BaselineOrderBook):
        best = min(run(cls) for _ in range(5))
        elapsed, trades, resting = best
        calls = 1_000_000
        print(f"{cls.__name__:18s} {elapsed * 1e9 / calls:7.0f} ns per loop iteration "
              f"(best of 5; {trades} trades, {resting} resting at end)")
