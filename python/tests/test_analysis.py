"""Tests for the stage-4 measurement helpers in mm/analysis.py."""
import math

import numpy as np
import pytest

from pylob import Side

from mm.analysis import (attribution, fit_exponential, markouts, pure_symmetric_constant_spread,
                         quote_distance_and_fill, reach_probability, taker_kind)
from mm.avellaneda_stoikov import ASParams, inventory_quotes
from mm.flow import FlowConfig, Tape, generate_tape
from mm.simulate import EpisodeResult, run_episode

CONFIG = FlowConfig()
SIGMA = CONFIG.sigma * math.sqrt(CONFIG.n_steps)
PARAMS = ASParams(gamma=0.75 / (5 * SIGMA ** 2), sigma=SIGMA, k=0.7)


# --- Calibration -------------------------------------------------------------

def test_fit_recovers_exact_exponential():
    deltas = np.arange(0.5, 6.0, 0.5)
    fit = fit_exponential(deltas, 0.37 * np.exp(-0.71 * deltas))
    assert fit.A == pytest.approx(0.37)
    assert fit.k == pytest.approx(0.71)
    assert fit.r2 == pytest.approx(1.0)
    assert fit.max_log_residual < 1e-12


def test_calibration_recovers_known_curve_from_synthetic_reach():
    # On a fraction A of steps aggressive flow arrives and reaches an exponential
    # distance with rate k; on the rest reach is NaN. Then P(reach >= d) = A exp(-k d).
    A, k, n = 0.3, 0.8, 400_000
    rng = np.random.default_rng(1)
    reach = np.where(rng.random(n) < A, rng.exponential(1.0 / k, n), np.nan)
    deltas = np.arange(0.5, 4.01, 0.5)
    fit = fit_exponential(deltas, reach_probability(reach, deltas))
    assert fit.A == pytest.approx(A, rel=0.03)
    assert fit.k == pytest.approx(k, rel=0.03)


def test_reach_probability_counts_nan_steps_as_not_reached():
    reach = np.array([np.nan, 0.5, 1.5, np.nan])
    assert reach_probability(reach, np.array([0.5, 1.0, 2.0])).tolist() == [0.5, 0.25, 0.0]


def test_fit_rejects_zero_probability():
    with pytest.raises(ValueError):
        fit_exponential(np.array([1.0, 2.0]), np.array([0.1, 0.0]))


# --- Markouts and attribution ------------------------------------------------

def test_markout_sign_convention():
    path = np.array([100.0, 101.0, 103.0, 99.0])
    fills = [(0, Side.Buy, 100, 2),    # we bought, price rose: +1 after 1 step, +3 after 2
             (1, Side.Sell, 101, 1)]   # we sold, price rose then fell: -2 after 1 step, +2 after 2
    total, weight = markouts(fills, path, [0, 1, 2, 3])
    assert weight.tolist() == [3, 3, 3, 2]       # the sell's 3-step horizon runs off the end
    assert total.tolist() == [0.0, 2 * 1 - 1 * 2, 2 * 3 + 1 * 2, 2 * -1]


def test_attribution_at_mid_matches_simulator_and_sums_to_pnl():
    result = run_episode(generate_tape(CONFIG, 4), CONFIG, inventory_quotes, PARAMS)
    spread, inventory = attribution(result, result.mid)
    assert np.array_equal(spread, result.spread_pnl)
    assert np.array_equal(inventory, result.inventory_pnl)
    # Against the fundamental the identity is the same, up to float rounding (the fundamental is not a multiple of 0.5).
    spread_f, inventory_f = attribution(result, result.fundamental)
    pnl_f = result.cash + result.inventory * result.fundamental
    assert np.allclose(np.cumsum(spread_f + inventory_f), pnl_f, atol=1e-6)


def test_taker_kind_reads_the_tape():
    config = FlowConfig(warmup_steps=0, n_steps=3)
    tape = Tape(fundamental=[100.0, 100.0, 100.0],
                limits=[[(Side.Sell, 99, 3, 50)], [(Side.Sell, 102, 3, 50)], [(Side.Sell, 98, 1, 50)]],
                markets=[[], [(Side.Sell, 2)], [(Side.Sell, 4)]])
    assert taker_kind(tape, config, (0, Side.Buy, 100, 3)) == "limit"    # crossing limit sell, no market sell
    assert taker_kind(tape, config, (1, Side.Buy, 100, 2)) == "market"   # the limit sell at 102 cannot reach 100
    assert taker_kind(tape, config, (2, Side.Buy, 100, 1)) == "either"
    with pytest.raises(RuntimeError):
        taker_kind(tape, config, (0, Side.Sell, 101, 1))                 # no buyer at all in step 0


def test_quote_distance_uses_previous_mid_and_next_step_fills():
    nan = math.nan
    result = EpisodeResult(
        mid=np.array([100.0, 100.5, 101.0]), fundamental=np.zeros(3),
        bid_quote=np.array([98.0, 99.0, nan]), ask_quote=np.array([102.0, 102.0, 103.0]),
        inventory=np.zeros(3, np.int64), cash=np.zeros(3, np.int64), pnl=np.zeros(3),
        spread_pnl=np.zeros(3), inventory_pnl=np.zeros(3), background_spread=np.zeros(3),
        resting_orders=np.zeros(3), fills=[(1, Side.Buy, 98, 1), (2, Side.Sell, 102, 5)])
    dist, filled = quote_distance_and_fill(result)
    # bids posted at steps 0, 1 (distances 2.0, 1.5), then asks (2.0, 1.5); fills land one step later.
    assert dist.tolist() == [2.0, 1.5, 2.0, 1.5]
    assert filled.tolist() == [True, False, False, True]


# --- The paper's constant-spread benchmark ------------------------------------

def test_constant_spread_benchmark_uses_the_average_spread():
    p = ASParams(gamma=0.1, sigma=2.0, k=1.5)
    spread, pnl, q = pure_symmetric_constant_spread(p, 140.0, n_paths=200, n_steps=200, dt=0.005, seed=0)
    expected = np.mean([0.1 * 4 * (1 - i * 0.005) + 20 * math.log(1 + 0.1 / 1.5) for i in range(200)])
    assert spread == pytest.approx(expected)
    assert pnl.shape == q.shape == (200,)
