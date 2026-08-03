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
