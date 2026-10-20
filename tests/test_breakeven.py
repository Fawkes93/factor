"""Pure-logic tests for breakeven + position reconcile."""

from types import SimpleNamespace

import pytest

from factor.broker.ctrader_client import CTraderClient
from factor.rules.presets import price_decimals, round_price
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


def test_tp_distance_prefers_points_over_absolute_price():
    """Fill entry + alert absolute TP must not inflate trigger (live bug)."""
    # fill 4404.14, alert TP 4415.28 → abs dist ~11.14; points say 10.0
    assert (
        tp_distance_price(
            side="BUY",
            entry=4404.14,
            tp_price=4415.28634,
            tp_points=1000,
            point_size=0.01,
        )
        == 10.0
    )
    # Half of 10 = 5; should_move at mark fill+5.3
    ok, be, reason = should_move_to_breakeven(
        side="BUY",
        entry=4404.14,
        mark=4409.44,
        tp_price=4415.28634,
        tp_points=1000,
        point_size=0.01,
        fraction=0.5,
        current_sl=4394.14,
    )
    assert ok is True
    assert be == 4404.14
    assert "trigger" in reason


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


def _eur_be_order():
    return {
        "id": "ord-eur-be",
        "kind": "candle",
        "symbol": "EUR/USD",
        "side": "BUY",
        "broker_position_id": "88",
        "entry_price": 1.12200,
        "tp_price": 1.12500,
        "sl_price": 1.12050,
        "detail": {
            "breakeven": {
                "enabled": True,
                "applied": False,
                "fraction": 0.5,
                "buffer_points": 0,
                "tp_points": 300,
                "sl_points": 150,
                "point_size": 0.00001,
            }
        },
    }


def _eur_be_position():
    return {
        "position_id": 88,
        "side": "BUY",
        "entry_price": 1.12200,
        "stop_loss": 1.12050,
        "take_profit": 1.12500,
        "broker_symbol": "EURUSD",
    }


class _ScaledSpotBroker:
    """get_spot applies the same 1/100000 scale as CTraderClient.get_spot."""

    def __init__(self, bid_raw, ask_raw):
        self._dry_run = False
        self.bid_raw = bid_raw
        self.ask_raw = ask_raw
        self.amends = []

    def get_spot(self, sym):
        assert sym == "EURUSD"
        return {
            "bid": CTraderClient._normalize_spot_quote(self.bid_raw),
            "ask": CTraderClient._normalize_spot_quote(self.ask_raw),
        }

    def amend_position_sl_tp(self, *, position_id, stop_loss, take_profit):
        self.amends.append(
            {
                "position_id": position_id,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        return {"ok": True}


def test_eur_breakeven_uses_scaled_spot(monkeypatch):
    """Protocol 112220 is 1.12220, short of half of a 300-point TP."""
    from factor.db import repos

    monkeypatch.setattr(repos, "update_order", lambda oid, **fields: {"id": oid, **fields})
    # Half of 300 pts × 0.00001 = 0.00150. Unscaled 112220 would look miles past it.
    early = _ScaledSpotBroker(112220, 112222)
    r = process_open_order_for_be(
        _eur_be_order(), broker=early, positions=[_eur_be_position()]
    )
    assert r["action"] == "skip"
    assert "not_yet" in r["reason"]
    assert r["mark"] == pytest.approx(1.12220)
    assert early.amends == []

    # 112460 → 1.12460, past the 1.12350 trigger. SL moves to the EUR entry.
    hit = _ScaledSpotBroker(112460, 112462)
    moved = process_open_order_for_be(
        _eur_be_order(), broker=hit, positions=[_eur_be_position()]
    )
    assert moved["action"] == "moved"
    assert moved["mark"] == pytest.approx(1.12460)
    assert moved["be_sl"] == pytest.approx(1.12200)
    assert hit.amends[0]["stop_loss"] == pytest.approx(1.12200)


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


def test_round_price_matches_symbol_digits():
    assert price_decimals(0.01) == 2
    assert price_decimals(0.00001) == 5
    assert price_decimals(1.0) == 0
    # Live 902 failure: alert TP had too many digits for XAU
    assert round_price(4415.28634, 0.01) == 4415.29
    assert round_price(4404.14, 0.01) == 4404.14


def test_process_be_rounds_tp_on_amend(monkeypatch):
    """Regression: unrounded alert TP caused TRADING_BAD_STOPS on amend."""
    from factor.db import repos

    amends = []
    monkeypatch.setattr(repos, "update_order", lambda oid, **f: {"id": oid, **f})

    class FakeBroker:
        _dry_run = False

        def get_spot(self, sym):
            return {"bid": 2005.0, "ask": 2005.1}

        def amend_position_sl_tp(self, *, position_id, stop_loss, take_profit):
            amends.append({"sl": stop_loss, "tp": take_profit})
            return {"ok": True}

    order = {
        "id": "ord-digits",
        "kind": "candle",
        "symbol": "XAU/USD",
        "side": "BUY",
        "broker_position_id": "1",
        "entry_price": 2000.12345,
        "tp_price": 2010.12345678,
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
            "position_id": 1,
            "side": "BUY",
            "entry_price": 2000.0,
            "stop_loss": 1990.0,
            # no live TP — fall back to order tp_price (must be rounded)
            "take_profit": None,
            "broker_symbol": "XAUUSD",
        }
    ]
    r = process_open_order_for_be(order, broker=FakeBroker(), positions=positions)
    assert r["action"] == "moved"
    assert amends[0]["sl"] == 2000.0
    assert amends[0]["tp"] == 2010.12  # rounded to 2 digits, not 2010.12345678


def _asia_protect_order():
    return {
        "id": "ord-asia",
        "kind": "asia_breakout",
        "symbol": "XAU/USD",
        "side": "BUY",
        "broker_position_id": "77",
        "entry_price": 4000.0,
        "sl_price": 3990.0,
        "tp_price": 4020.0,
        "created_at": "2026-10-06T05:00:00Z",
        "detail": {
            "breakeven": {
                "enabled": False,
                "applied": False,
                "fraction": 0.5,
                "point_size": 0.01,
            },
            "protect": {
                "enabled": True,
                "kind": "asia_breakout",
                "flatten_hour": 23,
                "flatten_tz": "Europe/Bucharest",
                "max_hold_hours": 14,
                "entry_at": "2026-10-06T05:00:00Z",
                "trail_arm_pts": 2000,
                "trail_pts": 200,
                "flattened": False,
            },
        },
    }


def _asia_position():
    return {
        "position_id": 77,
        "side": "BUY",
        "entry_price": 4000.0,
        "volume": 100,
        "stop_loss": 3990.0,
        "take_profit": 4020.0,
        "broker_symbol": "XAUUSD",
    }


def test_flatten_runs_when_breakeven_is_off_and_spot_fails(monkeypatch):
    """Asia forces breakeven off. Flatten at the alert hour must still close."""
    from factor.db import repos
    from factor.services import protect as protect_mod

    updated = {}
    monkeypatch.setattr(
        repos,
        "update_order",
        lambda oid, **fields: updated.update({"id": oid, **fields}) or {"id": oid},
    )
    monkeypatch.setattr(
        protect_mod,
        "flatten_due",
        lambda *a, **k: "flatten_hour:23:00 Europe/Bucharest",
    )
    closed = []

    class FakeBroker:
        _dry_run = False

        def get_spot(self, sym):
            raise RuntimeError("no quote")

        def close_position(self, *, position_id, volume):
            closed.append((position_id, volume))
            return {"ok": True}

    r = process_open_order_for_be(
        _asia_protect_order(),
        broker=FakeBroker(),
        positions=[_asia_position()],
    )
    assert r["action"] == "flattened"
    assert r["reason"] == "flatten_hour:23:00 Europe/Bucharest"
    assert closed == [(77, 100)]
    assert updated.get("status") == "closed"


def test_protect_without_flatten_does_not_close_when_spot_fails(monkeypatch):
    from factor.services import protect as protect_mod

    monkeypatch.setattr(protect_mod, "flatten_due", lambda *a, **k: None)
    closed = []

    class FakeBroker:
        _dry_run = False

        def get_spot(self, sym):
            raise RuntimeError("no quote")

        def close_position(self, *, position_id, volume):
            closed.append((position_id, volume))
            return {"ok": True}

    r = process_open_order_for_be(
        _asia_protect_order(),
        broker=FakeBroker(),
        positions=[_asia_position()],
    )
    assert r["action"] == "skip"
    assert r["reason"] == "spot_unavailable"
    assert closed == []
