"""Measurement helpers for the stage-4 analysis (python/run_analysis.py).

Nothing here changes how an episode runs. These functions only read the
outputs of mm.simulate.run_episode (and the tape it was given) and reduce
them to numbers: fill-curve calibration, P&L attribution against any price
path, markouts, and a compact per-episode summary that is cheap to send back
from a worker process.

Conventions follow mm.simulate: index s is a measured step, mid[s] is the
background mid at the END of step s, a fill stamped s happened during step s,
and the quote that was hit was posted at the end of step s - 1.
"""
import math
from dataclasses import dataclass

import numpy as np

from pylob import Side

from mm.avellaneda_stoikov import ASParams
from mm.flow import FlowConfig, Tape, generate_tape
from mm.simulate import EpisodeResult, run_episode

# ---------------------------------------------------------------------------
# Fill-curve calibration
# ---------------------------------------------------------------------------


def reach_probability(reach: np.ndarray, deltas: np.ndarray) -> np.ndarray:
    """Per-step probability that aggressive flow reached at least `delta` ticks.

    `reach` holds one value per step (NaN when no aggressive order arrived on
    that side). A NaN step counts as "not reached": it is a step on which a
    quote at any distance would not have been hit, so it belongs in the
    denominator.
    """
    filled = np.where(np.isnan(reach), -np.inf, reach)
    return np.array([(filled >= d).mean() for d in deltas])


@dataclass(frozen=True)
class ExpFit:
    A: float              # intensity at distance 0, per step
    k: float              # decay per tick
    r2: float             # R^2 of the straight-line fit to log(probability)
    max_log_residual: float   # largest |log(observed) - log(fitted)|: 0.1 is roughly a 10% miss


def fit_exponential(deltas: np.ndarray, prob: np.ndarray) -> ExpFit:
    """Least-squares fit of log(prob) = log(A) - k * delta."""
    deltas, prob = np.asarray(deltas, float), np.asarray(prob, float)
    if np.any(prob <= 0):
        raise ValueError("probabilities must be positive to take logs")
    y = np.log(prob)
    slope, intercept = np.polyfit(deltas, y, 1)
    residual = y - (intercept + slope * deltas)
    r2 = 1.0 - residual.var() / y.var() if y.var() > 0 else 1.0
    return ExpFit(A=math.exp(intercept), k=-slope, r2=float(r2),
                  max_log_residual=float(np.abs(residual).max()))


def quote_distance_and_fill(result: EpisodeResult) -> tuple[np.ndarray, np.ndarray]:
    """For every quote and every step it was exposed: its distance from the mid, and whether it filled.

    The quote posted at the end of step s faces step s + 1's flow, and its
    distance is measured from mid[s], the mid it was quoted around (the same
    reference the calibration uses: the mid at the start of the step).
    Bids and asks are pooled. "Filled" means at least one fill of any size.
    """
    n = len(result.mid)
    hit = {Side.Buy: np.zeros(n, bool), Side.Sell: np.zeros(n, bool)}
    for step, side, _, _ in result.fills:
        hit[side][step] = True
    bid_dist = result.mid[:-1] - result.bid_quote[:-1]
    ask_dist = result.ask_quote[:-1] - result.mid[:-1]
    dist = np.concatenate((bid_dist, ask_dist))
    filled = np.concatenate((hit[Side.Buy][1:], hit[Side.Sell][1:]))
    keep = ~np.isnan(dist)
    return dist[keep], filled[keep]


# ---------------------------------------------------------------------------
# P&L attribution and markouts
# ---------------------------------------------------------------------------

def _sign(side: Side) -> int:
    return 1 if side == Side.Buy else -1


def attribution(result: EpisodeResult, mark: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split the step-by-step change in (cash + inventory * mark) into spread and inventory parts.

    Same identity as mm.simulate, but against any price path: with
    mark = result.mid it reproduces result.spread_pnl and result.inventory_pnl;
    with mark = result.fundamental it values everything at the hidden "true"
    price instead of the book mid.
      inventory part at step s: inventory carried in from s - 1, times mark[s] - mark[s - 1]
      spread part at step s:    each fill's edge against mark[s], times its quantity
    """
    mark = np.asarray(mark, float)
    previous = np.concatenate(([0], result.inventory[:-1]))
    inventory_part = previous * np.diff(mark, prepend=mark[0])
    spread_part = np.zeros(len(mark))
    for step, side, price, qty in result.fills:
        spread_part[step] += _sign(side) * (mark[step] - price) * qty
    return spread_part, inventory_part


def markouts(fills: list[tuple[int, Side, int, int]], path: np.ndarray,
             horizons: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Quantity-weighted sums of the signed move of `path` over h steps after each fill.

    For a fill at step s the move is  sign * (path[s + h] - path[s]),  with
    sign +1 for our buys and -1 for our sells. Positive means the price went
    our way after we traded (good); negative is adverse selection. A fill
    whose horizon runs past the end of the episode is left out at that
    horizon rather than truncated, so every horizon averages complete moves.

    Returns (sum of qty * move, sum of qty) per horizon; divide for the mean.
    """
    path = np.asarray(path, float)
    n = len(path)
    total = np.zeros(len(horizons))
    weight = np.zeros(len(horizons))
    for step, side, _, qty in fills:
        for j, h in enumerate(horizons):
            if step + h < n:
                total[j] += _sign(side) * (path[step + h] - path[step]) * qty
                weight[j] += qty
    return total, weight


TAKER_KINDS = ("market", "limit", "either")


def taker_kind(tape: Tape, config: FlowConfig, fill: tuple[int, Side, int, int]) -> str:
    """What kind of background order could have traded against this fill.

    Read off the tape: a fill on our bid at price p was taken by a seller, which
    in that step was either a market sell or a background limit sell priced at
    or below p (one that crossed). If only one of the two was present in the
    step, the taker is known; if both were, it is reported as "either".
    """
    step, side, price, _ = fill
    t = step + config.warmup_steps
    taker = Side.Sell if side == Side.Buy else Side.Buy
    market = any(s == taker for s, _ in tape.markets[t])
    if taker == Side.Sell:
        limit = any(s == taker and p <= price for s, p, _, _ in tape.limits[t])
    else:
        limit = any(s == taker and p >= price for s, p, _, _ in tape.limits[t])
    if market and limit:
        return "either"
    if market:
        return "market"
    if limit:
        return "limit"
    raise RuntimeError(f"fill {fill} has no possible taker on the tape")


# ---------------------------------------------------------------------------
# One episode reduced to a summary
# ---------------------------------------------------------------------------

MARKOUT_HORIZONS = [0, 1, 2, 5, 10, 20, 50, 100]
DISTANCE_BINS = np.arange(0.0, 6.01, 0.5)   # quote distances are multiples of 0.5 tick


def summarise_episode(result: EpisodeResult, tape: Tape, config: FlowConfig) -> dict:
    """Everything the analysis needs from one episode, as plain numbers and small arrays."""
    inv = result.inventory
    spread_f, inv_f = attribution(result, result.fundamental)
    out = {
        "pnl": float(result.pnl[-1]),
        "spread_pnl": float(result.spread_pnl.sum()),
        "inventory_pnl": float(result.inventory_pnl.sum()),
        "pnl_fund": float(spread_f.sum() + inv_f.sum()),
        "spread_pnl_fund": float(spread_f.sum()),
        "inventory_pnl_fund": float(inv_f.sum()),
        "terminal_inventory": int(inv[-1]),
        "mean_abs_inventory": float(np.abs(inv).mean()),
        "max_abs_inventory": int(np.abs(inv).max()),
        "n_fills": len(result.fills),
        "filled_qty": int(sum(q for _, _, _, q in result.fills)),
        "mean_quoted_spread": float(np.nanmean(result.ask_quote - result.bid_quote)),
        "mean_background_spread": float(np.nanmean(result.background_spread)),
        "inventory_path": inv.astype(np.int32),
    }

    # Markouts, overall and by taker kind. Also three per-fill quantities that
    # decompose the edge: quoted edge vs the mid the quote was set against
    # (mid[s-1]), the move of the mid during the fill step (mid[s] - mid[s-1]),
    # and where the fundamental stood relative to the mid at the close of the
    # fill step (F[s] - mid[s]). All signed so that positive is good for us.
    groups: dict[str, list] = {"all": list(result.fills)}
    for kind in TAKER_KINDS:
        groups[kind] = []
    for fill in result.fills:
        groups[taker_kind(tape, config, fill)].append(fill)
    mid, fund = result.mid, result.fundamental
    per_group = {}
    for name, fills in groups.items():
        mk_mid, w = markouts(fills, mid, MARKOUT_HORIZONS)
        mk_fund, _ = markouts(fills, fund, MARKOUT_HORIZONS)
        qty = edge = impact = gap = 0.0
        for step, side, price, q in fills:
            sg = _sign(side)
            before = mid[step - 1] if step > 0 else mid[0]
            qty += q
            edge += sg * (before - price) * q
            impact += sg * (mid[step] - before) * q
            gap += sg * (fund[step] - mid[step]) * q
        per_group[name] = {"mid": mk_mid, "fund": mk_fund, "weight": w,
                           "qty": qty, "edge": edge, "impact": impact, "gap": gap,
                           "n": len(fills)}
    out["markouts"] = per_group

    # Realised fill frequency by quoted distance (for validating the calibration).
    dist, filled = quote_distance_and_fill(result)
    idx = np.searchsorted(DISTANCE_BINS, dist)   # exact multiples of 0.5 land on their own bin
    valid = (idx < len(DISTANCE_BINS)) & np.isclose(DISTANCE_BINS[np.minimum(idx, len(DISTANCE_BINS) - 1)], dist)
    out["quotes_at_distance"] = np.bincount(idx[valid], minlength=len(DISTANCE_BINS))
    out["fills_at_distance"] = np.bincount(idx[valid], weights=filled[valid], minlength=len(DISTANCE_BINS))
    return out


def run_and_summarise(job: tuple) -> dict:
    """Worker entry point: (config, seed, quoter, params, quote_size) -> summary dict."""
    config, seed, quoter, params, quote_size = job
    tape = generate_tape(config, seed)
    return summarise_episode(run_episode(tape, config, quoter, params, quote_size), tape, config)


def calibration_reach(job: tuple) -> np.ndarray:
    """Worker entry point: (config, seed) -> reach values of both sides, pooled."""
    config, seed = job
    result = run_episode(generate_tape(config, seed), config)
    return np.concatenate((result.reach_buy, result.reach_sell))


def pure_model_job(job: tuple):
    """Worker entry point for mm.pure_model.simulate: (params, A, keyword arguments)."""
    from mm.pure_model import simulate
    params, A, kwargs = job
    return simulate(params, A, **kwargs)


def pure_symmetric_constant_spread(params: ASParams, A: float, *, n_paths: int, n_steps: int,
                                   dt: float, s0: float = 100.0, seed: int = 0) -> tuple[float, np.ndarray, np.ndarray]:
    """The paper's own symmetric benchmark, which mm.pure_model does not implement.

    The paper's 'symmetric' strategy quotes a CONSTANT spread equal to the
    inventory strategy's time-average spread, centred on the mid.
    mm.avellaneda_stoikov.symmetric_quotes instead uses the same time-varying
    spread as the inventory strategy. Otherwise this is the same loop as
    mm.pure_model.simulate. Returns (constant spread, terminal pnl, terminal inventory).
    """
    rng = np.random.default_rng(seed)
    horizon = n_steps * dt
    spreads = [params.gamma * params.sigma ** 2 * (horizon - i * dt)
               + (2.0 / params.gamma) * math.log(1.0 + params.gamma / params.k) for i in range(n_steps)]
    half = float(np.mean(spreads)) / 2.0
    p_fill = min(1.0, A * math.exp(-params.k * half) * dt)
    mid = np.full(n_paths, s0)
    inventory = np.zeros(n_paths)
    cash = np.zeros(n_paths)
    for _ in range(n_steps):
        bid_hit = rng.random(n_paths) < p_fill
        ask_hit = rng.random(n_paths) < p_fill
        inventory += bid_hit.astype(float) - ask_hit.astype(float)
        cash += (mid + half) * ask_hit - (mid - half) * bid_hit
        mid += params.sigma * np.sqrt(dt) * rng.choice((-1.0, 1.0), size=n_paths)
    return 2.0 * half, cash + inventory * mid, inventory


def episode_params(config: FlowConfig, gamma: float, k: float) -> ASParams:
    """ASParams in the units the simulator uses: time in episodes (T = 1), prices in ticks.

    The fundamental moves config.sigma ticks per step, so over one episode of
    n_steps its standard deviation is sigma * sqrt(n_steps).
    """
    return ASParams(gamma=gamma, sigma=config.sigma * math.sqrt(config.n_steps), k=k)
