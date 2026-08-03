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
    monkeypatch.setattr(config, "MULTI_USER", True)
    monkeypatch.setattr(config, "MAX_ALERT_AGE_SEC", 5.0)
    monkeypatch.setattr(config, "CTRADER_DRY_RUN", True)
    monkeypatch.setattr(config, "FACTOR_TOKEN_ENC_KEY", "test-enc-key-for-multi")
    monkeypatch.setattr(config, "MARKET_OPEN_BLACKOUT", False)


def test_fanout_trades_and_skips_open(monkeypatch):
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

    def claim(uid, aid, reason="claimed"):
        key = (uid, aid)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    monkeypatch.setattr(repos, "claim_alert", claim)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "ord-1"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)
    monkeypatch.setattr(
        repos,
        "list_candidates_for_alert",
        lambda **k: [{"user": user, "pref": pref, "connection": conn}],
    )

    open_counts = {"n": 0}

    class FakeBroker:
        def count_open_positions(self, broker_symbol=None):
            return open_counts["n"]

        def has_open_position(self, broker_symbol=None):
            return open_counts["n"] > 0

        def place_market_with_sl_tp(self, plan):
            open_counts["n"] += 1
            return OrderResult(
                ok=True,
                status="open",
                broker_order_id="b1",
                message="ok",
            )

    monkeypatch.setattr(
        engine_multi, "broker_from_connection", lambda **k: FakeBroker()
    )

    r1 = engine_multi.process_alert_multi(_alert(alert_id=1))
    assert r1["action"] == "fanout"
    assert r1["traded"] == 1

    # Same user still has open position
    r2 = engine_multi.process_alert_multi(_alert(alert_id=2))
    assert r2["traded"] == 0
    assert r2["skipped"] == 1
    assert r2["results"][0]["reason"].startswith("broker_has_open_position")


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

    def claim(uid, aid, reason="claimed"):
        calls["claim"] += 1
        return calls["claim"] == 1  # only first succeeds

    monkeypatch.setattr(repos, "claim_alert", claim)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "o"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)

    class FakeBroker:
        def count_open_positions(self, broker_symbol=None):
            return 0

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

    r = engine_multi.process_alert_for_user(
        _alert(alert_id=5, triggered_at=_iso_ago(60)),
        user=user,
        pref=pref,
        connection=conn,
    )
    assert r["action"] == "skip"
    assert "alert_too_old" in r["reason"]
