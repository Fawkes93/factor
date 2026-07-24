from pathlib import Path

from fastapi.testclient import TestClient

from factor import config
from factor.api.app import create_app
from factor.store.state import StateStore


def test_admin_token_protects_trades(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "secret-admin")
    monkeypatch.setattr(config, "CATCHUP_ON_START", False)
    monkeypatch.setattr(config, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "whsec")

    app = create_app(store=StateStore(tmp_path / "a.db"), skip_catchup=True)
    client = TestClient(app)

    assert client.get("/api/trades").status_code == 401
    ok = client.get(
        "/api/trades",
        headers={"Authorization": "Bearer secret-admin"},
    )
    assert ok.status_code == 200
    assert ok.json()["count"] == 0
