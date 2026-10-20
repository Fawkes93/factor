"""Multi-user fan-out unit tests (mocked repos + broker)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from factor import config
from factor.broker.base import OrderResult
from factor.inbound.models import AlertData


def _iso_ago(seconds: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.isoformat().replace("+00:00", "Z")


def _alert(**kwargs) -> AlertData:
    base = dict(
        alert_id=9001,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=4000.0,
        timeframe_min=15,
        triggered_at=_iso_ago(0.5),
    )
    base.update(kwargs)
    return AlertData(**base)


@pytest.fixture(autouse=True)
def _mu_flags(monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "MAX_ALERT_AGE_SEC", 5.0)
    monkeypatch.setattr(config, "MAX_ALERT_AGE_ASIA_SEC", 20.0)
    monkeypatch.setattr(config, "CTRADER_DRY_RUN", True)
    monkeypatch.setattr(config, "FACTOR_TOKEN_ENC_KEY", "test-enc-key-for-multi")
    monkeypatch.setattr(config, "MARKET_OPEN_BLACKOUT", False)
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN", "FRI 23:45 Europe/Chisinau")
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", True)
    monkeypatch.setattr(config, "CRT_MAX_ENTRY_SLIP_PTS", 0.0)


def test_fanout_trades_and_skips_open_same_pref(monkeypatch):
    """max_concurrent is per pref (kind+symbol): second candle blocked while open."""
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("access-tok", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "11111111-1111-1111-1111-111111111111",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
    }
    conn = {
        "account_id": 12345,
        "host": "demo.ctraderapi.com",
        "access_token_enc": enc,
    }

    claimed: set = set()
    # Simulate Factor open-order book: (kind, symbol) -> open count
    open_by_pref: Dict[tuple, int] = {}

    def claim(uid, aid, reason="claimed", account_id=None):
        key = (uid, aid, account_id)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    def count_open(uid, kind=None, symbol=None, account_id=None):
        return int(open_by_pref.get((account_id, kind, symbol), 0))

    def insert_order(**k):
        if k.get("status") == "open":
            key = (k.get("account_id"), k.get("kind"), k.get("symbol"))
            open_by_pref[key] = open_by_pref.get(key, 0) + 1
        return {"id": "ord-1"}

    monkeypatch.setattr(repos, "claim_alert", claim)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", insert_order)
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", count_open)
    monkeypatch.setattr(
        repos,
        "list_candidates_for_alert",
        lambda **k: [{"user": user, "pref": pref, "connection": conn}],
    )

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            return OrderResult(
                ok=True,
                status="open",
                broker_order_id="b1",
                message="ok",
            )

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )

    r1 = engine_multi.process_alert_multi(_alert(alert_id=1, kind="candle"))
    assert r1["action"] == "fanout"
    assert r1["traded"] == 1

    # Same pref still has an open order → skip
    r2 = engine_multi.process_alert_multi(_alert(alert_id=2, kind="candle"))
    assert r2["traded"] == 0
    assert r2["skipped"] == 1
    assert r2["results"][0]["reason"].startswith("max_concurrent:")


def test_max_concurrent_is_per_kind(monkeypatch):
    """Open candle does not block spike when each pref has max_concurrent=1."""
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("access-tok", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "44444444-4444-4444-4444-444444444444",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
    }
    conn = {
        "account_id": 55,
        "host": "demo.ctraderapi.com",
        "access_token_enc": enc,
    }

    claimed: set = set()
    open_by_pref: Dict[tuple, int] = {}

    def claim(uid, aid, reason="claimed", account_id=None):
        key = (uid, aid, account_id)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    def count_open(uid, kind=None, symbol=None, account_id=None):
        return int(open_by_pref.get((account_id, kind, symbol), 0))

    def insert_order(**k):
        if k.get("status") == "open":
            key = (k.get("account_id"), k.get("kind"), k.get("symbol"))
            open_by_pref[key] = open_by_pref.get(key, 0) + 1
        return {"id": "ord-x"}

    monkeypatch.setattr(repos, "claim_alert", claim)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", insert_order)
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", count_open)

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            return OrderResult(ok=True, status="open", broker_order_id="b", message="ok")

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )

    r_candle = engine_multi.process_alert_for_user(
        _alert(alert_id=10, kind="candle"),
        user=user,
        pref=pref,
        connection=conn,
    )
    assert r_candle["action"] == "traded"

    r_spike = engine_multi.process_alert_for_user(
        _alert(alert_id=11, kind="spike"),
        user=user,
        pref=pref,
        connection=conn,
    )
    assert r_spike["action"] == "traded"
    assert open_by_pref[(55, "candle", "XAU/USD")] == 1
    assert open_by_pref[(55, "spike", "XAU/USD")] == 1


def test_claim_prevents_double(monkeypatch):
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("access-tok", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "22222222-2222-2222-2222-222222222222",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 5,
        "max_per_day": 10,
    }
    conn = {
        "account_id": 99,
        "host": "demo.ctraderapi.com",
        "access_token_enc": enc,
    }

    calls = {"claim": 0, "place": 0}

    def claim(uid, aid, reason="claimed", account_id=None):
        calls["claim"] += 1
        return calls["claim"] == 1  # only first succeeds

    monkeypatch.setattr(repos, "claim_alert", claim)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "o"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            calls["place"] += 1
            return OrderResult(ok=True, status="open", broker_order_id="x")

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )

    a = _alert(alert_id=77)
    r1 = engine_multi.process_alert_for_user(
        a, user=user, pref=pref, connection=conn
    )
    r2 = engine_multi.process_alert_for_user(
        a, user=user, pref=pref, connection=conn
    )
    assert r1["action"] == "traded"
    assert r2["action"] == "skip"
    assert r2["reason"] == "duplicate_alert_id"
    assert calls["place"] == 1


def test_two_accounts_both_trade(monkeypatch):
    """Same user, two accounts with the same pref both receive the alert."""
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("access-tok", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "55555555-5555-5555-5555-555555555555",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
    }
    conn_a = {
        "account_id": 111,
        "host": "demo.ctraderapi.com",
        "access_token_enc": enc,
    }
    conn_b = {
        "account_id": 222,
        "host": "live.ctraderapi.com",
        "access_token_enc": enc,
        "is_live": True,
    }

    claimed: set = set()

    def claim(uid, aid, reason="claimed", account_id=None):
        key = (uid, aid, account_id)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    monkeypatch.setattr(repos, "claim_alert", claim)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": f"ord-{k.get('account_id')}"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)
    monkeypatch.setattr(
        repos,
        "list_candidates_for_alert",
        lambda **k: [
            {"user": user, "pref": pref, "connection": conn_a},
            {"user": user, "pref": pref, "connection": conn_b},
        ],
    )

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            return OrderResult(ok=True, status="open", broker_order_id="b", message="ok")

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )

    r = engine_multi.process_alert_multi(_alert(alert_id=88, kind="candle"))
    assert r["traded"] == 2
    assert r["users"] == 2
    ids = {row["account_id"] for row in r["results"]}
    assert ids == {111, 222}


def test_account_kill_not_overridden_by_user_flag(monkeypatch):
    """Resumed account must trade even if legacy user.kill_switch is still true."""
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("t", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "66666666-6666-6666-6666-666666666666",
        "kill_switch": True,
        "trading_enabled": False,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
    }
    conn = {
        "account_id": 9,
        "host": "demo.ctraderapi.com",
        "access_token_enc": enc,
        "kill_switch": False,
        "trading_enabled": True,
    }
    monkeypatch.setattr(repos, "claim_alert", lambda *a, **k: True)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "ok"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            return OrderResult(ok=True, status="open", broker_order_id="x")

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )
    r = engine_multi.process_alert_for_user(
        _alert(alert_id=9), user=user, pref=pref, connection=conn
    )
    assert r["action"] == "traded"


def test_stale_skipped(monkeypatch):
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("t", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "33333333-3333-3333-3333-333333333333",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
    }
    conn = {"account_id": 1, "host": "demo.ctraderapi.com", "access_token_enc": enc}

    monkeypatch.setattr(repos, "claim_alert", lambda *a, **k: True)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "s"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)

    r = engine_multi.process_alert_for_user(
        _alert(alert_id=5, triggered_at=_iso_ago(60)),
        user=user,
        pref=pref,
        connection=conn,
    )
    assert r["action"] == "skip"
    assert "alert_too_old" in r["reason"]


def _age_decision(monkeypatch, *, kind: str, age_s: float, alert_id: int):
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("t", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "44444444-4444-4444-4444-444444444444",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
    }
    conn = {"account_id": 1, "host": "demo.ctraderapi.com", "access_token_enc": enc}
    monkeypatch.setattr(repos, "claim_alert", lambda *a, **k: True)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "age"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            return OrderResult(ok=True, status="open", broker_order_id="age")

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )
    return engine_multi.process_alert_for_user(
        _alert(alert_id=alert_id, kind=kind, triggered_at=_iso_ago(age_s)),
        user=user,
        pref=pref,
        connection=conn,
    )


def test_asia_breakout_accepts_age_inside_20s(monkeypatch):
    r = _age_decision(monkeypatch, kind="asia_breakout", age_s=15, alert_id=51)
    assert r["action"] == "traded"


def test_asia_breakout_skips_after_20s(monkeypatch):
    r = _age_decision(monkeypatch, kind="asia_breakout", age_s=25, alert_id=52)
    assert r["action"] == "skip"
    assert "alert_too_old" in r["reason"]
    assert ">20s" in r["reason"]


def test_session_kinds_accept_age_inside_20s(monkeypatch):
    for kind, alert_id in (("asia_breakout_h", 61), ("dawn_cross", 62)):
        r = _age_decision(monkeypatch, kind=kind, age_s=15, alert_id=alert_id)
        assert r["action"] == "traded", kind


def test_session_kinds_skip_after_20s(monkeypatch):
    for kind, alert_id in (("asia_breakout_h", 63), ("dawn_cross", 64)):
        r = _age_decision(monkeypatch, kind=kind, age_s=25, alert_id=alert_id)
        assert r["action"] == "skip", kind
        assert "alert_too_old" in r["reason"]
        assert ">20s" in r["reason"]


def test_session_alerts_store_flatten_and_disable_breakeven(monkeypatch):
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("t", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "55555555-5555-5555-5555-555555555555",
        "kill_switch": False,
        "trading_enabled": True,
    }
    conn = {"account_id": 1, "host": "demo.ctraderapi.com", "access_token_enc": enc}
    inserted: Dict[str, Any] = {}

    def _insert(**kwargs):
        inserted.clear()
        inserted.update(kwargs)
        return {"id": "sess"}

    monkeypatch.setattr(repos, "claim_alert", lambda *a, **k: True)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", _insert)
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)

    class FakeBroker:
        def place_market_with_sl_tp(self, plan):
            return OrderResult(ok=True, status="open", broker_order_id="sess")

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )

    cases = (
        ("asia_breakout_h", 71, 23, "Europe/Bucharest", 3990.0),
        ("dawn_cross", 72, 12, "Europe/Chisinau", 3980.0),
    )
    for kind, alert_id, hour, tz, expect_sl in cases:
        pref = {
            "volume_lots": 0.01,
            "sl_points": 1000,
            "tp_points": 1000,
            "max_concurrent": 1,
            "max_per_day": 5,
            "breakeven_half_tp": True,
            "sl_tp_override": True,
        }
        r = engine_multi.process_alert_for_user(
            _alert(
                alert_id=alert_id,
                kind=kind,
                price=4000.0,
                metrics={
                    "sl_price": 3980.0,
                    "tp_price": 4050.0,
                    "flatten_hour": hour,
                    "flatten_tz": tz,
                    "trail_arm_pts": 2000 if kind != "dawn_cross" else 0,
                    "trail_pts": 190 if kind != "dawn_cross" else 0,
                },
                triggered_at=_iso_ago(2),
            ),
            user=user,
            pref=pref,
            connection=conn,
        )
        assert r["action"] == "traded", kind
        assert r["plan"]["sl_price"] == expect_sl, kind
        detail = inserted["detail"]
        assert detail["breakeven"]["enabled"] is False
        prot = detail["protect"]
        assert prot["enabled"] is True
        assert prot["kind"] == kind
        assert prot["flatten_hour"] == hour
        assert prot["flatten_tz"] == tz
        if kind == "dawn_cross":
            assert prot["max_hold_hours"] == 0.0
            assert prot["trail_pts"] == 0.0
        else:
            assert prot["trail_pts"] == 190.0


def test_spike_stays_on_5s_age_limit(monkeypatch):
    r = _age_decision(monkeypatch, kind="spike", age_s=8, alert_id=53)
    assert r["action"] == "skip"
    assert "alert_too_old" in r["reason"]
    assert ">5s" in r["reason"]


def _crt_metrics(**over):
    base = {
        "strategy": "crt",
        "order_type": "market",
        "entry_price": 4000.0,
        "sl_price": 3985.0,
        "tp_price": 4075.0,
        "tp1_price": 4015.0,
        "tp1_frac": 0.5,
        "tp1_rr": 1.0,
        "be_after_tp1": False,
        "max_hold_until": "2026-10-27T16:00:00Z",
        "max_hold_hours": 96,
        "weekly_flatten": "MON 00:01 UTC",
        "htf_min": 120,
        "max_hold_htf": 48,
        "range_high": 4012.0,
        "range_low": 3988.0,
        "sweep_price": 3985.0,
        "fvg_top": 4004.0,
        "fvg_bottom": 3996.0,
    }
    base.update(over)
    return base


def _run_crt(monkeypatch, *, metrics, bid, ask, alert_id=80, side="UP", price=4000.0, symbol="XAU/USD", position_id=55):
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos

    enc = encrypt_token("t", config.FACTOR_TOKEN_ENC_KEY)
    user = {
        "id": "66666666-6666-6666-6666-666666666666",
        "kill_switch": False,
        "trading_enabled": True,
    }
    pref = {
        "volume_lots": 0.02,
        "sl_points": 1000,
        "tp_points": 1000,
        "max_concurrent": 1,
        "max_per_day": 5,
        "breakeven_half_tp": True,
        "sl_tp_override": True,
    }
    conn = {"account_id": 1, "host": "demo.ctraderapi.com", "access_token_enc": enc}
    inserted: Dict[str, Any] = {}

    def _insert(**kwargs):
        inserted.clear()
        inserted.update(kwargs)
        return {"id": "crt"}

    monkeypatch.setattr(repos, "claim_alert", lambda *a, **k: True)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", _insert)
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(repos, "count_open_orders_db", lambda *a, **k: 0)

    broker = MagicMock()
    broker._dry_run = False
    broker.get_spot.return_value = {"bid": bid, "ask": ask}
    broker.place_market_with_sl_tp.return_value = OrderResult(
        ok=True,
        status="open",
        broker_order_id="crt1",
        detail={"position_id": position_id} if position_id is not None else {},
    )
    broker.amend_position_sl_tp.return_value = {"ok": True}
    monkeypatch.setattr(engine_multi, "broker_from_connection", lambda **k: broker)
    result = engine_multi.process_alert_for_user(
        _alert(
            alert_id=alert_id,
            kind="crt",
            symbol=symbol,
            side=side,
            price=price,
            metrics=metrics,
            triggered_at=_iso_ago(1),
        ),
        user=user,
        pref=pref,
        connection=conn,
    )
    return result, inserted, broker


def test_crt_stores_protect_and_disables_breakeven(monkeypatch):
    result, inserted, broker = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(),
        bid=4001.0,
        ask=4001.2,
    )
    assert result["action"] == "traded"
    # Preset 1000 pts on 4000 would be 3990 / 4010. Webhook absolutes win.
    assert result["plan"]["sl_price"] == 3985.0
    assert result["plan"]["tp_price"] == 4075.0
    detail = inserted["detail"]
    assert detail["breakeven"]["enabled"] is False
    prot = detail["protect"]
    assert prot["enabled"] is True
    assert prot["kind"] == "crt"
    assert prot["tp1_price"] == 4015.0
    assert prot["tp1_frac"] == 0.5
    assert prot["be_after_tp1"] is False
    assert prot["partial_done"] is False
    assert prot["max_hold_until"] == "2026-10-27T16:00:00Z"
    assert prot["max_hold_hours"] == 96.0
    assert prot["flatten_weekday"] == 4
    assert prot["flatten_time"] == "23:45"
    assert prot["flatten_tz"] == "Europe/Chisinau"
    # Webhook weekly string is ignored.
    assert "MON" not in prot["flatten_time"]
    assert detail["crt_sltp_pinned"] is True
    broker.amend_position_sl_tp.assert_called_once_with(
        position_id=55, stop_loss=3985.0, take_profit=4075.0
    )


def test_crt_eur_plan_uses_metric_prices(monkeypatch):
    result, inserted, _broker = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(
            entry_price=1.10000,
            sl_price=1.09800,
            tp_price=1.10600,
            tp1_price=1.10100,
            max_hold_hours=48,
            max_hold_until="2026-10-25T16:00:00Z",
        ),
        bid=1.10020,
        ask=1.10030,
        alert_id=82,
        symbol="EUR/USD",
        price=1.10000,
    )
    assert result["action"] == "traded"
    assert result["plan"]["sl_price"] == 1.09800
    assert result["plan"]["tp_price"] == 1.10600
    assert inserted["detail"]["protect"]["max_hold_hours"] == 48.0
    assert inserted["detail"]["protect"]["tp1_price"] == 1.10100


def test_crt_skips_when_mark_is_beyond_sl_or_tp1(monkeypatch):
    beyond_sl, _, broker_sl = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(),
        bid=3980.0,
        ask=3980.2,
        alert_id=83,
    )
    assert beyond_sl["action"] == "skip"
    assert beyond_sl["reason"] == "crt_stale_fill:beyond_sl"
    broker_sl.place_market_with_sl_tp.assert_not_called()

    beyond_tp, _, broker_tp = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(),
        bid=4015.0,
        ask=4015.2,
        alert_id=84,
    )
    assert beyond_tp["action"] == "skip"
    assert beyond_tp["reason"] == "crt_stale_fill:beyond_tp1"
    broker_tp.place_market_with_sl_tp.assert_not_called()

    sell_sl, _, broker_sell = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(sl_price=4010.0, tp_price=3940.0, tp1_price=3990.0),
        bid=4011.0,
        ask=4012.0,
        alert_id=85,
        side="DOWN",
    )
    assert sell_sl["reason"] == "crt_stale_fill:beyond_sl"
    broker_sell.place_market_with_sl_tp.assert_not_called()

    sell_tp, _, broker_sell_tp = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(sl_price=4010.0, tp_price=3940.0, tp1_price=3990.0),
        bid=3988.0,
        ask=3990.0,
        alert_id=86,
        side="DOWN",
    )
    assert sell_tp["reason"] == "crt_stale_fill:beyond_tp1"
    broker_sell_tp.place_market_with_sl_tp.assert_not_called()


def test_crt_slip_cap_is_off_by_default_and_rejects_when_set(monkeypatch):
    # 4005 is between SL 3985 and TP1 4015. 500 points of slip, cap off.
    ok, _, broker = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(),
        bid=4005.0,
        ask=4005.2,
        alert_id=87,
    )
    assert ok["action"] == "traded"
    assert ok["plan"]["sl_price"] == 3985.0
    assert ok["plan"]["tp_price"] == 4075.0
    broker.place_market_with_sl_tp.assert_called_once()

    monkeypatch.setattr(config, "CRT_MAX_ENTRY_SLIP_PTS", 100.0)
    blocked, _, blocked_broker = _run_crt(
        monkeypatch,
        metrics=_crt_metrics(),
        bid=4005.0,
        ask=4005.2,
        alert_id=88,
    )
    assert blocked["action"] == "skip"
    assert blocked["reason"] == "crt_stale_fill:slip"
    blocked_broker.place_market_with_sl_tp.assert_not_called()


def test_crt_without_metric_prices_does_not_use_preset(monkeypatch):
    result, inserted, broker = _run_crt(
        monkeypatch,
        metrics={"strategy": "crt", "tp1_price": 4010.0},
        bid=4001.0,
        ask=4001.2,
        alert_id=89,
    )
    assert result["action"] == "skip"
    assert result["reason"] == "crt_missing_sl_tp"
    broker.place_market_with_sl_tp.assert_not_called()
    assert "protect" not in (inserted.get("detail") or {})
