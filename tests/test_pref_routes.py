"""Partial alert routes and order P/L on /api/me/orders."""

from fastapi.testclient import TestClient

from factor.api.app import create_app
from factor.auth import routes as auth_routes


def _client(monkeypatch):
    monkeypatch.setattr(auth_routes, "_require_user_id", lambda _request: "user-1")
    monkeypatch.setattr(
        auth_routes,
        "_selected_connection",
        lambda _uid: {
            "user_id": "user-1",
            "account_id": 10,
            "deposit_currency": "USD",
        },
    )
    return TestClient(create_app())


def test_patch_pref_sends_only_given_fields(monkeypatch):
    seen = {}

    def update_pref(uid, account_id, kind, symbol, fields):
        seen["args"] = (uid, account_id, kind, symbol, dict(fields))
        return {
            "user_id": uid,
            "account_id": account_id,
            "kind": kind,
            "symbol": symbol,
            "volume_lots": fields["volume_lots"],
            "enabled": True,
        }

    monkeypatch.setattr(auth_routes.repos, "update_pref", update_pref)
    client = _client(monkeypatch)
    res = client.patch(
        "/api/me/prefs/candle/XAU/USD",
        json={"volume_lots": 0.02},
    )
    assert res.status_code == 200
    assert seen["args"] == ("user-1", 10, "candle", "XAU/USD", {"volume_lots": 0.02})
    assert res.json()["pref"]["volume_lots"] == 0.02


def test_patch_pref_404_when_missing(monkeypatch):
    def update_pref(*_a, **_k):
        raise LookupError("alert not found")

    monkeypatch.setattr(auth_routes.repos, "update_pref", update_pref)
    client = _client(monkeypatch)
    res = client.patch("/api/me/prefs/spike/EUR/USD", json={"enabled": False})
    assert res.status_code == 404


def test_delete_pref_404(monkeypatch):
    monkeypatch.setattr(auth_routes.repos, "delete_pref", lambda *a, **k: False)
    client = _client(monkeypatch)
    res = client.delete("/api/me/prefs/candle/BTC/USD")
    assert res.status_code == 404


def test_orders_expose_closed_and_live_pnl(monkeypatch):
    orders = [
        {
            "id": "open-1",
            "status": "open",
            "broker_position_id": "55",
            "detail": {},
        },
        {
            "id": "closed-1",
            "status": "closed",
            "detail": {
                "outcome": {
                    "net_pnl": 14.4,
                    "gross_profit": 15.0,
                    "swap": -0.1,
                    "commission": -0.5,
                }
            },
        },
        {"id": "closed-blank", "status": "closed", "detail": {}},
    ]
    monkeypatch.setattr(
        auth_routes.repos, "list_orders", lambda *a, **k: orders
    )
    monkeypatch.setattr(
        auth_routes.live_pnl,
        "unrealized_for_connection",
        lambda _conn: {"55": {"net": -0.25, "gross": -0.2}},
    )
    client = _client(monkeypatch)
    res = client.get("/api/me/orders")
    assert res.status_code == 200
    body = res.json()
    assert body["deposit_currency"] == "USD"
    by_id = {row["id"]: row for row in body["orders"]}
    assert by_id["open-1"]["pnl"] == -0.25
    assert by_id["open-1"]["pnl_live"] is True
    assert by_id["closed-1"]["pnl"] == 14.4
    assert by_id["closed-1"]["pnl_gross"] == 15.0
    assert by_id["closed-1"]["pnl_live"] is False
    assert by_id["closed-blank"]["pnl"] is None
