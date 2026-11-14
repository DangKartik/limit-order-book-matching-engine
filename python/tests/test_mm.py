"""Tests for the market-making simulator: formulas, tape, market maker, episode."""
import math
from collections import Counter

import numpy as np
import pytest

import pylob
from pylob import Side

import mm.simulate
from mm.avellaneda_stoikov import ASParams, inventory_quotes, reservation_price, symmetric_quotes
from mm.flow import FlowConfig, generate_tape
from mm.market_maker import MarketMaker
from mm.simulate import run_episode

CONFIG = FlowConfig()
SIGMA = CONFIG.sigma * math.sqrt(CONFIG.n_steps)
PARAMS = ASParams(gamma=0.75 / (5 * SIGMA ** 2), sigma=SIGMA, k=0.7)


# --- Avellaneda-Stoikov formulas -------------------------------------------

def test_reservation_price_is_mid_at_zero_inventory():
    p = ASParams(gamma=0.1, sigma=2.0, k=1.5)
    assert reservation_price(100.0, 0, 0.7, p) == 100.0


def test_long_inventory_lowers_both_quotes():
    p = ASParams(gamma=0.1, sigma=2.0, k=1.5)
    flat_bid, flat_ask = inventory_quotes(100.0, 0, 1.0, p)
    long_bid, long_ask = inventory_quotes(100.0, 3, 1.0, p)
    assert long_bid < flat_bid and long_ask < flat_ask


def test_spread_value_from_the_paper_parameters():
    p = ASParams(gamma=0.1, sigma=2.0, k=1.5)
    bid, ask = inventory_quotes(100.0, 0, 1.0, p)
    assert ask - bid == pytest.approx(1.69077, abs=5e-6)


def test_symmetric_and_inventory_quotes_have_equal_spread():
    p = ASParams(gamma=0.1, sigma=2.0, k=1.5)
    bid_i, ask_i = inventory_quotes(100.0, 4, 0.5, p)
    bid_s, ask_s = symmetric_quotes(100.0, 4, 0.5, p)
    assert ask_i - bid_i == pytest.approx(ask_s - bid_s)
    assert (bid_s + ask_s) / 2 == pytest.approx(100.0)


# --- Tape ------------------------------------------------------------------

def test_tape_is_deterministic_in_the_seed():
    assert generate_tape(CONFIG, 7) == generate_tape(CONFIG, 7)
    assert generate_tape(CONFIG, 7) != generate_tape(CONFIG, 8)


def test_tape_is_independent_of_the_strategy():
    tape = generate_tape(CONFIG, 3)
    run_episode(tape, CONFIG, inventory_quotes, PARAMS)
    run_episode(tape, CONFIG, symmetric_quotes, PARAMS)
    assert tape == generate_tape(CONFIG, 3)   # running a strategy did not consume or alter it


# --- Market maker on a hand-built book ---------------------------------------

def fixed_quoter(bid: float, ask: float):
    return lambda mid, inventory, time_left, p: (bid, ask)


@pytest.fixture
def small_book():
    book = pylob.OrderBook(1, 1000)
    book.add_limit(Side.Buy, 98, 2)    # background bid
    book.add_limit(Side.Sell, 105, 4)  # background ask
    return book


def test_background_touch_excludes_own_quote(small_book):
    mm_ = MarketMaker(small_book, fixed_quoter(100.0, 103.0), PARAMS)
    mm_.requote(101.5, 1.0)
    assert (small_book.best_bid(), small_book.best_ask()) == (100, 103)
    assert mm_.background_touch() == (98, 105)
    # Once someone else joins our price level, that price is background too.
    small_book.add_limit(Side.Buy, 100, 2)
    assert mm_.background_touch() == (100, 105)


def test_background_touch_is_none_when_we_are_alone_on_a_side():
    book = pylob.OrderBook(1, 1000)
    book.add_limit(Side.Sell, 105, 4)
    mm_ = MarketMaker(book, fixed_quoter(100.0, 103.0), PARAMS)
    mm_.requote(102.0, 1.0)
    assert mm_.background_touch() == (None, 105)


def test_requote_keeps_order_id_when_price_unchanged(small_book):
    mm_ = MarketMaker(small_book, fixed_quoter(100.2, 102.6), PARAMS)
    mm_.requote(101.5, 1.0)
    bid_id, ask_id = mm_.bid.order_id, mm_.ask.order_id
    mm_.requote(101.5, 0.5)
    assert (mm_.bid.order_id, mm_.ask.order_id) == (bid_id, ask_id)
    mm_.quoter = fixed_quoter(99.0, 102.6)   # only the bid moves
    mm_.requote(101.5, 0.5)
    assert mm_.bid.order_id != bid_id and mm_.ask.order_id == ask_id
    assert small_book.quantity_at(Side.Buy, 100) == 0   # the old bid was cancelled


def test_requote_rounds_outwards_and_stays_passive(small_book):
    mm_ = MarketMaker(small_book, fixed_quoter(100.2, 102.1), PARAMS)
    mm_.requote(101.5, 1.0)
    assert (mm_.bid.price, mm_.ask.price) == (100, 103)   # floor and ceil
    mm_.quoter = fixed_quoter(107.0, 108.0)   # the model wants to bid through the ask at 105
    mm_.requote(101.5, 1.0)
    assert mm_.bid.price == 104 and mm_.ask.price == 108
    assert small_book.quantity_at(Side.Sell, 105) == 4   # nothing traded


def test_partial_fills(small_book):
    mm_ = MarketMaker(small_book, fixed_quoter(100.0, 103.0), PARAMS)
    mm_.requote(101.5, 1.0)
    mm_.step = 11
    _, trades = small_book.add_market(Side.Sell, 2)
    mm_.on_trades(trades)
    assert (mm_.inventory, mm_.cash, mm_.bid.remaining) == (2, -200, 3)
    bid_id = mm_.bid.order_id
    mm_.requote(101.5, 1.0)                  # same price: the partly filled order stays
    assert (mm_.bid.order_id, mm_.bid.remaining) == (bid_id, 3)

    mm_.step = 12
    _, trades = small_book.add_market(Side.Sell, 4)   # 3 from us at 100, 1 from the background at 98
    mm_.on_trades(trades)
    assert (mm_.inventory, mm_.cash, mm_.bid) == (5, -500, None)
    assert mm_.fills == [(11, Side.Buy, 100, 2), (12, Side.Buy, 100, 3)]


# --- Whole episodes ----------------------------------------------------------

class AuditedMarketMaker(MarketMaker):
    """Tracks our orders independently of MarketMaker's own fields, to check them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.all_trades: list[pylob.Trade] = []   # every trade the episode produced
        self.side_of: dict[int, Side] = {}        # every order we posted
        self.open_qty: Counter = Counter()        # our view of each order's remaining qty
        self.violations: list[dict] = []          # moments with two live orders on one side

    def on_trades(self, trades):
        self.all_trades.extend(trades)
        for t in trades:
            if t.maker_id in self.open_qty:
                self.open_qty[t.maker_id] -= t.qty
        super().on_trades(trades)

    def _post(self, side, price):
        quote = super()._post(side, price)
        self.side_of[quote.order_id] = side
        self.open_qty[quote.order_id] = quote.remaining
        live = Counter(self.side_of[i] for i, q in self.open_qty.items() if q > 0)
        if max(live.values()) > 1:
            self.violations.append(dict(live))
        return quote

    def _cancel(self, quote):
        super()._cancel(quote)
        self.open_qty[quote.order_id] = 0


@pytest.fixture(scope="module")
def audited_episode():
    created: list[AuditedMarketMaker] = []

    def make(*args, **kwargs):
        created.append(AuditedMarketMaker(*args, **kwargs))
        return created[-1]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mm.simulate, "MarketMaker", make)
        result = run_episode(generate_tape(CONFIG, 1), CONFIG, inventory_quotes, PARAMS)
    return result, created[0]


def test_episode_has_fills(audited_episode):
    result, _ = audited_episode
    assert len(result.fills) > 50


def test_pnl_identity_is_exact(audited_episode):
    result, _ = audited_episode
    change = np.diff(result.pnl, prepend=0.0)
    # Exact, not approximate: every term is a multiple of 0.5 (see simulate.py).
    assert np.array_equal(change, result.spread_pnl + result.inventory_pnl)


def test_inventory_and_cash_match_fills(audited_episode):
    result, _ = audited_episode
    sign = {Side.Buy: 1, Side.Sell: -1}
    assert result.inventory[-1] == sum(sign[s] * q for _, s, _, q in result.fills)
    assert result.cash[-1] == -sum(sign[s] * p * q for _, s, p, q in result.fills)


def test_market_maker_is_never_taker_and_has_one_order_per_side(audited_episode):
    result, audit = audited_episode
    ours = audit.side_of.keys()
    assert sum(1 for t in audit.all_trades if t.maker_id in ours) == len(result.fills)
    assert not any(t.taker_id in ours for t in audit.all_trades)
    assert audit.violations == []


def test_no_market_maker_means_no_inventory():
    result = run_episode(generate_tape(CONFIG, 2), CONFIG)
    assert result.fills == []
    assert not result.inventory.any() and not result.cash.any()
    assert result.reach_buy is not None and np.isfinite(result.reach_buy).any()


def test_same_seed_same_strategy_same_result():
    a = run_episode(generate_tape(CONFIG, 5), CONFIG, symmetric_quotes, PARAMS)
    b = run_episode(generate_tape(CONFIG, 5), CONFIG, symmetric_quotes, PARAMS)
    assert a.fills == b.fills
    for field in ("mid", "bid_quote", "ask_quote", "inventory", "cash", "pnl", "background_spread"):
        assert np.array_equal(getattr(a, field), getattr(b, field), equal_nan=True), field
