import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from factor import config
from factor.api.app import create_app
from factor.inbound.auth import sign_body
from factor.store.state import StateStore


def test_webhook_happy_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "TRADE_KINDS", ["candle"])
    monkeypatch.setattr(config, "TRADE_SYMBOLS", ["XAU/USD"])
    monkeypatch.setattr(config, "COOLDOWN_SEC", 0)
    monkeypatch.setattr(config, "MAX_ALERT_AGE_SEC", 5.0)
    monkeypatch.setattr(config, "CATCHUP_ON_START", False)
    monkeypatch.setattr(config, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(config, "BROKER", "ctrader")
    monkeypatch.setattr(config, "CTRADER_DRY_RUN", True)
    monkeypatch.setattr(config, "CTRADER_CLIENT_ID", "test-id")
    monkeypatch.setattr(config, "CTRADER_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(config, "CTRADER_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(config, "CTRADER_ACCOUNT_ID", "12345")
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "")

    store = StateStore(tmp_path / "hook.db")
    app = create_app(store=store, skip_catchup=True)
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
    assert data["action"] == "traded"
    assert data["plan"]["side"] == "SELL"

    # bad signature
    bad = client.post(
        "/hooks/reaktor",
        content=raw,
        headers={"X-Reaktor-Signature": "sha256=nope"},
    )
    assert bad.status_code == 401

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
