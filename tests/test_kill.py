"""Kill switch service unit tests (mocked repos + broker)."""

from __future__ import annotations

import pytest

from factor import config


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(config, "FACTOR_TOKEN_ENC_KEY", "kill-test-key")
    monkeypatch.setattr(config, "CTRADER_DRY_RUN", True)


def test_activate_kill_closes_and_flags(monkeypatch):
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos
    from factor.services import kill as kill_svc

    uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    enc = encrypt_token("tok", config.FACTOR_TOKEN_ENC_KEY)

    monkeypatch.setattr(
        repos,
        "get_user",
        lambda u: {
            "id": uid,
            "kill_switch": False,
            "trading_enabled": True,
        },
    )
    monkeypatch.setattr(
        repos,
        "set_account_kill_switch",
        lambda u, a, v: {
            "account_id": a,
            "kill_switch": v,
            "trading_enabled": not v,
            "host": "demo.ctraderapi.com",
            "access_token_enc": enc,
        },
    )
    monkeypatch.setattr(
        repos,
        "get_connection",
        lambda u, a=None: {
            "account_id": a or 1,
            "host": "demo.ctraderapi.com",
            "access_token_enc": enc,
        },
    )
    monkeypatch.setattr(repos, "mark_open_orders_closed", lambda *a, **k: 2)

    class FakeBroker:
        def close_all_positions(self):
            return [
                {"position_id": 10, "ok": True, "message": "closed"},
                {"position_id": 11, "ok": False, "message": "fail"},
            ]

    monkeypatch.setattr(
        kill_svc, "broker_from_connection", lambda **k: FakeBroker()
    )

    out = kill_svc.activate_kill_switch(uid)
    assert out["ok"] is True
    assert out["kill_switch"] is True
    assert out["close"]["ok_count"] == 1
    assert out["close"]["fail_count"] == 1
    assert out["orders_marked_closed"] == 2


def test_resume(monkeypatch):
    from factor.db import repos
    from factor.services import kill as kill_svc

    uid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    monkeypatch.setattr(
        repos,
        "get_user",
        lambda u: {"id": uid, "kill_switch": True, "trading_enabled": False},
    )
    monkeypatch.setattr(
        repos,
        "get_connection",
        lambda u, a=None: {
            "account_id": a or 1,
            "kill_switch": True,
            "trading_enabled": False,
        },
    )
    monkeypatch.setattr(
        repos,
        "set_account_kill_switch",
        lambda u, a, v: {
            "account_id": a,
            "kill_switch": v,
            "trading_enabled": not v,
        },
    )
    out = kill_svc.resume_trading(uid, 1)
    assert out["kill_switch"] is False
    assert out["trading_enabled"] is True


def test_engine_multi_respects_kill(monkeypatch):
    from factor import engine_multi
    from factor.crypto.tokens import encrypt_token
    from factor.db import repos
    from factor.inbound.models import AlertData
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    alert = AlertData(
        alert_id=1,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=1.0,
        timeframe_min=15,
        triggered_at=now,
    )
    enc = encrypt_token("t", config.FACTOR_TOKEN_ENC_KEY)
    monkeypatch.setattr(repos, "claim_alert", lambda *a, **k: True)
    monkeypatch.setattr(repos, "update_processed_reason", lambda *a, **k: None)
    monkeypatch.setattr(repos, "insert_order", lambda **k: {"id": "x"})
    monkeypatch.setattr(repos, "count_orders_today", lambda *a, **k: 0)

    r = engine_multi.process_alert_for_user(
        alert,
        user={
            "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "kill_switch": True,
            "trading_enabled": False,
        },
        pref={
            "volume_lots": 0.01,
            "sl_points": 1000,
            "tp_points": 1000,
            "max_concurrent": 1,
            "max_per_day": 5,
        },
        connection={
            "account_id": 1,
            "host": "demo.ctraderapi.com",
            "access_token_enc": enc,
        },
    )
    assert r["action"] == "skip"
    assert r["reason"] == "kill_switch"
