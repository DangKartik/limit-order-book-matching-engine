"""Monte Carlo of the pure Avellaneda-Stoikov model, with no order book.

Fills are drawn directly from the model's own assumption: in each time step a
quote at distance delta from the mid is filled with probability
A * exp(-k * delta) * dt. There is no queue, no tick size, no other
participants and no adverse selection: the mid moves independently of fills.

Two uses:
  1. Reproduce the paper's experiment, which checks the formulas and this code.
  2. Run it with parameters fitted to the order-book simulator, to see how far
     the idealised model is from a market where fills come from real matching.
"""
from dataclasses import dataclass

import numpy as np

from mm.avellaneda_stoikov import ASParams


@dataclass(frozen=True)
class PureModelResult:
    pnl: np.ndarray            # terminal profit per path
    inventory: np.ndarray      # terminal inventory per path
    mean_spread: float         # average quoted spread over the run
    mean_abs_inventory: float  # average |inventory| over time and paths
    fills_per_path: float


def simulate(params: ASParams, A: float, *, strategy: str, n_paths: int, n_steps: int,
             dt: float, s0: float = 100.0, fill_size: float = 1.0, seed: int = 0) -> PureModelResult:
    """Simulate `n_paths` independent paths at once using numpy arrays.

    strategy is "inventory" (Avellaneda-Stoikov) or "symmetric" (same spread
    centred on the mid). Time runs from 0 to T = n_steps * dt.
    """
    if strategy not in ("inventory", "symmetric"):
        raise ValueError(f"unknown strategy {strategy!r}")
    rng = np.random.default_rng(seed)
    horizon = n_steps * dt

    mid = np.full(n_paths, s0)
    inventory = np.zeros(n_paths)
    cash = np.zeros(n_paths)
    spread_sum = 0.0
    abs_inventory_sum = 0.0
    fills = 0

    for step in range(n_steps):
        time_left = horizon - step * dt
        risk = params.gamma * params.sigma ** 2 * time_left
        spread = risk + (2.0 / params.gamma) * np.log(1.0 + params.gamma / params.k)
        centre = mid - inventory * risk if strategy == "inventory" else mid
        bid = centre - spread / 2.0
        ask = centre + spread / 2.0

        # Fill probability over one step. Clipped at 1 because A * dt can exceed
        # 1 when a quote is at or through the mid; the model is a rate, the
        # simulation needs a probability.
        p_bid = np.clip(A * np.exp(-params.k * (mid - bid)) * dt, 0.0, 1.0)
        p_ask = np.clip(A * np.exp(-params.k * (ask - mid)) * dt, 0.0, 1.0)
        bid_hit = rng.random(n_paths) < p_bid
        ask_hit = rng.random(n_paths) < p_ask

        inventory += fill_size * (bid_hit.astype(float) - ask_hit.astype(float))
        cash += fill_size * (ask * ask_hit - bid * bid_hit)

        # The mid moves by +/- sigma * sqrt(dt), as in the paper's simulation.
        mid += params.sigma * np.sqrt(dt) * rng.choice((-1.0, 1.0), size=n_paths)

        spread_sum += spread
        abs_inventory_sum += np.abs(inventory).mean()
        fills += int(bid_hit.sum() + ask_hit.sum())

    return PureModelResult(
        pnl=cash + inventory * mid,   # inventory marked at the final mid
        inventory=inventory,
        mean_spread=spread_sum / n_steps,
        mean_abs_inventory=abs_inventory_sum / n_steps,
        fills_per_path=fills / n_paths,
    )
