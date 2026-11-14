"""Stage-4 analysis: the Avellaneda-Stoikov market maker in the order-book simulator.

Run from the repository root:

    PYTHONPATH=python .venv/bin/python python/run_analysis.py

Everything is driven by fixed seeds, so a re-run reproduces every number. The
script prints a report and writes into results/mm/:
    summary.json   every number the report (and DESIGN_NOTES) quotes
    report.txt     the printed report
    *.png          the figures

Experiments, in order:
  1. pure model: reproduce the paper's simulation (checks formulas and Monte Carlo)
  2. calibrate the fill curve lambda(delta) = A exp(-k delta) from the book, no market maker
  3. main comparison in the book: inventory (Avellaneda-Stoikov) vs symmetric, shared tapes
     (2b: validate the calibrated curve against the market maker's real fill rates)
  4. book vs pure model with matched parameters
  5. adverse selection: markouts, attribution at the fundamental, volatility sweep
  6. risk-aversion sweep
  7. inventory dispersion over the episode

Units, as in mm/: prices in ticks, quantities in lots, P&L in ticks x lots.
Time is in episodes (T = 1) for the market maker's formulas; one step is
1 / n_steps of an episode.
"""
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pylob import Side  # noqa: E402

from mm.analysis import (  # noqa: E402
    DISTANCE_BINS, MARKOUT_HORIZONS, TAKER_KINDS, ExpFit, calibration_reach, episode_params,
    fit_exponential, pure_model_job, pure_symmetric_constant_spread, reach_probability, run_and_summarise)
from mm.avellaneda_stoikov import ASParams, inventory_quotes, symmetric_quotes  # noqa: E402
from mm.flow import FlowConfig, generate_tape  # noqa: E402
from mm.simulate import run_episode  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "results" / "mm"

# --- Fixed choices -----------------------------------------------------------
QUOTE_SIZE = 5
SEEDS_MAIN = range(0, 1000)           # experiment 3 (and 5, 7, which reuse its episodes)
SEEDS_CALIBRATION = range(10_000, 10_200)   # disjoint from every evaluation seed
SEEDS_SWEEP = range(0, 400)           # volatility sweep, per point
SEEDS_GAMMA = range(0, 500)           # risk-aversion sweep, per point
PURE_PATHS = 20_000
FIT_RANGE = (1.0, 3.0)                # distances (ticks) the market maker actually quotes at
TAIL_RANGE = (3.0, 8.0)               # reported for context only
CALIBRATION_GRID = np.arange(0.5, 8.01, 0.5)
REPORT_HORIZONS = [1, 5, 20, 100]
SKEW_PER_QUOTE = 0.75   # ticks: reservation-price shift for one quote's worth of inventory at t = 0
VOL_SWEEP = [0.2, 0.4, 0.8, 1.2, 1.4, 1.6, 3.2]       # fundamental std-dev per step, ticks
GAMMA_MULTIPLIERS = [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
STRATEGIES = {"inventory": inventory_quotes, "symmetric": symmetric_quotes}

# Avellaneda & Stoikov (2008), Quantitative Finance 8(3), p. 222-223, Tables 1-3
# (1000 simulations each; s=100, T=1, sigma=2, dt=0.005, k=1.5, A=140). Read from
# the author-hosted copy of the published paper,
# https://math.nyu.edu/~avellane/HighFrequencyTrading.pdf
PAPER = {
    "source": "Avellaneda & Stoikov (2008), Quantitative Finance 8(3), Tables 1-3, p. 222-223; "
              "https://math.nyu.edu/~avellane/HighFrequencyTrading.pdf",
    "0.1": {"inventory": {"spread": 1.49, "profit": 65.0, "std_profit": 6.6, "final_q": 0.08, "std_q": 2.9},
            "symmetric": {"spread": 1.49, "profit": 68.4, "std_profit": 12.7, "final_q": 0.26, "std_q": 8.4}},
    "0.01": {"inventory": {"spread": 1.35, "profit": 68.6, "std_profit": 8.7, "final_q": 0.12, "std_q": 5.1},
             "symmetric": {"spread": 1.35, "profit": 68.8, "std_profit": 12.8, "final_q": 0.09, "std_q": 8.7}},
    "1.0": {"inventory": {"spread": 3.02, "profit": 31.4, "std_profit": 5.0, "final_q": 0.02, "std_q": 1.7},
            "symmetric": {"spread": 3.02, "profit": 44.0, "std_profit": 11.0, "final_q": 0.00, "std_q": 5.1}},
}

# --- Plot style (one colour per strategy, used in every figure) ----------------
COLOR = {"inventory": "#2a78d6", "symmetric": "#eb6834"}
ACCENT = "#1baf7a"     # fundamental / calibration data
VIOLET = "#6250d6"     # second non-strategy series
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 150, "figure.facecolor": "white", "axes.facecolor": "white",
    "savefig.facecolor": "white", "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "axes.edgecolor": "#c3c2b7", "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    "lines.linewidth": 1.6, "axes.titlelocation": "left", "axes.titlecolor": INK,
})

LINES: list[str] = []
POOL: ProcessPoolExecutor | None = None


def say(text: str = "") -> None:
    print(text, flush=True)
    LINES.append(text)


def r(x: float, digits: int) -> float:
    """Round for the summary. The report prints these exact values, so quotes match the JSON."""
    return float(round(float(x), digits))


def mean_se(values) -> tuple[float, float]:
    v = np.asarray(values, float)
    return float(v.mean()), float(v.std(ddof=1) / math.sqrt(len(v)))


def bootstrap_ci(values, statistic, n_resamples: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% percentile-bootstrap interval for statistic(values)."""
    v = np.asarray(values, float)
    rng = np.random.default_rng(seed)
    stats = [statistic(v[rng.integers(0, len(v), len(v))]) for _ in range(n_resamples)]
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def run_strategy(config: FlowConfig, params: ASParams, strategy: str, seeds) -> list[dict]:
    jobs = [(config, s, STRATEGIES[strategy], params, QUOTE_SIZE) for s in seeds]
    return list(POOL.map(run_and_summarise, jobs, chunksize=8))


def default_gamma(config: FlowConfig) -> float:
    """gamma such that one quote's worth of inventory moves the reservation price by
    SKEW_PER_QUOTE ticks at the start of the episode: q * gamma * sigma^2 * T = 0.75."""
    sigma = episode_params(config, 1.0, 1.0).sigma
    return SKEW_PER_QUOTE / (QUOTE_SIZE * sigma ** 2)


# ============================================================================
# 1. Pure model: the paper's experiment
# ============================================================================

def exp1_paper_reproduction() -> dict:
    say("=" * 78)
    say("1. Pure Avellaneda-Stoikov model, the paper's parameters")
    say("   s0=100, T=1, sigma=2, dt=0.005 (200 steps), k=1.5, A=140; "
        f"{PURE_PATHS:,} paths per row (the paper used 1000)")
    say("=" * 78)
    jobs, keys = [], []
    for gamma in ("0.1", "0.01", "1.0"):
        for i, strategy in enumerate(STRATEGIES):
            params = ASParams(gamma=float(gamma), sigma=2.0, k=1.5)
            kwargs = dict(strategy=strategy, n_paths=PURE_PATHS, n_steps=200, dt=0.005, seed=100 + i)
            jobs.append((params, 140.0, kwargs))
            keys.append((gamma, strategy))
    results = list(POOL.map(pure_model_job, jobs))
    out = {"paper_source": PAPER["source"], "n_paths": PURE_PATHS}
    say(f"{'gamma':>5} {'strategy':<10} {'':>6} {'spread':>7} {'profit':>7} {'sd':>6} {'final q':>8} {'sd q':>6}")
    for (gamma, strategy), res in zip(keys, results):
        row = {"spread": r(res.mean_spread, 2), "profit": r(res.pnl.mean(), 2),
               "std_profit": r(res.pnl.std(ddof=1), 2), "final_q": r(res.inventory.mean(), 2),
               "std_q": r(res.inventory.std(ddof=1), 2),
               "se_profit": r(res.pnl.std(ddof=1) / math.sqrt(PURE_PATHS), 2)}
        out.setdefault(gamma, {})[strategy] = {"ours": row, "paper": PAPER[gamma][strategy]}
        p = PAPER[gamma][strategy]
        say(f"{gamma:>5} {strategy:<10} {'ours':>6} {row['spread']:7.2f} {row['profit']:7.2f} "
            f"{row['std_profit']:6.2f} {row['final_q']:8.2f} {row['std_q']:6.2f}")
        say(f"{'':>5} {'':<10} {'paper':>6} {p['spread']:7.2f} {p['profit']:7.1f} "
            f"{p['std_profit']:6.1f} {p['final_q']:8.2f} {p['std_q']:6.1f}")
    say("Paper figures: " + PAPER["source"])
    say("Standard error of our mean profit is std/sqrt(20000); of the paper's, std/sqrt(1000).")
    # The paper's symmetric benchmark uses a constant spread (the inventory strategy's average);
    # ours uses the same time-varying spread. Run the paper's version too, to see whether that
    # explains the gap at gamma = 1, where the spread varies most over the episode.
    say("Paper's own symmetric benchmark (constant spread = inventory strategy's average spread):")
    out["symmetric_constant_spread"] = {}
    for gamma in ("0.1", "0.01", "1.0"):
        spread, pnl, q = pure_symmetric_constant_spread(ASParams(float(gamma), 2.0, 1.5), 140.0, n_paths=PURE_PATHS,
                                                        n_steps=200, dt=0.005, seed=101)
        row = {"spread": r(spread, 2), "profit": r(pnl.mean(), 2), "std_profit": r(pnl.std(ddof=1), 2),
               "final_q": r(q.mean(), 2), "std_q": r(q.std(ddof=1), 2)}
        out["symmetric_constant_spread"][gamma] = row
        say(f"{gamma:>5} {'sym-const':<10} {'ours':>6} {row['spread']:7.2f} {row['profit']:7.2f} "
            f"{row['std_profit']:6.2f} {row['final_q']:8.2f} {row['std_q']:6.2f}")
    return out


# ============================================================================
# 2. Calibration of the fill curve, no market maker
# ============================================================================

def exp2_calibration(config: FlowConfig, verbose: bool = True) -> tuple[ExpFit, dict]:
    reach = np.concatenate(list(POOL.map(calibration_reach, [(config, s) for s in SEEDS_CALIBRATION])))
    prob = reach_probability(reach, CALIBRATION_GRID)
    steps_reached = np.round(prob * len(reach)).astype(int)
    in_fit = (CALIBRATION_GRID >= FIT_RANGE[0]) & (CALIBRATION_GRID <= FIT_RANGE[1])
    in_tail = (CALIBRATION_GRID >= TAIL_RANGE[0]) & (CALIBRATION_GRID <= TAIL_RANGE[1]) & (prob > 0)
    fit = fit_exponential(CALIBRATION_GRID[in_fit], prob[in_fit])
    tail = fit_exponential(CALIBRATION_GRID[in_tail], prob[in_tail])
    with np.errstate(divide="ignore", invalid="ignore"):
        local_k = -np.diff(np.log(prob)) / np.diff(CALIBRATION_GRID)
    out = {
        "seeds": [SEEDS_CALIBRATION.start, SEEDS_CALIBRATION.stop - 1],
        "side_steps": int(len(reach)),
        "fraction_steps_with_reach": r(np.isfinite(reach).mean(), 4),
        "grid": CALIBRATION_GRID.tolist(),
        "probability": [r(p, 6) for p in prob],
        "steps_reached": steps_reached.tolist(),
        "fit_range": list(FIT_RANGE),
        "A_per_step": r(fit.A, 4), "k_per_tick": r(fit.k, 4),
        "r2": r(fit.r2, 4), "max_log_residual": r(fit.max_log_residual, 3),
        "tail_range": list(TAIL_RANGE), "tail_A_per_step": r(tail.A, 4), "tail_k_per_tick": r(tail.k, 4),
        "tail_r2": r(tail.r2, 4),
        "local_k_between_grid_points": [r(x, 3) for x in local_k],
    }
    if verbose:
        say("=" * 78)
        say("2. Fill-curve calibration from the book with no market maker")
        say("=" * 78)
        say(f"{len(SEEDS_CALIBRATION)} episodes (seeds {out['seeds'][0]}..{out['seeds'][1]}), both sides pooled: "
            f"{out['side_steps']:,} side-steps; aggressive flow arrived on "
            f"{out['fraction_steps_with_reach']:.4f} of them (reach is NaN on the rest, counted as 'not reached').")
        say(" delta  P(reach>=delta)  side-steps reached   local slope to next point")
        for d, p, c, lk in zip(CALIBRATION_GRID, out["probability"], out["steps_reached"],
                               out["local_k_between_grid_points"] + [math.nan]):
            say(f"{d:6.1f}  {p:15.6f}  {c:18d}   {lk:6.3f}")
        say(f"Fit on delta in [{FIT_RANGE[0]}, {FIT_RANGE[1]}] (where the market maker quotes): "
            f"A = {out['A_per_step']} per step, k = {out['k_per_tick']} per tick, "
            f"R^2 = {out['r2']}, max |log residual| = {out['max_log_residual']}")
        say(f"For context, the tail [{TAIL_RANGE[0]}, {TAIL_RANGE[1]}]: A = {out['tail_A_per_step']}, "
            f"k = {out['tail_k_per_tick']}, R^2 = {out['tail_r2']}. The curve is not one exponential: "
            "it is flatter near the touch and steeper further out.")
    return fit, out


def exp2b_validate(fit: ExpFit, main: dict[str, list[dict]]) -> tuple[ExpFit, dict]:
    say("-" * 78)
    say("2b. Validation: fitted curve vs the market maker's realised fill frequency (experiment 3 episodes)")
    say("    'fill' = at least one fill on that quote during the step it was exposed; bids and asks pooled")
    out = {"bins": DISTANCE_BINS.tolist()}
    min_quotes = 1000
    for strategy, eps in main.items():
        quotes = np.sum([e["quotes_at_distance"] for e in eps], axis=0)
        fills = np.sum([e["fills_at_distance"] for e in eps], axis=0)
        freq = np.divide(fills, quotes, out=np.full(len(quotes), np.nan), where=quotes > 0)
        possible = sum(2 * (len(e["inventory_path"]) - 1) for e in eps)   # two quotes, each exposed n - 1 times
        out[strategy] = {"quote_steps": quotes.astype(int).tolist(),
                         "fill_frequency": [None if np.isnan(f) else r(f, 4) for f in freq],
                         "share_of_quote_steps_outside_bins": r(1 - quotes.sum() / possible, 6)}
        say(f"  {strategy}:  delta  quote-steps  realised  fitted  realised/fitted")
        for d, q, f in zip(DISTANCE_BINS, quotes, freq):
            if q >= min_quotes:
                pred = fit.A * math.exp(-fit.k * d)
                say(f"            {d:5.1f}  {int(q):11d}  {f:8.4f}  {pred:6.4f}  {f / pred:6.3f}")
    # Effective curve from the inventory strategy's own fills (it quotes the widest range of distances).
    quotes = np.array(out["inventory"]["quote_steps"])
    freq = np.array([np.nan if f is None else f for f in out["inventory"]["fill_frequency"]])
    use = (quotes >= min_quotes) & (DISTANCE_BINS >= FIT_RANGE[0]) & (DISTANCE_BINS <= FIT_RANGE[1])
    eff = fit_exponential(DISTANCE_BINS[use], freq[use])
    ratios = freq[use] / (fit.A * np.exp(-fit.k * DISTANCE_BINS[use]))
    out.update({"min_quote_steps_per_bin": min_quotes,
                "effective_A_per_step": r(eff.A, 4), "effective_k_per_tick": r(eff.k, 4),
                "effective_r2": r(eff.r2, 4), "effective_fit_bins": DISTANCE_BINS[use].tolist(),
                "realised_over_fitted_min": r(ratios.min(), 3), "realised_over_fitted_max": r(ratios.max(), 3)})
    say(f"  Realised / fitted over delta {out['effective_fit_bins']}: "
        f"{out['realised_over_fitted_min']} to {out['realised_over_fitted_max']}")
    say(f"  Effective curve fitted to the inventory strategy's realised fills on the same bins: "
        f"A = {out['effective_A_per_step']} per step, k = {out['effective_k_per_tick']} per tick "
        f"(R^2 {out['effective_r2']})")
    return eff, out


# ============================================================================
# 3. Main comparison in the order-book simulator
# ============================================================================

def strategy_stats(eps: list[dict]) -> dict:
    pnl = np.array([e["pnl"] for e in eps])
    term_q = np.array([e["terminal_inventory"] for e in eps])

    def ms(key, digits=1):
        m, se = mean_se([e[key] for e in eps])
        return {"mean": r(m, digits), "se": r(se, digits)}

    ratio = pnl.mean() / pnl.std(ddof=1)
    p5 = np.percentile(pnl, 5)
    return {
        "n_episodes": len(eps),
        "pnl_mean": r(pnl.mean(), 1), "pnl_mean_se": r(pnl.std(ddof=1) / math.sqrt(len(pnl)), 1),
        "pnl_std": r(pnl.std(ddof=1), 1),
        "pnl_std_ci95": [r(x, 1) for x in bootstrap_ci(pnl, lambda v: v.std(ddof=1))],
        "mean_over_std": r(ratio, 3),
        "mean_over_std_ci95": [r(x, 3) for x in bootstrap_ci(pnl, lambda v: v.mean() / v.std(ddof=1))],
        "pnl_p5": r(p5, 1), "pnl_p5_ci95": [r(x, 1) for x in bootstrap_ci(pnl, lambda v: np.percentile(v, 5))],
        "fraction_losing_episodes": r((pnl < 0).mean(), 4),
        "terminal_inventory_mean": r(term_q.mean(), 2),
        "terminal_inventory_std": r(term_q.std(ddof=1), 1),
        "mean_abs_inventory": ms("mean_abs_inventory", 2),
        "max_abs_inventory": ms("max_abs_inventory", 1),
        "max_abs_inventory_worst_episode": int(max(e["max_abs_inventory"] for e in eps)),
        "fills_per_episode": ms("n_fills", 1),
        "filled_qty_per_episode": ms("filled_qty", 1),
        "mean_quoted_spread": ms("mean_quoted_spread", 3),
        "mean_background_spread": ms("mean_background_spread", 3),
        "spread_pnl": ms("spread_pnl", 1),
        "inventory_pnl": ms("inventory_pnl", 1),
        "pnl_fund": ms("pnl_fund", 1),
        "spread_pnl_fund": ms("spread_pnl_fund", 1),
        "inventory_pnl_fund": ms("inventory_pnl_fund", 1),
    }


def exp3_main(config: FlowConfig, params: ASParams) -> tuple[dict, dict[str, list[dict]]]:
    say("=" * 78)
    say(f"3. Order-book simulator: inventory vs symmetric on the same {len(SEEDS_MAIN)} tapes "
        f"(seeds {SEEDS_MAIN.start}..{SEEDS_MAIN.stop - 1})")
    say(f"   quote size {QUOTE_SIZE}; ASParams gamma={params.gamma:.6g}, sigma={params.sigma:.4f} "
        f"ticks/sqrt(episode), k={params.k}")
    say("=" * 78)
    episodes = {name: run_strategy(config, params, name, SEEDS_MAIN) for name in STRATEGIES}
    out = {"seeds": [SEEDS_MAIN.start, SEEDS_MAIN.stop - 1], "quote_size": QUOTE_SIZE,
           "gamma": params.gamma, "sigma_per_sqrt_episode": r(params.sigma, 4), "k": params.k}
    for name, eps in episodes.items():
        out[name] = strategy_stats(eps)
    diff = np.array([a["pnl"] - b["pnl"] for a, b in zip(episodes["inventory"], episodes["symmetric"])])
    m, se = mean_se(diff)
    out["paired_pnl_difference_inventory_minus_symmetric"] = {
        "mean": r(m, 1), "se": r(se, 1), "ci95": [r(m - 1.96 * se, 1), r(m + 1.96 * se, 1)],
        "std": r(diff.std(ddof=1), 1), "fraction_inventory_better": r((diff > 0).mean(), 4)}
    i, s = out["inventory"], out["symmetric"]

    def row(label, a, b):
        say(f"  {label:<36} {a:>22} {b:>22}")

    def pm(d):
        return f"{d['mean']} +/- {d['se']}"
    row("", "inventory (A-S)", "symmetric")
    row("terminal PnL mean +/- s.e.", f"{i['pnl_mean']} +/- {i['pnl_mean_se']}", f"{s['pnl_mean']} +/- {s['pnl_mean_se']}")
    row("terminal PnL std [95% CI]", f"{i['pnl_std']} {i['pnl_std_ci95']}", f"{s['pnl_std']} {s['pnl_std_ci95']}")
    row("mean / std [95% CI]", f"{i['mean_over_std']} {i['mean_over_std_ci95']}", f"{s['mean_over_std']} {s['mean_over_std_ci95']}")
    row("5th percentile PnL [95% CI]", f"{i['pnl_p5']} {i['pnl_p5_ci95']}", f"{s['pnl_p5']} {s['pnl_p5_ci95']}")
    row("fraction of losing episodes", i["fraction_losing_episodes"], s["fraction_losing_episodes"])
    row("terminal inventory mean / std", f"{i['terminal_inventory_mean']} / {i['terminal_inventory_std']}",
        f"{s['terminal_inventory_mean']} / {s['terminal_inventory_std']}")
    row("time-average |inventory|", pm(i["mean_abs_inventory"]), pm(s["mean_abs_inventory"]))
    row("max |inventory| per episode", pm(i["max_abs_inventory"]), pm(s["max_abs_inventory"]))
    row("max |inventory|, worst episode", i["max_abs_inventory_worst_episode"], s["max_abs_inventory_worst_episode"])
    row("fills per episode", pm(i["fills_per_episode"]), pm(s["fills_per_episode"]))
    row("filled quantity per episode", pm(i["filled_qty_per_episode"]), pm(s["filled_qty_per_episode"]))
    row("average quoted spread (ticks)", pm(i["mean_quoted_spread"]), pm(s["mean_quoted_spread"]))
    row("average background spread (ticks)", pm(i["mean_background_spread"]), pm(s["mean_background_spread"]))
    row("spread PnL (vs book mid)", pm(i["spread_pnl"]), pm(s["spread_pnl"]))
    row("inventory PnL (vs book mid)", pm(i["inventory_pnl"]), pm(s["inventory_pnl"]))
    d = out["paired_pnl_difference_inventory_minus_symmetric"]
    say(f"  Paired PnL difference, inventory minus symmetric, same tape: {d['mean']} +/- {d['se']} "
        f"(95% CI {d['ci95']}); per-tape std {d['std']}; inventory better on {d['fraction_inventory_better']} of tapes")
    return out, episodes


# ============================================================================
# 4. Order-book simulator vs the pure model with matched parameters
# ============================================================================

def exp4_pure_vs_book(config: FlowConfig, params: ASParams, fit: ExpFit, main: dict,
                      validation: dict) -> dict:
    say("=" * 78)
    say("4. Order-book simulator vs pure model with matched parameters")
    say(f"   pure model: dt = 1 step, {config.n_steps} steps, sigma = {config.sigma} ticks/step, same gamma, "
        f"calibrated A = {r(fit.A, 4)}, k = {r(fit.k, 4)}, fill_size = {QUOTE_SIZE}, {PURE_PATHS:,} paths")
    say("=" * 78)
    p = ASParams(gamma=params.gamma, sigma=config.sigma, k=params.k)   # sigma per step, time in steps
    jobs = [(p, fit.A, dict(strategy=strategy, n_paths=PURE_PATHS, n_steps=config.n_steps, dt=1.0,
                            s0=float(config.start_price), fill_size=float(QUOTE_SIZE), seed=200 + j))
            for j, strategy in enumerate(STRATEGIES)]
    results = dict(zip(STRATEGIES, POOL.map(pure_model_job, jobs)))
    out = {"A_per_step": r(fit.A, 4), "k_per_tick": params.k, "n_paths": PURE_PATHS}
    keys = ("pnl_mean", "pnl_std", "terminal_inventory_std", "mean_abs_inventory", "fills_per_episode",
            "filled_qty_per_episode", "pnl_per_filled_lot", "mean_quoted_spread")
    say(f"  {'':<12} {'':<10} {'PnL mean':>9} {'PnL std':>8} {'inv std':>8} {'mean|q|':>8} "
        f"{'fills':>7} {'lots':>8} {'PnL/lot':>8} {'spread':>7}")
    bins = np.array(validation["bins"])
    for strategy in STRATEGIES:
        b, res = main[strategy], results[strategy]
        book = {"pnl_mean": b["pnl_mean"], "pnl_std": b["pnl_std"],
                "terminal_inventory_std": b["terminal_inventory_std"],
                "mean_abs_inventory": b["mean_abs_inventory"]["mean"],
                "fills_per_episode": b["fills_per_episode"]["mean"],
                "filled_qty_per_episode": b["filled_qty_per_episode"]["mean"],
                "pnl_per_filled_lot": r(b["pnl_mean"] / b["filled_qty_per_episode"]["mean"], 3),
                "mean_quoted_spread": b["mean_quoted_spread"]["mean"]}
        pure = {"pnl_mean": r(res.pnl.mean(), 1), "pnl_std": r(res.pnl.std(ddof=1), 1),
                "terminal_inventory_std": r(res.inventory.std(ddof=1), 1),
                "mean_abs_inventory": r(res.mean_abs_inventory, 2),
                "fills_per_episode": r(res.fills_per_path, 1),
                "filled_qty_per_episode": r(res.fills_per_path * QUOTE_SIZE, 1),
                "pnl_per_filled_lot": r(res.pnl.mean() / (res.fills_per_path * QUOTE_SIZE), 3),
                "mean_quoted_spread": r(res.mean_spread, 3)}
        # Where the volume goes: a chain of per-side-step fill probabilities.
        #   pure:      the pure model's own rate, at its continuous quote distances
        #   curve@book: the calibrated curve evaluated at the distances the book market maker actually quoted
        #   realised:  the book market maker's actual rate (steps with at least one fill / quote-steps)
        quotes = np.array(validation[strategy]["quote_steps"], float)
        freq = np.array([0.0 if f is None else f for f in validation[strategy]["fill_frequency"]])
        rate_pure = res.fills_per_path / (2 * config.n_steps)
        rate_curve_at_book = float(np.sum(quotes * np.minimum(1.0, fit.A * np.exp(-fit.k * bins))) / quotes.sum())
        rate_realised = float(np.sum(quotes * freq) / quotes.sum())
        chain = {"pure_model_fill_rate": r(rate_pure, 4),
                 "calibrated_curve_at_book_quote_distances": r(rate_curve_at_book, 4),
                 "book_realised_fill_rate": r(rate_realised, 4),
                 "book_lots_per_fill": r(b["filled_qty_per_episode"]["mean"] / b["fills_per_episode"]["mean"], 3),
                 "pure_lots_per_fill": float(QUOTE_SIZE)}
        out[strategy] = {"order book": book, "pure model": pure, "fill_rate_chain": chain}
        for name, v in (("order book", book), ("pure model", pure)):
            say(f"  {name:<12} {strategy:<10} {v['pnl_mean']:9.1f} {v['pnl_std']:8.1f} "
                f"{v['terminal_inventory_std']:8.1f} {v['mean_abs_inventory']:8.2f} {v['fills_per_episode']:7.1f} "
                f"{v['filled_qty_per_episode']:8.1f} {v['pnl_per_filled_lot']:8.3f} {v['mean_quoted_spread']:7.3f}")
        say(f"    fill probability per side-step: pure model {chain['pure_model_fill_rate']} -> calibrated curve at the "
            f"book's actual quote distances {chain['calibrated_curve_at_book_quote_distances']} -> realised "
            f"{chain['book_realised_fill_rate']}; lots per fill {chain['pure_lots_per_fill']:g} (pure) vs "
            f"{chain['book_lots_per_fill']} (book)")
    say("  Quote-steps at distances outside 0..6 ticks (or with no quote) are left out of the chain; their share: " +
        ", ".join(f"{name} {validation[name]['share_of_quote_steps_outside_bins']}" for name in STRATEGIES))
    say("  'spread' for the book is the average integer quote spread after rounding and the passivity clamp;")
    say("  for the pure model it is the continuous formula value averaged over time.")
    return out


# ============================================================================
# 5. Adverse selection
# ============================================================================

def markout_table(eps: list[dict]) -> dict:
    """Quantity-weighted mean markouts and edge decomposition, overall and by taker kind."""
    out = {}
    total_qty = sum(e["markouts"]["all"]["qty"] for e in eps)
    for group in ("all",) + TAKER_KINDS:
        g = [e["markouts"][group] for e in eps]
        qty = sum(x["qty"] for x in g)
        if qty == 0:
            out[group] = None
            continue
        w = np.sum([x["weight"] for x in g], axis=0)
        mid = np.sum([x["mid"] for x in g], axis=0) / w
        fund = np.sum([x["fund"] for x in g], axis=0) / w
        out[group] = {
            "share_of_filled_qty": r(qty / total_qty, 4),
            "fills": int(sum(x["n"] for x in g)),
            "edge_vs_mid_before": r(sum(x["edge"] for x in g) / qty, 3),
            "mid_move_during_fill_step": r(sum(x["impact"] for x in g) / qty, 3),
            "fundamental_minus_mid_at_fill": r(sum(x["gap"] for x in g) / qty, 3),
            "markout_mid": {str(h): r(v, 3) for h, v in zip(MARKOUT_HORIZONS, mid)},
            "markout_fundamental": {str(h): r(v, 3) for h, v in zip(MARKOUT_HORIZONS, fund)},
        }
    return out


def exp5_adverse_selection(main_eps: dict[str, list[dict]], main: dict) -> dict:
    say("=" * 78)
    say("5. Adverse selection at the default volatility (experiment 3 episodes)")
    say("   markout(h) = sign * (price[s+h] - price[s]) per lot, s = fill step, sign +1 for our buys;")
    say("   positive = the price moved our way after the fill. Quantity-weighted means, in ticks.")
    say("=" * 78)
    out = {}
    for strategy, eps in main_eps.items():
        t = markout_table(eps)
        out[strategy] = {"markouts": t,
                         "attribution_mid": {"spread": main[strategy]["spread_pnl"], "inventory": main[strategy]["inventory_pnl"],
                                             "total": {"mean": main[strategy]["pnl_mean"], "se": main[strategy]["pnl_mean_se"]}},
                         "attribution_fundamental": {"spread": main[strategy]["spread_pnl_fund"],
                                                     "inventory": main[strategy]["inventory_pnl_fund"],
                                                     "total": main[strategy]["pnl_fund"]}}
        say(f"  {strategy}:")
        a, f = out[strategy]["attribution_mid"], out[strategy]["attribution_fundamental"]
        say(f"    attribution marked at book mid:     spread {a['spread']['mean']} +/- {a['spread']['se']}, "
            f"inventory {a['inventory']['mean']} +/- {a['inventory']['se']}")
        say(f"    attribution marked at fundamental:  spread {f['spread']['mean']} +/- {f['spread']['se']}, "
            f"inventory {f['inventory']['mean']} +/- {f['inventory']['se']}, total {f['total']['mean']} +/- {f['total']['se']}")
        say(f"    {'taker':<8} {'qty share':>9} {'edge':>6} {'impact':>7} {'F-mid':>6} | mid markout h=" +
            ",".join(map(str, REPORT_HORIZONS)) + " | fundamental markout h=" + ",".join(map(str, REPORT_HORIZONS)))
        for group in ("all",) + TAKER_KINDS:
            g = t[group]
            mids = " ".join(f"{g['markout_mid'][str(h)]:6.3f}" for h in REPORT_HORIZONS)
            funds = " ".join(f"{g['markout_fundamental'][str(h)]:6.3f}" for h in REPORT_HORIZONS)
            say(f"    {group:<8} {g['share_of_filled_qty']:9.4f} {g['edge_vs_mid_before']:6.3f} "
                f"{g['mid_move_during_fill_step']:7.3f} {g['fundamental_minus_mid_at_fill']:6.3f} | {mids} | {funds}")
    say("  edge = quoted edge vs the mid the quote was set against; impact = signed move of the book mid")
    say("  during the fill step; F-mid = signed gap between fundamental and book mid at the close of that step.")
    return out


def exp5b_volatility_sweep(base: FlowConfig, gamma_fixed: float) -> dict:
    say("-" * 78)
    say("5b. Volatility sweep: FlowConfig.sigma varied, everything else in the flow fixed.")
    say("    For each sigma: ASParams.sigma = sigma*sqrt(n_steps), k re-calibrated from the book with no")
    say(f"    market maker (same seeds and fit range as experiment 2), gamma held at {gamma_fixed:.6g}.")
    say(f"    Robustness rows ('rescaled gamma') instead hold gamma*sigma^2 fixed. {len(SEEDS_SWEEP)} shared tapes per point.")
    out = {"sigmas": VOL_SWEEP, "gamma_fixed": gamma_fixed, "seeds": [SEEDS_SWEEP.start, SEEDS_SWEEP.stop - 1],
           "points": []}
    for sigma in VOL_SWEEP:
        config = replace(base, sigma=sigma)
        fit, _ = exp2_calibration(config, verbose=False)
        k = r(fit.k, 4)
        point = {"sigma": sigma, "k": k, "A": r(fit.A, 4)}
        for label, gamma in (("fixed_gamma", gamma_fixed), ("rescaled_gamma", default_gamma(config))):
            params = episode_params(config, gamma, k)
            point[label] = {"gamma": gamma}
            for strategy in STRATEGIES:
                eps = run_strategy(config, params, strategy, SEEDS_SWEEP)
                st = strategy_stats(eps)
                entry = {key: st[key] for key in ("pnl_mean", "pnl_mean_se", "pnl_std", "mean_over_std",
                                                  "terminal_inventory_std", "fills_per_episode",
                                                  "mean_quoted_spread", "spread_pnl", "inventory_pnl",
                                                  "spread_pnl_fund", "inventory_pnl_fund")}
                if label == "fixed_gamma":
                    mk = markout_table(eps)
                    entry["markout_mid"] = {str(h): mk["all"]["markout_mid"][str(h)] for h in REPORT_HORIZONS}
                    entry["markout_fundamental"] = {str(h): mk["all"]["markout_fundamental"][str(h)] for h in REPORT_HORIZONS}
                    entry["limit_taker_qty_share"] = r(sum(mk[g]["share_of_filled_qty"] for g in ("limit", "either")
                                                           if mk[g] is not None), 4)
                    entry["fundamental_minus_mid_at_fill"] = mk["all"]["fundamental_minus_mid_at_fill"]
                point[label][strategy] = entry
        out["points"].append(point)
        for strategy in STRATEGIES:
            e = point["fixed_gamma"][strategy]
            rs = point["rescaled_gamma"][strategy]
            say(f"  sigma {sigma:3.1f} k {k:.4f} {strategy:<9} PnL {e['pnl_mean']:8.1f} +/- {e['pnl_mean_se']:6.1f} "
                f"sd {e['pnl_std']:7.1f} | spread {e['spread_pnl']['mean']:7.1f} inv {e['inventory_pnl']['mean']:8.1f} "
                f"| at F: spread {e['spread_pnl_fund']['mean']:7.1f} inv {e['inventory_pnl_fund']['mean']:7.1f} "
                f"| fills {e['fills_per_episode']['mean']:5.1f} quoted spread {e['mean_quoted_spread']['mean']:6.3f}")
            say(f"        markout mid h=1,5,20,100: " + " ".join(f"{e['markout_mid'][str(h)]:6.3f}" for h in REPORT_HORIZONS) +
                f" | fundamental: " + " ".join(f"{e['markout_fundamental'][str(h)]:6.3f}" for h in REPORT_HORIZONS) +
                f" | limit-or-either taker share {e['limit_taker_qty_share']:.4f}")
            say(f"        rescaled gamma {point['rescaled_gamma']['gamma']:.6g}: PnL {rs['pnl_mean']:8.1f} +/- {rs['pnl_mean_se']:6.1f} sd {rs['pnl_std']:7.1f}")
    # Where does mean PnL first become significantly negative / stop being significantly positive?
    for label in ("fixed_gamma", "rescaled_gamma"):
        for strategy in STRATEGIES:
            signs = []
            for pt in out["points"]:
                e = pt[label][strategy]
                lo, hi = e["pnl_mean"] - 1.96 * e["pnl_mean_se"], e["pnl_mean"] + 1.96 * e["pnl_mean_se"]
                signs.append("+" if lo > 0 else "-" if hi < 0 else "0")
            out.setdefault("sign_of_mean_pnl", {}).setdefault(label, {})[strategy] = signs
    say("  Sign of mean PnL at each sigma (+ / - = 95% CI excludes zero, 0 = not distinguishable):")
    for label, d in out["sign_of_mean_pnl"].items():
        for strategy, signs in d.items():
            say(f"    {label:<15} {strategy:<9} " + "  ".join(f"{s}@{sg}" for s, sg in zip(VOL_SWEEP, signs)))
    return out


# ============================================================================
# 6. Risk-aversion sweep
# ============================================================================

def exp6_gamma_sweep(config: FlowConfig, gamma0: float, k: float) -> dict:
    say("=" * 78)
    say(f"6. Risk-aversion sweep, inventory strategy, {len(SEEDS_GAMMA)} shared tapes per point "
        f"(seeds {SEEDS_GAMMA.start}..{SEEDS_GAMMA.stop - 1})")
    say("=" * 78)
    out = {"gamma_default": gamma0, "seeds": [SEEDS_GAMMA.start, SEEDS_GAMMA.stop - 1], "points": []}
    keys = ("pnl_mean", "pnl_mean_se", "pnl_std", "pnl_std_ci95", "mean_over_std", "terminal_inventory_std",
            "mean_abs_inventory", "fills_per_episode", "mean_quoted_spread")
    for mult in GAMMA_MULTIPLIERS:
        params = episode_params(config, gamma0 * mult, k)
        st = strategy_stats(run_strategy(config, params, "inventory", SEEDS_GAMMA))
        point = {"multiplier": mult, "gamma": gamma0 * mult,
                 "skew_per_quote_at_start_ticks": r(SKEW_PER_QUOTE * mult, 3), **{key: st[key] for key in keys}}
        out["points"].append(point)
    sym = strategy_stats(run_strategy(config, episode_params(config, gamma0, k), "symmetric", SEEDS_GAMMA))
    out["symmetric_at_default"] = {key: sym[key] for key in keys}
    say(f"  {'gamma x':>8} {'skew/quote':>10} {'PnL mean':>9} {'s.e.':>5} {'PnL std':>8} {'mean/std':>8} "
        f"{'inv std':>8} {'mean|q|':>8} {'fills':>6} {'spread':>7}")
    for p in out["points"] + [dict(out["symmetric_at_default"], multiplier="sym", skew_per_quote_at_start_ticks=0.0)]:
        say(f"  {str(p['multiplier']):>8} {p['skew_per_quote_at_start_ticks']:10.3f} {p['pnl_mean']:9.1f} "
            f"{p['pnl_mean_se']:5.1f} {p['pnl_std']:8.1f} {p['mean_over_std']:8.3f} {p['terminal_inventory_std']:8.1f} "
            f"{p['mean_abs_inventory']['mean']:8.2f} {p['fills_per_episode']['mean']:6.1f} "
            f"{p['mean_quoted_spread']['mean']:7.3f}")
    say("  skew/quote = reservation-price shift (ticks) from holding one quote's worth of inventory at t = 0.")
    return out


# ============================================================================
# 7. Inventory over time
# ============================================================================

def exp7_inventory_over_time(main_eps: dict[str, list[dict]], config: FlowConfig, params: ASParams) -> dict:
    say("=" * 78)
    say("7. Cross-sectional std of inventory across the experiment-3 tapes, by step")
    say("=" * 78)
    n = config.n_steps
    out = {"report_steps": [99, 499, 999, 1499, 1799, 1899, 1949, 1999]}
    skew = [QUOTE_SIZE * params.gamma * params.sigma ** 2 * (n - 1 - i) / n for i in out["report_steps"]]
    out["skew_per_quote_ticks"] = [r(x, 4) for x in skew]
    for strategy, eps in main_eps.items():
        paths = np.stack([e["inventory_path"] for e in eps])
        std = paths.std(axis=0, ddof=1)
        out[strategy] = {"std_by_step": [r(x, 2) for x in std],
                         "std_at_report_steps": [r(std[i], 2) for i in out["report_steps"]],
                         "std_mean_steps_500_to_1499": r(std[500:1500].mean(), 2),
                         "std_min": r(std.min(), 2), "std_min_step": int(std.argmin())}
    say("  step:              " + " ".join(f"{s:7d}" for s in out["report_steps"]))
    say("  skew/quote (ticks):" + " ".join(f"{x:7.4f}" for x in out["skew_per_quote_ticks"]))
    for strategy in STRATEGIES:
        d = out[strategy]
        say(f"  {strategy:<19}" + " ".join(f"{x:7.2f}" for x in d["std_at_report_steps"]))
        say(f"      mean over steps 500..1499: {d['std_mean_steps_500_to_1499']}")
    return out


# ============================================================================
# Figures
# ============================================================================

def save(fig, name: str) -> None:
    fig.savefig(OUT / name, bbox_inches="tight")
    plt.close(fig)


def fig_example_episode(config: FlowConfig, params: ASParams, seed: int = 0) -> None:
    tape = generate_tape(config, seed)
    res = run_episode(tape, config, inventory_quotes, params, QUOTE_SIZE)
    steps = np.arange(config.n_steps)
    lo, hi = 1000, 1150      # zoom window
    fig = plt.figure(figsize=(8.4, 8.2))
    gs = fig.add_gridspec(4, 1, height_ratios=[1.0, 1.2, 0.9, 1.1], hspace=0.55)
    ax0 = fig.add_subplot(gs[0])
    ax1 = fig.add_subplot(gs[1])
    ax2 = fig.add_subplot(gs[2], sharex=ax0)
    ax3 = fig.add_subplot(gs[3], sharex=ax0)

    ax0.plot(steps, res.fundamental, color=ACCENT, lw=1.0, label="fundamental (hidden)")
    ax0.plot(steps, res.mid, color=INK2, lw=1.0, label="background book mid")
    ax0.axvspan(lo, hi, color=GRID, alpha=0.7, lw=0)
    ax0.text(lo + (hi - lo) / 2, 0.96, "zoom\nbelow", transform=ax0.get_xaxis_transform(),
             ha="center", va="top", color=INK2, fontsize=7)
    ax0.set_ylabel("price (ticks)")
    ax0.set_title(f"One episode, inventory strategy (seed {seed})")
    ax0.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5)

    z = slice(lo, hi)
    ax1.step(steps[z], res.ask_quote[z], where="post", color=COLOR["inventory"], lw=1.2, label="our ask")
    ax1.step(steps[z], res.bid_quote[z], where="post", color=COLOR["inventory"], lw=1.2, alpha=0.55, label="our bid")
    ax1.plot(steps[z], res.mid[z], color=INK2, lw=1.0, label="book mid")
    ax1.plot(steps[z], res.fundamental[z], color=ACCENT, lw=1.0, label="fundamental")
    buys = [(s, p) for s, side, p, _ in res.fills if side == Side.Buy and lo <= s < hi]
    sells = [(s, p) for s, side, p, _ in res.fills if side == Side.Sell and lo <= s < hi]
    if buys:
        ax1.scatter(*zip(*buys), marker="^", s=36, color=INK, edgecolor="white", linewidth=1.0, zorder=5, label="we buy")
    if sells:
        ax1.scatter(*zip(*sells), marker="v", s=36, color=VIOLET, edgecolor="white", linewidth=1.0, zorder=5, label="we sell")
    ax1.set_xlim(lo, hi)
    ax1.set_ylabel("price (ticks)")
    ax1.set_title(f"Steps {lo} to {hi}: quotes and fills")
    ax1.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5)

    ax2.plot(steps, res.inventory, color=COLOR["inventory"], lw=1.0)
    ax2.axhline(0, color="#c3c2b7", lw=0.8)
    ax2.set_ylabel("inventory (lots)")
    ax2.set_title("Inventory")

    ax3.plot(steps, res.pnl, color=INK, lw=1.4, label="total (marked at book mid)")
    ax3.plot(steps, np.cumsum(res.spread_pnl), color=COLOR["inventory"], lw=1.2, label="spread part")
    ax3.plot(steps, np.cumsum(res.inventory_pnl), color=COLOR["inventory"], lw=1.2, ls="--", label="inventory part")
    ax3.axhline(0, color="#c3c2b7", lw=0.8)
    ax3.set_ylabel("P&L (ticks x lots)")
    ax3.set_xlabel("step")
    ax3.set_title("P&L and its attribution (spread + inventory = total, exactly)")
    ax3.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5)
    ax3.set_xlim(0, config.n_steps)
    save(fig, "fig1_example_episode.png")


def fig_pnl_distribution(main_eps: dict[str, list[dict]], main: dict) -> None:
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    all_pnl = np.concatenate([[e["pnl"] for e in eps] for eps in main_eps.values()])
    bins = np.linspace(np.percentile(all_pnl, 0.5), np.percentile(all_pnl, 99.5), 70)
    for name, eps in main_eps.items():
        pnl = [e["pnl"] for e in eps]
        ax.hist(pnl, bins=bins, color=COLOR[name], alpha=0.25, lw=0)
        ax.hist(pnl, bins=bins, color=COLOR[name], histtype="step", lw=1.4,
                label=f"{name}: mean {main[name]['pnl_mean']}, std {main[name]['pnl_std']}")
    ax.axvline(0, color="#c3c2b7", lw=0.8)
    ax.set_xlabel("terminal P&L (ticks x lots)")
    ax.set_ylabel("episodes")
    n = main["inventory"]["n_episodes"]
    ax.set_title(f"Terminal P&L over {n} shared tapes (x range: 0.5th to 99.5th percentile of both)")
    ax.legend(loc="upper right", fontsize=8)
    save(fig, "fig2_pnl_distribution.png")


def fig_inventory_over_time(inv_time: dict, config: FlowConfig, params: ASParams) -> None:
    n = config.n_steps
    steps = np.arange(n)
    fig, (ax, axs) = plt.subplots(2, 1, figsize=(6.4, 4.6), sharex=True, height_ratios=[2.2, 1])
    for name in STRATEGIES:
        ax.plot(steps, inv_time[name]["std_by_step"], color=COLOR[name], label=name)
    ax.set_ylabel("std of inventory\nacross tapes (lots)")
    ax.set_title("Inventory dispersion over the episode")
    ax.legend(loc="upper left")
    skew = QUOTE_SIZE * params.gamma * params.sigma ** 2 * (n - 1 - steps) / n
    axs.plot(steps, skew, color=COLOR["inventory"], lw=1.4)
    axs.set_ylabel("skew per quote\n(ticks)")
    axs.set_xlabel("step")
    axs.set_title("Inventory strategy: reservation-price shift for 5 lots, q*gamma*sigma^2*(T-t)", fontsize=9)
    axs.set_xlim(0, n)
    axs.set_ylim(0, None)
    save(fig, "fig3_inventory_std_over_time.png")


def fig_calibration(cal: dict, fit: ExpFit, val: dict) -> None:
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    grid = np.array(cal["grid"])
    prob = np.array(cal["probability"])
    ax.scatter(grid, prob, s=28, color=ACCENT, edgecolor="white", linewidth=1.0, zorder=4,
               label="reach probability, book without market maker")
    x_fit = np.linspace(*FIT_RANGE, 50)
    x_all = np.linspace(0.5, 8, 100)
    ax.plot(x_all, fit.A * np.exp(-fit.k * x_all), color=ACCENT, lw=0.9, ls=":")
    ax.plot(x_fit, fit.A * np.exp(-fit.k * x_fit), color=ACCENT, lw=2.0,
            label=f"fit on [{FIT_RANGE[0]:g}, {FIT_RANGE[1]:g}]: A={cal['A_per_step']}, k={cal['k_per_tick']}")
    bins = np.array(val["bins"])
    for name in STRATEGIES:
        q = np.array(val[name]["quote_steps"])
        f = np.array([np.nan if v is None else v for v in val[name]["fill_frequency"]])
        use = q >= val["min_quote_steps_per_bin"]
        ax.plot(bins[use], f[use], color=COLOR[name], marker="o", ms=5, lw=1.0, mec="white", mew=1.0,
                label=f"market maker's realised fill rate, {name}")
    ax.set_yscale("log")
    ax.set_xlabel("distance of quote from mid, delta (ticks)")
    ax.set_ylabel("probability per step")
    ax.set_title("Fill curve: calibrated vs realised")
    ax.legend(loc="lower left", fontsize=7.5)
    save(fig, "fig4_calibration.png")


def fig_pure_vs_book(comp: dict) -> None:
    metrics = [("pnl_mean", "P&L mean (ticks x lots)"), ("pnl_std", "P&L std (ticks x lots)"),
               ("filled_qty_per_episode", "filled lots per episode"),
               ("pnl_per_filled_lot", "P&L per filled lot (ticks)"),
               ("terminal_inventory_std", "terminal inventory std (lots)"),
               ("mean_quoted_spread", "average quoted spread (ticks)")]
    sources = ["order book", "pure model"]
    fig, axes = plt.subplots(2, 3, figsize=(7.6, 4.8))
    x = np.arange(len(sources))
    width = 0.36
    for ax, (key, title) in zip(axes.flat, metrics):
        for j, name in enumerate(STRATEGIES):
            vals = [comp[name][s][key] for s in sources]
            ax.bar(x + (j - 0.5) * width, vals, width=width * 0.92, color=COLOR[name], label=name)
        ax.set_xticks(x, sources, fontsize=8)
        ax.set_title(title, fontsize=8.5)
        ax.grid(axis="x", visible=False)
    axes[0, 0].legend(loc="upper left", fontsize=7.5)
    fig.suptitle("Order-book simulator vs pure model with matched parameters", x=0.02, ha="left", fontsize=10.5)
    fig.tight_layout()
    save(fig, "fig5_pure_vs_book.png")


def fig_gamma_frontier(sweep: dict) -> None:
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.4, 3.5))
    pts = sweep["points"]
    xs = [p["pnl_std"] for p in pts]
    ys = [p["pnl_mean"] for p in pts]
    ax.plot(xs, ys, color=COLOR["inventory"], marker="o", ms=5, mec="white", mew=1.0, label="inventory strategy")
    for p in pts:
        ax.annotate(f"{p['multiplier']:g}x", (p["pnl_std"], p["pnl_mean"]), textcoords="offset points",
                    xytext=(5, -9), fontsize=7, color=INK2)
    s = sweep["symmetric_at_default"]
    ax.errorbar([s["pnl_std"]], [s["pnl_mean"]], yerr=[1.96 * s["pnl_mean_se"]], color=COLOR["symmetric"],
                marker="s", ms=6, mec="white", lw=1.0, capsize=2, label="symmetric (default gamma)")
    ax.errorbar(xs, ys, yerr=[1.96 * p["pnl_mean_se"] for p in pts], fmt="none", ecolor=COLOR["inventory"], lw=1.0, capsize=2)
    ax.set_xlabel("std of terminal P&L")
    ax.set_ylabel("mean terminal P&L")
    ax.set_title("Risk/return across gamma (x default)")
    ax.legend(loc="lower right", fontsize=7.5)
    ax2.plot([p["multiplier"] for p in pts], [p["terminal_inventory_std"] for p in pts], color=COLOR["inventory"],
             marker="o", ms=5, mec="white", mew=1.0)
    ax2.axhline(s["terminal_inventory_std"], color=COLOR["symmetric"], lw=1.2)
    ax2.text(GAMMA_MULTIPLIERS[0], s["terminal_inventory_std"], " symmetric", va="bottom", fontsize=7.5, color=INK2)
    ax2.set_xscale("log")
    ax2.set_xlabel("gamma / default gamma")
    ax2.set_ylabel("terminal inventory std (lots)")
    ax2.set_title("Inventory risk")
    ax2.set_ylim(0, None)
    fig.tight_layout()
    save(fig, "fig6_gamma_frontier.png")


def fig_volatility_sweep(vol: dict) -> None:
    sig = np.array(vol["sigmas"])
    fig, axes = plt.subplots(2, 2, figsize=(7.6, 6.2))
    (ax, ax_share), (ax_mid, ax_fund) = axes
    for name in STRATEGIES:
        pts = [p["fixed_gamma"][name] for p in vol["points"]]
        m = np.array([p["pnl_mean"] for p in pts])
        se = np.array([p["pnl_mean_se"] for p in pts])
        ax.errorbar(sig, m, yerr=1.96 * se, color=COLOR[name], marker="o", ms=5, mec="white", capsize=2, label=name)
        for a, suffix in ((ax_mid, ""), (ax_fund, "_fund")):
            a.plot(sig, [p["spread_pnl" + suffix]["mean"] for p in pts], color=COLOR[name], marker="o", ms=4,
                   mec="white", label=f"{name}: spread part")
            a.plot(sig, [p["inventory_pnl" + suffix]["mean"] for p in pts], color=COLOR[name], marker="s", ms=4,
                   mec="white", ls="--", label=f"{name}: inventory part")
        ax_share.plot(sig, [p["limit_taker_qty_share"] for p in pts], color=COLOR[name], marker="o", ms=5,
                      mec="white", label=name)
    for a in axes.flat:
        a.set_xlim(0, sig.max() * 1.05)
        a.set_xlabel("fundamental volatility (ticks per step)")
    for a in (ax, ax_mid, ax_fund):
        a.axhline(0, color="#c3c2b7", lw=0.8)
    ax.set_ylabel("ticks x lots")
    ax.set_title("Mean terminal P&L (95% CI)")
    ax.legend(loc="lower left", fontsize=8)
    ax_share.set_ylabel("share of filled lots")
    ax_share.set_title("Lots taken by crossing limit orders")
    ax_share.set_ylim(0, None)
    ax_share.legend(loc="upper left", fontsize=8)
    ax_mid.set_ylabel("ticks x lots")
    ax_mid.set_title("Attribution, marked at the book mid")
    ax_mid.legend(loc="lower left", fontsize=6.5)
    ax_fund.set_title("Attribution, marked at the fundamental")
    ax_fund.sharey(ax_mid)
    fig.tight_layout()
    save(fig, "fig7_volatility_sweep.png")


def fig_markouts(adverse: dict) -> None:
    h = np.array(MARKOUT_HORIZONS)
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.6, 3.5))
    for name in STRATEGIES:
        t = adverse[name]["markouts"]["all"]
        ax.plot(h, [t["markout_mid"][str(x)] for x in h], color=COLOR[name], marker="o", ms=4, mec="white",
                label=f"{name}: book mid")
        ax.plot(h, [t["markout_fundamental"][str(x)] for x in h], color=COLOR[name], marker="s", ms=4, mec="white",
                ls="--", label=f"{name}: fundamental")
    t = adverse["inventory"]["markouts"]
    for group, color, label in (("market", ACCENT, "taker was a market order"),
                                ("limit", VIOLET, "taker was a crossing limit order")):
        share = t[group]["share_of_filled_qty"]
        ax2.plot(h, [t[group]["markout_mid"][str(x)] for x in h], color=color, marker="o", ms=4, mec="white",
                 label=f"{label} ({share:.0%} of lots)")
    for a in (ax, ax2):
        a.axhline(0, color="#c3c2b7", lw=0.8)
        a.set_xscale("symlog", linthresh=1)
        a.set_xlim(0, 120)
        a.set_xticks([0, 1, 2, 5, 10, 20, 50, 100], ["0", "1", "2", "5", "10", "20", "50", "100"])
        a.minorticks_off()
        a.set_xlabel("steps after the fill, h")
    ax.set_ylabel("mean signed move per lot (ticks)\npositive = in our favour")
    ax.set_title("Markouts, all fills")
    ax.legend(loc="upper left", fontsize=7)
    ax2.set_ylabel("ticks")
    ax2.set_title("Book-mid markouts by taker, inventory strategy")
    ax2.legend(loc="lower left", fontsize=7)
    fig.tight_layout()
    save(fig, "fig8_markouts.png")


# ============================================================================

def main() -> None:
    global POOL
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    timings = {}
    summary: dict = {}
    config = FlowConfig()
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as POOL:
        say("Stage-4 analysis. Units: prices in ticks, quantity in lots, P&L in ticks x lots.")
        say(f"Flow: default FlowConfig ({config.n_steps} measured steps after {config.warmup_steps} warm-up, "
            f"fundamental sigma {config.sigma} ticks/step). Workers: {os.cpu_count()}.")

        t = time.perf_counter()
        summary["exp1_pure_model_paper"] = exp1_paper_reproduction()
        timings["exp1"] = time.perf_counter() - t

        t = time.perf_counter()
        fit, summary["exp2_calibration"] = exp2_calibration(config)
        timings["exp2"] = time.perf_counter() - t

        k = r(fit.k, 4)
        gamma0 = default_gamma(config)
        params = episode_params(config, gamma0, k)
        summary["quoting_parameters"] = {
            "gamma": gamma0, "gamma_rule": "q*gamma*sigma^2*T = 0.75 ticks for q = one quote (5 lots)",
            "sigma_per_sqrt_episode": r(params.sigma, 4), "k_per_tick": k, "quote_size": QUOTE_SIZE,
            "spread_at_start_ticks": r(params.gamma * params.sigma ** 2 + 2 / params.gamma * math.log(1 + params.gamma / k), 4),
            "spread_at_end_ticks": r(2 / params.gamma * math.log(1 + params.gamma / k), 4)}
        say(f"Quoting parameters: gamma = {gamma0:.6g}, sigma = {params.sigma:.4f} ticks/sqrt(episode), k = {k}; "
            f"model spread {summary['quoting_parameters']['spread_at_start_ticks']} ticks at t=0, "
            f"{summary['quoting_parameters']['spread_at_end_ticks']} at t=T")

        t = time.perf_counter()
        main_stats, main_eps = exp3_main(config, params)
        summary["exp3_main"] = main_stats
        timings["exp3"] = time.perf_counter() - t

        _, summary["exp2b_validation"] = exp2b_validate(fit, main_eps)

        t = time.perf_counter()
        summary["exp4_pure_vs_book"] = exp4_pure_vs_book(config, params, fit, main_stats, summary["exp2b_validation"])
        timings["exp4"] = time.perf_counter() - t

        t = time.perf_counter()
        summary["exp5_adverse_selection"] = exp5_adverse_selection(main_eps, main_stats)
        summary["exp5b_volatility_sweep"] = exp5b_volatility_sweep(config, gamma0)
        timings["exp5"] = time.perf_counter() - t

        t = time.perf_counter()
        summary["exp6_gamma_sweep"] = exp6_gamma_sweep(config, gamma0, k)
        timings["exp6"] = time.perf_counter() - t

        summary["exp7_inventory_over_time"] = exp7_inventory_over_time(main_eps, config, params)

    t = time.perf_counter()
    fig_example_episode(config, params)
    fig_pnl_distribution(main_eps, main_stats)
    fig_inventory_over_time(summary["exp7_inventory_over_time"], config, params)
    fig_calibration(summary["exp2_calibration"], fit, summary["exp2b_validation"])
    fig_pure_vs_book(summary["exp4_pure_vs_book"])
    fig_gamma_frontier(summary["exp6_gamma_sweep"])
    fig_volatility_sweep(summary["exp5b_volatility_sweep"])
    fig_markouts(summary["exp5_adverse_selection"])
    timings["figures"] = time.perf_counter() - t

    total = time.perf_counter() - started
    summary["timings_seconds"] = {key: r(v, 1) for key, v in timings.items()} | {"total": r(total, 1)}
    say("=" * 78)
    say("Timings (s): " + ", ".join(f"{key} {v}" for key, v in summary["timings_seconds"].items()))
    with open(OUT / "summary.json", "w") as f:
        json.dump(summary, f, indent=1)
    (OUT / "report.txt").write_text("\n".join(LINES) + "\n")


if __name__ == "__main__":
    main()
