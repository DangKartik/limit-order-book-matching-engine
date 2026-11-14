"""A market maker that rests quotes in the real book and keeps its own books.

It holds at most one bid and one ask. Every step the simulator hands it the
trades produced by the background flow; the ones that hit its orders change its
inventory and cash. At the end of the step it asks a quoting function (either
Avellaneda-Stoikov or the symmetric benchmark) where to quote and moves its
orders there.

It is only ever a maker: every quote it posts is checked to be passive, so it
never pays the spread and never appears as the taker in a trade.
"""
import math
from collections.abc import Callable
from dataclasses import dataclass

import pylob
from pylob import Side

from mm.avellaneda_stoikov import ASParams

# Same signature as avellaneda_stoikov.inventory_quotes / symmetric_quotes.
Quoter = Callable[[float, float, float, ASParams], tuple[float, float]]


@dataclass
class LiveQuote:
    order_id: int
    price: int
    remaining: int


class MarketMaker:
    def __init__(self, book: pylob.OrderBook, quoter: Quoter, params: ASParams, quote_size: int = 5):
        self.book = book
        self.quoter = quoter
        self.params = params
        self.quote_size = quote_size
        self.inventory = 0   # units held; negative means short
        self.cash = 0        # ticks x units; an int, so accounting is exact
        self.bid: LiveQuote | None = None
        self.ask: LiveQuote | None = None
        self.fills: list[tuple[int, Side, int, int]] = []   # (step, side we traded, price, qty)
        self.step = 0        # set by the simulator so fills can be stamped with it

    def on_trades(self, trades: list[pylob.Trade]) -> None:
        """Apply the trades from one background order to our quotes, if any were hit."""
        for trade in trades:
            if self.bid is not None and trade.maker_id == self.bid.order_id:
                self.inventory += trade.qty
                self.cash -= trade.price * trade.qty
                self.bid = self._record_fill(self.bid, Side.Buy, trade)
            elif self.ask is not None and trade.maker_id == self.ask.order_id:
                self.inventory -= trade.qty
                self.cash += trade.price * trade.qty
                self.ask = self._record_fill(self.ask, Side.Sell, trade)

    def _record_fill(self, quote: LiveQuote, side: Side, trade: pylob.Trade) -> LiveQuote | None:
        self.fills.append((self.step, side, trade.price, trade.qty))
        quote.remaining -= trade.qty
        # A fully filled order is gone from the book, so we must forget it too.
        return quote if quote.remaining > 0 else None

    def background_touch(self) -> tuple[int | None, int | None]:
        """Best bid and ask of everyone except us.

        We quote around the mid of this touch, not of the full book. Otherwise,
        once our own quote became the best price, the mid would move towards it
        and we would end up chasing our own orders.
        """
        return (self._background_best(Side.Buy, self.bid),
                self._background_best(Side.Sell, self.ask))

    def _background_best(self, side: Side, own: LiveQuote | None) -> int | None:
        levels = self.book.depth(side, 2)
        if not levels:
            return None
        top = levels[0]
        # If the whole top level is our order, the background's best is one level down.
        if own is not None and top.price == own.price and top.qty == own.remaining:
            return levels[1].price if len(levels) > 1 else None
        return top.price

    def requote(self, mid: float, time_left: float) -> None:
        model_bid, model_ask = self.quoter(mid, self.inventory, time_left, self.params)
        # Round away from the mid: never quote tighter than the model asks for.
        bid, ask = math.floor(model_bid), math.ceil(model_ask)

        # Stay passive: a bid at or above the background's best ask would trade.
        bg_bid, bg_ask = self.background_touch()
        if bg_ask is not None:
            bid = min(bid, bg_ask - 1)
        if bg_bid is not None:
            ask = max(ask, bg_bid + 1)
        lo, hi = self.book.min_price, self.book.max_price
        bid, ask = min(max(bid, lo), hi), min(max(ask, lo), hi)

        # Cancel every stale quote before posting anything. If the mid jumped,
        # a new bid could otherwise be priced through our own old ask.
        if self.bid is not None and self.bid.price != bid:
            self._cancel(self.bid)
            self.bid = None
        if self.ask is not None and self.ask.price != ask:
            self._cancel(self.ask)
            self.ask = None
        # A quote already at the target price is left alone, even if partly
        # filled: cancelling and reposting it would send it to the back of the
        # queue at that price and lose its time priority.
        if self.bid is None:
            self.bid = self._post(Side.Buy, bid)
        if self.ask is None:
            self.ask = self._post(Side.Sell, ask)

    def _cancel(self, quote: LiveQuote) -> None:
        if not self.book.cancel(quote.order_id):
            raise RuntimeError(f"quote {quote.order_id} was not resting; fill accounting is out of sync")

    def _post(self, side: Side, price: int) -> LiveQuote:
        result, trades = self.book.add_limit(side, price, self.quote_size)
        # A real exception rather than `assert`, which `python -O` would strip.
        if trades or not result.accepted:
            raise RuntimeError(f"quote {side} @ {price} was not passive: {trades}")
        return LiveQuote(result.id, price, result.resting)
