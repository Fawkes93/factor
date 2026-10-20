import json
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from factor import config
from factor.api.app import create_app
from factor.inbound.auth import sign_body


def test_webhook_happy_path(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "")

    app = create_app()
    client = TestClient(app)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body_obj = {
        "specversion": "1.0",
        "id": "reaktor-alert-42",
        "source": "reaktor",
        "type": "com.reaktor.alert.v1",
        "time": now,
        "data": {
            "alert_id": 42,
            "kind": "candle",
            "symbol": "XAU/USD",
            "side": "DOWN",
            "price": 4000.0,
            "timeframe_min": 15,
            "triggered_at": now,
        },
    }
    raw = json.dumps(body_obj, separators=(",", ":")).encode()
    sig = sign_body("whsec_test", raw)

    fanout = {
        "action": "fanout",
        "alert_id": 42,
        "users": 1,
        "traded": 1,
        "skipped": 0,
        "results": [{"user_id": "u1", "action": "traded"}],
    }

    with patch("factor.api.app.supabase_configured", return_value=True), patch(
        "factor.api.app.process_alert_multi", return_value=fanout
    ) as mock_fanout:
        resp = client.post(
            "/hooks/reaktor",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Reaktor-Signature": sig,
                "X-Reaktor-Event-Id": "42",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "fanout"
        assert data["traded"] == 1
        mock_fanout.assert_called_once()

    # bad signature
    bad = client.post(
        "/hooks/reaktor",
        content=raw,
        headers={"X-Reaktor-Signature": "sha256=nope"},
    )
    assert bad.status_code == 401

    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert body["mode"] == "multi_user"


def test_webhook_crt_metrics_reach_the_engine(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "")

    app = create_app()
    client = TestClient(app)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metrics = {
        "strategy": "crt",
        "order_type": "market",
        "entry_price": 4000.0,
        "sl_price": 3990.0,
        "tp_price": 4060.0,
        "tp1_price": 4010.0,
        "tp1_frac": 0.5,
        "tp1_rr": 1.0,
        "be_after_tp1": False,
        "max_hold_until": "2026-10-27T16:00:00Z",
        "max_hold_hours": 96,
        "weekly_flatten": "FRI 23:45 Europe/Chisinau",
        "htf_min": 120,
        "max_hold_htf": 48,
        "range_high": 4012.0,
        "range_low": 3988.0,
        "sweep_price": 3985.0,
        "fvg_top": 4004.0,
        "fvg_bottom": 3996.0,
    }
    body_obj = {
        "specversion": "1.0",
        "id": "reaktor-alert-77",
        "source": "reaktor",
        "type": "com.reaktor.alert.v1",
        "time": now,
        "data": {
            "alert_id": 77,
            "kind": "crt",
            "symbol": "XAU/USD",
            "side": "UP",
            "price": 4000.0,
            "timeframe_min": 120,
            "triggered_at": now,
            "metrics": metrics,
        },
    }
    raw = json.dumps(body_obj, separators=(",", ":")).encode()
    sig = sign_body("whsec_test", raw)
    captured = {}

    def _fanout(alert):
        captured["alert"] = alert
        return {
            "action": "fanout",
            "alert_id": alert.alert_id,
            "users": 0,
            "traded": 0,
            "skipped": 0,
            "results": [],
        }

    with patch("factor.api.app.supabase_configured", return_value=True), patch(
        "factor.api.app.process_alert_multi", side_effect=_fanout
    ):
        resp = client.post(
            "/hooks/reaktor",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Reaktor-Signature": sig,
            },
        )
    assert resp.status_code == 200
    alert = captured["alert"]
    assert alert.kind == "crt"
    assert alert.metrics["sl_price"] == 3990.0
    assert alert.metrics["tp_price"] == 4060.0
    assert alert.metrics["tp1_price"] == 4010.0
    assert alert.metrics["tp1_frac"] == 0.5
    assert alert.metrics["be_after_tp1"] is False
    assert alert.metrics["max_hold_hours"] == 96
    assert alert.metrics["max_hold_until"] == "2026-10-27T16:00:00Z"
    assert alert.metrics["fvg_bottom"] == 3996.0
    assert alert.metrics["sweep_price"] == 3985.0


def test_webhook_requires_supabase(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "whsec_test")
    app = create_app()
    client = TestClient(app)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body_obj = {
        "data": {
            "alert_id": 1,
            "kind": "candle",
            "symbol": "XAU/USD",
            "side": "UP",
            "price": 1.0,
            "timeframe_min": 15,
            "triggered_at": now,
        }
    }
    raw = json.dumps(body_obj).encode()
    sig = sign_body("whsec_test", raw)
    with patch("factor.api.app.supabase_configured", return_value=False):
        resp = client.post(
            "/hooks/reaktor",
            content=raw,
            headers={"X-Reaktor-Signature": sig},
        )
    assert resp.status_code == 503
