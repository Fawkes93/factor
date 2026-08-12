"""Pure-logic tests for breakeven + position reconcile."""

from types import SimpleNamespace

from factor.services.breakeven import (
    breakeven_sl_price,
    build_breakeven_order_detail,
    clamp_breakeven_fraction,
    favorable_distance,
    mark_price_for_side,
    process_open_order_for_be,
    reconcile_order_vs_positions,
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


def test_clamp_breakeven_fraction():
    assert clamp_breakeven_fraction(0.9) == 0.9
    assert clamp_breakeven_fraction(0) == 0.5
    assert clamp_breakeven_fraction(1.5) == 0.5
    assert clamp_breakeven_fraction("0.75") == 0.75


def test_build_detail_uses_pref_fraction():
    plan = SimpleNamespace(tp_points=1000, sl_points=1000, point_size=0.01)
    d = build_breakeven_order_detail(
        plan=plan,
        pref={"breakeven_half_tp": True, "breakeven_fraction": 0.9},
    )
    assert d["enabled"] is True
    assert d["fraction"] == 0.9
    d2 = build_breakeven_order_detail(
        plan=plan,
        pref={"breakeven_half_tp": True},
    )
    assert d2["fraction"] == 0.5


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


def test_should_move_at_custom_fraction():
    # 90% of +10 TP = +9 → mark 2009
    ok, be, reason = should_move_to_breakeven(
        side="BUY",
        entry=2000.0,
        mark=2009.0,
        tp_points=1000,
        point_size=0.01,
        fraction=0.9,
        current_sl=1990.0,
    )
    assert ok is True
    assert be == 2000.0
    ok2, _, r2 = should_move_to_breakeven(
        side="BUY",
        entry=2000.0,
        mark=2008.0,
        tp_points=1000,
        point_size=0.01,
        fraction=0.9,
        current_sl=1990.0,
    )
    assert ok2 is False
    assert "not_yet" in r2


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


def test_reconcile_closes_when_no_positions(monkeypatch):
    from factor.db import repos

    updated = {}

    def fake_update(oid, **fields):
        updated["id"] = oid
        updated.update(fields)
        return {"id": oid, **fields}

    monkeypatch.setattr(repos, "update_order", fake_update)
    order = {
        "id": "ord-600",
        "kind": "candle",
        "symbol": "XAU/USD",
        "broker_position_id": "42",
        "status": "open",
    }
    r = reconcile_order_vs_positions(order, [], dry=False)
    assert r is not None
    assert r["action"] == "closed_local"
    assert updated["status"] == "closed"
    assert updated["id"] == "ord-600"
    # Without broker, result is unknown but still closed
    assert updated.get("result") in ("unknown", None) or updated["detail"]["outcome"]["result"] == "unknown"


def test_reconcile_closes_when_position_id_gone_others_open(monkeypatch):
    """Order 600 closed on cTrader while another position remains — must free concurrent."""
    from factor.db import repos

    updated = {}
    monkeypatch.setattr(
        repos,
        "update_order",
        lambda oid, **fields: updated.update({"id": oid, **fields}) or {"id": oid, **fields},
    )
    order = {
        "id": "ord-600",
        "kind": "candle",
        "symbol": "XAU/USD",
        "broker_position_id": "100",
        "status": "open",
        "detail": {"breakeven": {"applied": True, "enabled": True, "position_id": 100}},
    }
    other_open = [{"position_id": 200, "side": "BUY", "entry_price": 1.0}]
    r = reconcile_order_vs_positions(order, other_open, dry=False)
    assert r is not None
    assert r["action"] == "closed_local"
    assert "position_gone:100" in r["reason"]
    assert updated["status"] == "closed"


def test_reconcile_keeps_open_when_position_still_there():
    order = {
        "id": "ord-1",
        "broker_position_id": "100",
        "status": "open",
    }
    positions = [{"position_id": 100, "side": "BUY"}]
    assert reconcile_order_vs_positions(order, positions, dry=False) is None


def test_reconcile_dry_run_never_closes():
    order = {"id": "ord-1", "broker_position_id": "1", "status": "open"}
    assert reconcile_order_vs_positions(order, [], dry=True) is None


def test_process_be_applied_still_reconciles_closed(monkeypatch):
    """Regression: after BE applied, cycle used to skip the order entirely."""
    from factor.db import repos

    updated = {}
    monkeypatch.setattr(
        repos,
        "update_order",
        lambda oid, **fields: updated.update({"id": oid, **fields}) or {"id": oid, **fields},
    )

    class FakeBroker:
        _dry_run = False

        def list_open_positions_info(self):
            return []

    order = {
        "id": "ord-600",
        "kind": "candle",
        "symbol": "XAU/USD",
        "side": "BUY",
        "broker_position_id": "55",
        "entry_price": 2000.0,
        "detail": {
            "breakeven": {
                "enabled": True,
                "applied": True,
                "fraction": 0.5,
                "position_id": 55,
            }
        },
    }
    r = process_open_order_for_be(order, broker=FakeBroker(), positions=[])
    assert r["action"] == "closed_local"
    assert updated.get("status") == "closed"


def test_process_be_applied_skip_when_still_open(monkeypatch):
    class FakeBroker:
        _dry_run = False

        def list_open_positions_info(self):
            return []

        def get_spot(self, sym):
            return {"bid": 2005.0, "ask": 2005.1}

    order = {
        "id": "ord-be",
        "kind": "candle",
        "symbol": "XAU/USD",
        "side": "BUY",
        "broker_position_id": "9",
        "entry_price": 2000.0,
        "tp_price": 2010.0,
        "sl_price": 1990.0,
        "detail": {
            "breakeven": {"enabled": True, "applied": True, "fraction": 0.5, "position_id": 9}
        },
    }
    positions = [
        {
            "position_id": 9,
            "side": "BUY",
            "entry_price": 2000.0,
            "stop_loss": 2000.0,
            "take_profit": 2010.0,
            "broker_symbol": "XAUUSD",
        }
    ]
    r = process_open_order_for_be(order, broker=FakeBroker(), positions=positions)
    assert r["action"] == "skip"
    assert r["reason"] == "already_applied"


def test_process_be_moves_sl_when_half_tp_hit(monkeypatch):
    """Full path: open position + spot past half TP → amend + applied."""
    from factor.db import repos

    updated = {}

    def fake_update(oid, **fields):
        updated["id"] = oid
        updated.update(fields)
        return {"id": oid, **fields}

    monkeypatch.setattr(repos, "update_order", fake_update)

    amends = []

    class FakeBroker:
        _dry_run = False

        def list_open_positions_info(self):
            return []

        def get_spot(self, sym):
            assert sym == "XAUUSD"
            return {"bid": 2005.0, "ask": 2005.2}

        def amend_position_sl_tp(self, *, position_id, stop_loss, take_profit):
            amends.append(
                {
                    "position_id": position_id,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                }
            )
            return {"ok": True}

    order = {
        "id": "ord-move",
        "kind": "candle",
        "symbol": "XAU/USD",
        "side": "BUY",
        "broker_position_id": "42",
        "entry_price": 2000.0,
        "tp_price": 2010.0,
        "sl_price": 1990.0,
        "detail": {
            "breakeven": {
                "enabled": True,
                "applied": False,
                "fraction": 0.5,
                "tp_points": 1000,
                "sl_points": 1000,
                "point_size": 0.01,
            },
            "broker_detail": {
                "plan": {
                    "broker_symbol": "XAUUSD",
                    "tp_points": 1000,
                    "sl_points": 1000,
                    "point_size": 0.01,
                }
            },
        },
    }
    positions = [
        {
            "position_id": 42,
            "side": "BUY",
            "entry_price": 2000.0,
            "stop_loss": 1990.0,
            "take_profit": 2010.0,
            "broker_symbol": "XAUUSD",
        }
    ]
    r = process_open_order_for_be(order, broker=FakeBroker(), positions=positions)
    assert r["action"] == "moved"
    assert r["be_sl"] == 2000.0
    assert len(amends) == 1
    assert amends[0]["stop_loss"] == 2000.0
    assert amends[0]["take_profit"] == 2010.0
    assert updated.get("sl_price") == 2000.0
    assert updated["detail"]["breakeven"]["applied"] is True


def test_process_be_skips_before_half_and_records_reason(monkeypatch):
    from factor.db import repos

    updated = {}
    monkeypatch.setattr(
        repos,
        "update_order",
        lambda oid, **fields: updated.update({"id": oid, **fields}) or {"id": oid, **fields},
    )

    class FakeBroker:
        _dry_run = False

        def get_spot(self, sym):
            return {"bid": 2002.0, "ask": 2002.1}

    order = {
        "id": "ord-early",
        "kind": "candle",
        "symbol": "XAU/USD",
        "side": "BUY",
        "broker_position_id": "7",
        "entry_price": 2000.0,
        "tp_price": 2010.0,
        "sl_price": 1990.0,
        "detail": {
            "breakeven": {
                "enabled": True,
                "applied": False,
                "fraction": 0.5,
                "tp_points": 1000,
                "point_size": 0.01,
            }
        },
    }
    positions = [
        {
            "position_id": 7,
            "side": "BUY",
            "entry_price": 2000.0,
            "stop_loss": 1990.0,
            "take_profit": 2010.0,
            "broker_symbol": "XAUUSD",
        }
    ]
    r = process_open_order_for_be(order, broker=FakeBroker(), positions=positions)
    assert r["action"] == "skip"
    assert "not_yet" in r["reason"]
    assert updated["detail"]["breakeven"]["last_reason_key"] == "not_yet"


def test_sane_absolute_sl_drops_relative_protocol():
    from factor.services.breakeven import _sane_absolute_sl

    # relative 1000 pts * 100_000 = 1_000_000 must not block BE
    assert (
        _sane_absolute_sl(
            side="BUY", entry=2000.0, candidate=1_000_000.0, fallback=1990.0
        )
        == 1990.0
    )
    assert _sane_absolute_sl(side="BUY", entry=2000.0, candidate=1990.0) == 1990.0
