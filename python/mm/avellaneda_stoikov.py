"""The Avellaneda-Stoikov (2008) quoting formulas.

Reference: M. Avellaneda and S. Stoikov, "High-frequency trading in a limit
order book", Quantitative Finance 8(3), 2008.

The model: a dealer quotes a bid and an ask around a mid price s that follows a
driftless random walk with volatility sigma. A quote at distance delta from the
mid is filled at rate  lambda(delta) = A * exp(-k * delta).  The dealer holds
inventory q, has risk aversion gamma, and cares about wealth at a horizon T.

Two results come out of the paper's approximation:

  reservation price   r = s - q * gamma * sigma^2 * (T - t)
      The price at which the dealer is indifferent between buying and selling.
      Long inventory (q > 0) pushes r BELOW the mid, so both quotes shift down:
      the ask becomes more attractive, the bid less, and inventory mean-reverts.

  total spread        gamma * sigma^2 * (T - t) + (2 / gamma) * ln(1 + gamma / k)
      The first term is compensation for inventory risk over the remaining
      time. The second depends on how fast fills die off with distance (k): a
      steep fill curve (large k) means a narrow spread.

The quotes are r -/+ spread / 2. Note that A does not appear: the level of
order flow scales how often you trade, not where you should quote.

Units are whatever the caller uses consistently. In this project prices are in
ticks, time is in fractions of an episode (so T = 1), sigma is in ticks per
square-root-episode, and inventory is in units of quantity.
"""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ASParams:
    gamma: float   # risk aversion: larger means inventory is penalised more
    sigma: float   # volatility of the mid, per square root of one unit of time
    k: float       # decay of fill intensity with distance from the mid, per tick


def reservation_price(mid: float, inventory: float, time_left: float, p: ASParams) -> float:
    return mid - inventory * p.gamma * p.sigma ** 2 * time_left


def optimal_spread(time_left: float, p: ASParams) -> float:
    """Total bid-ask spread (ask minus bid)."""
    return p.gamma * p.sigma ** 2 * time_left + (2.0 / p.gamma) * math.log(1.0 + p.gamma / p.k)


def inventory_quotes(mid: float, inventory: float, time_left: float, p: ASParams) -> tuple[float, float]:
    """The Avellaneda-Stoikov strategy: quotes centred on the reservation price."""
    r = reservation_price(mid, inventory, time_left, p)
    half = optimal_spread(time_left, p) / 2.0
    return r - half, r + half


def symmetric_quotes(mid: float, inventory: float, time_left: float, p: ASParams) -> tuple[float, float]:
    """The benchmark: the same spread, centred on the mid, ignoring inventory.

    Comparing against this isolates the effect of the inventory skew, because
    the spread at every moment is identical. The paper's own symmetric
    benchmark is slightly different: it uses one constant spread, the
    inventory strategy's time-average. DESIGN_NOTES #29 measures the gap.
    """
    half = optimal_spread(time_left, p) / 2.0
    return mid - half, mid + half
