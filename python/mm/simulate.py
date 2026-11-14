"""One episode: replay a tape of background orders through a fresh book.

Each step:
  (a) cancel the background orders whose lifetime ends now,
  (b) submit the step's background limit orders, then its market orders,
      giving every resulting trade list to the market maker,
  (c) after warm-up, let the market maker requote around the background mid,
  (d) record the state.

Timing: the market maker requotes at the END of step t, so its quotes meet
step t+1's flow, which is priced off a fundamental that has already moved. It
reacts one step late, and that lag is what exposes it to adverse selection.

With quoter=None there is no market maker. That mode is for calibration: it
also records how far aggressive flow reached from the mid, which a later script
uses to fit the fill curve lambda(delta) = A * exp(-k * delta).
"""
import math
from dataclasses import dataclass

import numpy as np

import pylob
from pylob import Side

from mm.avellaneda_stoikov import ASParams
from mm.flow import FlowConfig, Tape
from mm.market_maker import MarketMaker, Quoter


@dataclass
class EpisodeResult:
    # Arrays over the measured steps (index 0 is the first step after warm-up).
    mid: np.ndarray                 # background mid at the end of the step (last value carried if a side is empty)
    fundamental: np.ndarray
    bid_quote: np.ndarray           # our live quote prices after requoting, NaN if none
    ask_quote: np.ndarray
    inventory: np.ndarray
    cash: np.ndarray
    pnl: np.ndarray                 # cash + inventory * mid (mark to market)
    spread_pnl: np.ndarray          # edge of this step's fills versus this step's closing mid
    inventory_pnl: np.ndarray       # previous inventory times the change in mid
    background_spread: np.ndarray   # background best ask - best bid, NaN if a side is empty
    resting_orders: np.ndarray      # orders in the book at the end of the step (ours included)
    fills: list[tuple[int, Side, int, int]]   # (step, side we traded, price, qty)
    reach_buy: np.ndarray | None = None       # calibration mode only, see _reach
    reach_sell: np.ndarray | None = None


def _reach(side: Side, limit_price: int | None, qty: int, trades: list[pylob.Trade], mid: float) -> float:
    """How far from `mid` one aggressive order would have hit a resting quote, in ticks.

    A buy that filled completely only proves it reached its worst trade price. A
    buy that did not fill completely (a limit order that came to rest) would
    have taken anything up to its limit price. Market orders always count as
    complete, since their unfilled remainder is cancelled rather than resting.
    This ignores queue position: a quote at that price might not have been
    reached if others were ahead of it.
    """
    filled = sum(t.qty for t in trades)
    if side == Side.Buy:
        if limit_price is not None and filled < qty:
            return limit_price - mid
        return max(t.price for t in trades) - mid if trades else math.nan
    if limit_price is not None and filled < qty:
        return mid - limit_price
    return mid - min(t.price for t in trades) if trades else math.nan


def run_episode(tape: Tape, config: FlowConfig, quoter: Quoter | None = None,
                params: ASParams | None = None, quote_size: int = 5) -> EpisodeResult:
    if quoter is not None and params is None:
        raise ValueError("a quoter needs ASParams")
    book = pylob.OrderBook(config.min_price, config.max_price)
    mm = MarketMaker(book, quoter, params, quote_size) if quoter is not None else None
    calibrating = mm is None
    on_trades = mm.on_trades if mm is not None else (lambda trades: None)
    cancels_at: dict[int, list[int]] = {}   # step -> engine ids of background orders to cancel then
    mid = float(config.start_price)         # last known background mid
    n = config.n_steps
    rec: dict[str, list] = {key: [] for key in (
        "mid", "bid", "ask", "inventory", "cash", "spread", "resting", "reach_buy", "reach_sell")}

    for t in range(config.total_steps):
        i = t - config.warmup_steps     # measured index; negative during warm-up
        measuring = i >= 0
        if mm is not None:
            mm.step = i
        start_mid = mid
        reach = {Side.Buy: math.nan, Side.Sell: math.nan}

        for order_id in cancels_at.pop(t, []):
            book.cancel(order_id)       # False if it already traded away; that is fine

        for side, price, qty, cancel_step in tape.limits[t]:
            result, trades = book.add_limit(side, price, qty)
            if trades:
                on_trades(trades)
            if result.resting:
                cancels_at.setdefault(cancel_step, []).append(result.id)
            # Aggressive = priced through the start-of-step mid, or traded at all
            # (it can trade below that mid if an earlier order this step moved the book).
            through_mid = price > start_mid if side == Side.Buy else price < start_mid
            if calibrating and measuring and (trades or through_mid):
                reach[side] = max(reach[side], _reach(side, price, qty, trades, start_mid), key=_nan_low)
        for side, qty in tape.markets[t]:
            _, trades = book.add_market(side, qty)
            if trades:
                on_trades(trades)
                if calibrating and measuring:
                    reach[side] = max(reach[side], _reach(side, None, qty, trades, start_mid), key=_nan_low)

        bg_bid, bg_ask = mm.background_touch() if mm is not None else (book.best_bid(), book.best_ask())
        if bg_bid is not None and bg_ask is not None:
            mid = (bg_bid + bg_ask) / 2.0
        if not measuring:
            continue
        if mm is not None:
            # Fraction of the episode during which this quote will be exposed:
            # steps i+1 .. n-1. At the last step it is 0 and the quote never trades.
            mm.requote(mid, (n - 1 - i) / n)

        rec["mid"].append(mid)
        rec["bid"].append(mm.bid.price if mm is not None and mm.bid is not None else math.nan)
        rec["ask"].append(mm.ask.price if mm is not None and mm.ask is not None else math.nan)
        rec["inventory"].append(mm.inventory if mm is not None else 0)
        rec["cash"].append(mm.cash if mm is not None else 0)
        rec["spread"].append(bg_ask - bg_bid if bg_bid is not None and bg_ask is not None else math.nan)
        rec["resting"].append(len(book))
        rec["reach_buy"].append(reach[Side.Buy])
        rec["reach_sell"].append(reach[Side.Sell])

    mids = np.array(rec["mid"])
    inventory = np.array(rec["inventory"], dtype=np.int64)
    cash = np.array(rec["cash"], dtype=np.int64)
    fills = mm.fills if mm is not None else []

    # P&L attribution. With pnl = cash + inventory * mid, the change in pnl over
    # a step splits exactly into: inventory carried in, times the move in mid;
    # plus each fill's edge against the closing mid. Every term is a multiple of
    # 0.5 far below 2**53, so in float64 the identity holds with no rounding.
    previous_inventory = np.concatenate(([0], inventory[:-1]))
    inventory_pnl = previous_inventory * np.diff(mids, prepend=mids[0])
    spread_pnl = np.zeros(n)
    for step, side, price, qty in fills:
        edge = mids[step] - price if side == Side.Buy else price - mids[step]
        spread_pnl[step] += edge * qty

    return EpisodeResult(
        mid=mids,
        fundamental=np.array(tape.fundamental[config.warmup_steps:]),
        bid_quote=np.array(rec["bid"]),
        ask_quote=np.array(rec["ask"]),
        inventory=inventory,
        cash=cash,
        pnl=cash + inventory * mids,
        spread_pnl=spread_pnl,
        inventory_pnl=inventory_pnl,
        background_spread=np.array(rec["spread"]),
        resting_orders=np.array(rec["resting"]),
        fills=list(fills),
        reach_buy=np.array(rec["reach_buy"]) if calibrating else None,
        reach_sell=np.array(rec["reach_sell"]) if calibrating else None,
    )


def _nan_low(x: float) -> float:
    """Sort key for max(): a NaN ("no aggressive order yet") loses to any number."""
    return -math.inf if math.isnan(x) else x
