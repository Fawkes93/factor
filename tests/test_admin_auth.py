from fastapi.testclient import TestClient

from factor import config
from factor.api.app import create_app


def test_admin_token_protects_trades(monkeypatch):
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "secret-admin")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "whsec")

    app = create_app()
    client = TestClient(app)

    assert client.get("/api/trades").status_code == 401
    ok = client.get(
        "/api/trades",
        headers={"Authorization": "Bearer secret-admin"},
    )
    assert ok.status_code == 200
    assert ok.json()["count"] == 0
