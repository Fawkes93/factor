from fastapi.testclient import TestClient

from factor.api.app import create_app
from factor.api import ai_routes
from factor.db.repos import validate_ai_limits
import pytest


def test_ai_limits_reject_a_max_below_the_min():
    with pytest.raises(ValueError, match="max lots"):
        validate_ai_limits({"min_volume_lots": 0.1, "max_volume_lots": 0.05})


def test_ai_limits_allow_an_open_max_rr():
    validate_ai_limits(
        {
            "min_volume_lots": 0.01,
            "max_volume_lots": 0.02,
            "min_rr": 1.5,
            "max_rr": None,
            "daily_loss_cap_usd": 50,
            "max_concurrent": 1,
        }
    )


def test_subscribers_route_returns_enabled_accounts(monkeypatch):
    monkeypatch.setattr(ai_routes.config, "FACTOR_ADMIN_TOKEN", "secret")
    monkeypatch.setattr(
        ai_routes.repos,
        "list_ai_subscribers",
        lambda: [
            {
                "user_id": "user-1",
                "account_id": 10,
                "telegram_chat_id": 99,
                "max_concurrent": 1,
                "min_volume_lots": 0.01,
                "max_volume_lots": 0.02,
                "min_rr": 1.5,
                "max_rr": None,
                "daily_loss_cap_usd": 40,
            }
        ],
    )
    client = TestClient(create_app())
    denied = client.get("/api/ai/subscribers")
    assert denied.status_code == 401
    allowed = client.get("/api/ai/subscribers", headers={"Authorization": "Bearer secret"})
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["count"] == 1
    assert body["subscribers"][0]["account_id"] == 10
    assert body["subscribers"][0]["max_volume_lots"] == 0.02
