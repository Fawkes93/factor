"""The spot stream stays open and the quote route reads it."""

import time

import pytest

from factor import config
from factor.broker.ctrader_client import parse_spot_event
from factor.services.ai_orders import AiOrderError, quote_ai_symbol
from factor.services.spot_feed import reset_live_quotes


class _Client:
    connects = 0

    def connect(self) -> None:
        _Client.connects += 1

    def close(self) -> None:
        pass

    def subscribe_spots(self, symbol: str) -> dict:
        assert symbol == "XAUUSD"
        return {"bid": 3900.0, "ask": 3900.4, "symbol_id": 7}

    def read_message(self, timeout_sec: float = 5.0):
        time.sleep(min(timeout_sec, 0.05))
        return None

    def send_heartbeat(self) -> None:
        pass


class _Broker:
    _dry_run = False
    _host = "demo.ctraderapi.com"
    _account_id = "9"

    def configured(self) -> bool:
        return True

    def _new_client(self) -> _Client:
        return _Client()


def setup_function() -> None:
    _Client.connects = 0
    reset_live_quotes()


def teardown_function() -> None:
    reset_live_quotes()


def test_spot_event_scales_the_protocol_price():
    spot = parse_spot_event({"payloadType": 2131, "payload": {"bid": 390010000, "ask": 390040000}})
    assert spot == {"bid": 3900.1, "ask": 3900.4}


def test_live_quote_reuses_one_stream(monkeypatch):
    monkeypatch.setattr(
        "factor.services.ai_orders.resolve_connection",
        lambda account_id=None: {"account_id": 9},
    )
    monkeypatch.setattr(
        "factor.services.ai_orders.broker_for_connection",
        lambda conn: _Broker(),
    )
    first = quote_ai_symbol(symbol="XAUUSD")
    second = quote_ai_symbol(symbol="xauusd")
    assert first["bid"] == 3900.0
    assert first["ask"] == 3900.4
    assert second["bid"] == 3900.0
    assert _Client.connects == 1
    assert second["age_sec"] >= 0


def test_quote_route_requires_admin_token(monkeypatch):
    from factor.api.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "secret")
    client = TestClient(create_app())
    denied = client.get("/api/ai/quote", params={"symbol": "XAUUSD"})
    assert denied.status_code == 401
    monkeypatch.setattr(
        "factor.api.ai_routes.quote_ai_symbol",
        lambda **_kwargs: {"success": True, "bid": 1.0, "ask": 1.1, "symbol": "XAUUSD"},
    )
    allowed = client.get(
        "/api/ai/quote",
        params={"symbol": "XAUUSD"},
        headers={"Authorization": "Bearer secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["ask"] == 1.1


def test_dry_run_has_no_quote(monkeypatch):
    monkeypatch.setattr(
        "factor.services.ai_orders.resolve_connection",
        lambda account_id=None: {"account_id": 9},
    )

    class Dry:
        _dry_run = True

        def configured(self) -> bool:
            return True

    monkeypatch.setattr("factor.services.ai_orders.broker_for_connection", lambda conn: Dry())
    with pytest.raises(AiOrderError) as caught:
        quote_ai_symbol(symbol="XAUUSD")
    assert caught.value.status_code == 503
