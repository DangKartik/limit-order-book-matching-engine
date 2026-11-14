"""Background order flow: an exogenous tape of orders sent into the real book.

The market is a crowd of anonymous traders around a hidden "fundamental" price
that follows a random walk. Each step some of them post limit orders a few
ticks either side of the fundamental, some of those orders are later
cancelled, and a few send market orders. The market maker never sees the
fundamental; it only sees the book that this flow builds.

Background limit prices are set relative to the FUNDAMENTAL, not to the book.
When the fundamental moves, new orders are priced through quotes that are now
stale (including the market maker's) and trade against them. That is where
adverse selection comes from in this simulator: nobody scripts it, it falls out
of the matching engine.

The whole tape is drawn up front and does not depend on the book or on any
strategy. Two strategies run on the same seed therefore face exactly the same
incoming orders (common random numbers), and only the matching outcomes
differ. That makes comparisons between strategies far less noisy than running
them on independent seeds.
"""
from dataclasses import dataclass

import numpy as np

from pylob import Side


@dataclass(frozen=True)
class FlowConfig:
    # Defaults tuned by running episodes (see the stage-4 notes): they give a
    # background spread of about 3 ticks, a book that never empties, and a
    # market maker quoting 5 lots gets a few hundred fills per episode. The
    # wide mean_offset keeps depth a few ticks out, so market orders rarely
    # sweep a side empty; the lifetime keeps the touch from filling in to 1 tick.
    n_steps: int = 2000           # measured steps (the market maker's episode)
    warmup_steps: int = 200       # steps before measuring, so the book fills up first
    start_price: int = 10_000
    sigma: float = 0.4            # std-dev of the fundamental's move per step, in ticks
    limit_rate: float = 1.0       # expected background limit orders per side per step
    mean_offset: float = 8.0      # mean distance (ticks) of a limit order from the fundamental
    limit_size_max: int = 10      # limit sizes are uniform on 1..limit_size_max
    mean_lifetime: float = 30.0   # mean steps before a background limit order is cancelled
    market_rate: float = 0.4      # expected market orders per step (either side)
    market_size_max: int = 10     # market sizes are uniform on 1..market_size_max
    min_price: int = 1
    max_price: int = 20_000

    @property
    def total_steps(self) -> int:
        return self.warmup_steps + self.n_steps


# (side, price, qty, cancel_step) and (side, qty). Plain tuples of Python ints,
# because the simulation loop is Python and indexing numpy scalars there is slow.
LimitOrder = tuple[Side, int, int, int]
MarketOrder = tuple[Side, int]


@dataclass(frozen=True)
class Tape:
    fundamental: list[float]          # fundamental price after each step's move
    limits: list[list[LimitOrder]]    # background limit orders submitted at each step
    markets: list[list[MarketOrder]]  # market orders submitted at each step (after the limits)


def _per_step(step_of: list[int], items: list, total_steps: int) -> list[list]:
    out: list[list] = [[] for _ in range(total_steps)]
    for step, item in zip(step_of, items):
        out[step].append(item)
    return out


def generate_tape(config: FlowConfig, seed: int) -> Tape:
    """Draw every background order for every step, using only `seed`."""
    rng = np.random.default_rng(seed)
    total = config.total_steps
    fundamental = config.start_price + np.cumsum(rng.normal(0.0, config.sigma, total))

    # One Poisson count of limit orders per step with a random side is the same
    # distribution as two independent Poisson counts, one per side (Poisson
    # splitting), and it also gives a random buy/sell interleaving within a step.
    counts = rng.poisson(2.0 * config.limit_rate, total)
    step_of = np.repeat(np.arange(total), counts)
    n = len(step_of)
    is_buy = rng.random(n) < 0.5
    # numpy's geometric has support 1, 2, ... and mean 1/p, i.e. "1 + a geometric
    # on 0, 1, ...". Never 0, so a new order never sits exactly at the fundamental.
    distance = rng.geometric(1.0 / config.mean_offset, n)
    qty = rng.integers(1, config.limit_size_max + 1, n)
    lifetime = np.maximum(1, np.ceil(rng.exponential(config.mean_lifetime, n))).astype(np.int64)
    anchor = np.rint(fundamental[step_of])
    price = np.where(is_buy, anchor - distance, anchor + distance)
    price = np.clip(price, config.min_price, config.max_price).astype(np.int64)
    cancel_step = step_of + lifetime   # may be past the end: then it is simply never cancelled

    sides = [Side.Buy if b else Side.Sell for b in is_buy.tolist()]
    limit_orders = list(zip(sides, price.tolist(), qty.tolist(), cancel_step.tolist()))

    m_counts = rng.poisson(config.market_rate, total)
    m_step_of = np.repeat(np.arange(total), m_counts)
    m = len(m_step_of)
    m_sides = [Side.Buy if b else Side.Sell for b in (rng.random(m) < 0.5).tolist()]
    m_qty = rng.integers(1, config.market_size_max + 1, m)
    market_orders = list(zip(m_sides, m_qty.tolist()))

    return Tape(
        fundamental=fundamental.tolist(),
        limits=_per_step(step_of.tolist(), limit_orders, total),
        markets=_per_step(m_step_of.tolist(), market_orders, total),
    )
