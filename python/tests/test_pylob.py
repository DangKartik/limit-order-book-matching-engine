"""Tests for the Python bindings.

The matching rules are tested thoroughly in C++ (tests/). These tests check the
binding layer: that types convert correctly, that errors surface as Python
exceptions, and that the two bound book types behave identically from Python.
"""
import random

import pytest

import pylob
from pylob import Side

BOOK_TYPES = [pylob.OrderBook, pylob.BaselineOrderBook]


@pytest.fixture(params=BOOK_TYPES, ids=lambda cls: cls.__name__)
def book(request):
    return request.param()


def test_empty_book(book):
    assert book.best_bid() is None      # std::optional<Price> empty -> None
    assert book.best_ask() is None
    assert len(book) == 0
    assert book.depth(Side.Buy) == []


def test_limit_order_rests_and_returns_result_and_trades(book):
    result, trades = book.add_limit(Side.Buy, 100, 10)
    assert (result.id, result.filled, result.resting) == (1, 0, 10)
    assert result.accepted
    assert trades == []
    assert book.best_bid() == 100
    assert book.quantity_at(Side.Buy, 100) == 10


def test_crossing_order_produces_trade_at_maker_price(book):
    maker, _ = book.add_limit(Side.Sell, 100, 10)
    taker, trades = book.add_limit(Side.Buy, 105, 4)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.maker_id == maker.id
    assert trade.taker_id == taker.id
    assert trade.price == 100
    assert trade.qty == 4
    assert trade.taker_side == Side.Buy
    assert taker.filled == 4 and taker.resting == 0
    assert book.quantity_at(Side.Sell, 100) == 6


def test_market_order_never_rests(book):
    book.add_limit(Side.Sell, 100, 5)
    result, trades = book.add_market(Side.Buy, 8)
    assert result.filled == 5 and result.resting == 0
    assert len(trades) == 1
    assert len(book) == 0


def test_cancel(book):
    result, _ = book.add_limit(Side.Buy, 100, 10)
    assert book.cancel(result.id) is True
    assert book.cancel(result.id) is False
    assert book.cancel(12345) is False


def test_trades_from_one_call_do_not_leak_into_the_next(book):
    # The wrapper reuses one C++ buffer; each call must return only its own trades.
    book.add_limit(Side.Sell, 100, 1)
    book.add_limit(Side.Sell, 100, 1)
    _, first = book.add_market(Side.Buy, 1)
    _, second = book.add_market(Side.Buy, 1)
    assert [t.maker_id for t in first] == [1]
    assert [t.maker_id for t in second] == [2]
    # The earlier list is an independent copy, not a view of the reused buffer.
    assert [t.maker_id for t in first] == [1]


def test_rejections_are_results_not_exceptions(book):
    result, trades = book.add_limit(Side.Buy, 100, 0)
    assert not result.accepted
    assert result.id == 0
    assert result.reject == pylob.RejectReason.InvalidQuantity
    assert trades == []

    result, _ = book.add_limit(Side.Buy, book.max_price + 1, 1)
    assert result.reject == pylob.RejectReason.InvalidPrice


def test_depth_returns_levels_best_first(book):
    book.add_limit(Side.Buy, 100, 5)
    book.add_limit(Side.Buy, 100, 3)
    book.add_limit(Side.Buy, 98, 7)
    levels = book.depth(Side.Buy, max_levels=5)
    assert [(l.price, l.qty, l.order_count) for l in levels] == [(100, 8, 2), (98, 7, 1)]


@pytest.mark.parametrize("cls", BOOK_TYPES, ids=lambda cls: cls.__name__)
def test_constructor_takes_price_range(cls):
    book = cls(min_price=50, max_price=60)
    assert (book.min_price, book.max_price) == (50, 60)
    assert not book.add_limit(Side.Buy, 49, 1)[0].accepted
    assert book.add_limit(Side.Buy, 50, 1)[0].accepted


@pytest.mark.parametrize("cls", BOOK_TYPES, ids=lambda cls: cls.__name__)
def test_invalid_range_raises_value_error(cls):
    # The C++ constructor throws std::invalid_argument; pybind11 maps it to ValueError.
    with pytest.raises(ValueError):
        cls(min_price=10, max_price=5)


def test_wrong_argument_types_raise_type_error(book):
    with pytest.raises(TypeError):
        book.add_limit("buy", 100, 1)
    with pytest.raises(TypeError):
        book.add_limit(Side.Buy, 100.5, 1)   # prices are integer ticks; floats are refused


def test_optimised_and_baseline_books_agree_on_a_random_stream():
    rng = random.Random(7)
    fast = pylob.OrderBook(min_price=90, max_price=110)
    base = pylob.BaselineOrderBook(min_price=90, max_price=110)
    last_id = 0
    total_trades = 0

    for _ in range(20_000):
        roll = rng.random()
        side = Side.Buy if rng.random() < 0.5 else Side.Sell
        if roll < 0.55:
            args = (side, rng.randint(88, 112), rng.randint(1, 20))
            (ra, ta), (rb, tb) = fast.add_limit(*args), base.add_limit(*args)
        elif roll < 0.65:
            args = (side, rng.randint(1, 20))
            (ra, ta), (rb, tb) = fast.add_market(*args), base.add_market(*args)
        else:
            target = rng.randint(max(1, last_id - 50), max(1, last_id))
            assert fast.cancel(target) == base.cancel(target)
            continue
        assert ra == rb
        assert ta == tb
        total_trades += len(ta)
        last_id = max(last_id, ra.id)
        assert fast.best_bid() == base.best_bid()
        assert fast.best_ask() == base.best_ask()

    assert len(fast) == len(base)
    assert total_trades > 1000   # the stream really did trade
