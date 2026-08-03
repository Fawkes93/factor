"""Pure-logic tests for breakeven-at-half-TP."""

from factor.services.breakeven import (
    breakeven_sl_price,
    favorable_distance,
    mark_price_for_side,
    should_move_to_breakeven,
    sl_is_worse_than_be,
    tp_distance_price,
)


def test_favorable_buy_and_sell():
    assert favorable_distance(side="BUY", entry=2000.0, mark=2005.0) == 5.0
    assert favorable_distance(side="SELL", entry=2000.0, mark=1995.0) == 5.0
    assert favorable_distance(side="BUY", entry=2000.0, mark=1990.0) == -10.0


def test_tp_distance_from_price_or_points():
    assert tp_distance_price(side="BUY", entry=2000.0, tp_price=2010.0) == 10.0
    # 1000 pts * 0.01 = 10.0
    assert (
        tp_distance_price(
            side="BUY", entry=2000.0, tp_points=1000, point_size=0.01
        )
        == 10.0
    )


def test_breakeven_sl_with_buffer():
    assert breakeven_sl_price(side="BUY", entry=2000.0) == 2000.0
    assert breakeven_sl_price(
        side="BUY", entry=2000.0, buffer_points=5, point_size=0.01
    ) == 2000.05
    assert breakeven_sl_price(
        side="SELL", entry=2000.0, buffer_points=5, point_size=0.01
    ) == 1999.95


def test_should_move_buy_half_tp():
    # entry 2000, TP +10 (1000 pts * 0.01), half = +5 → mark 2005
    ok, be, reason = should_move_to_breakeven(
        side="BUY",
        entry=2000.0,
        mark=2005.0,
        tp_points=1000,
        point_size=0.01,
        fraction=0.5,
        current_sl=1990.0,
    )
    assert ok is True
    assert be == 2000.0
    assert "trigger" in reason


def test_should_not_move_before_half():
    ok, be, reason = should_move_to_breakeven(
        side="BUY",
        entry=2000.0,
        mark=2004.0,
        tp_points=1000,
        point_size=0.01,
        fraction=0.5,
        current_sl=1990.0,
    )
    assert ok is False
    assert be is None
    assert "not_yet" in reason


def test_should_not_move_if_already_applied():
    ok, _, reason = should_move_to_breakeven(
        side="BUY",
        entry=2000.0,
        mark=2010.0,
        tp_points=1000,
        point_size=0.01,
        already_applied=True,
    )
    assert ok is False
    assert reason == "already_applied"


def test_should_not_move_if_sl_already_better():
    ok, _, reason = should_move_to_breakeven(
        side="BUY",
        entry=2000.0,
        mark=2005.0,
        tp_points=1000,
        point_size=0.01,
        current_sl=2000.0,  # already at BE
    )
    assert ok is False
    assert "already" in reason


def test_sell_half_tp():
    ok, be, _ = should_move_to_breakeven(
        side="SELL",
        entry=2000.0,
        mark=1995.0,
        tp_points=1000,
        point_size=0.01,
        fraction=0.5,
        current_sl=2010.0,
    )
    assert ok is True
    assert be == 2000.0


def test_mark_price_conservative():
    assert mark_price_for_side(side="BUY", bid=1.1, ask=1.2) == 1.1
    assert mark_price_for_side(side="SELL", bid=1.1, ask=1.2) == 1.2


def test_sl_is_worse():
    assert sl_is_worse_than_be(side="BUY", current_sl=1990.0, be_sl=2000.0) is True
    assert sl_is_worse_than_be(side="BUY", current_sl=2000.0, be_sl=2000.0) is False
    assert sl_is_worse_than_be(side="SELL", current_sl=2010.0, be_sl=2000.0) is True
