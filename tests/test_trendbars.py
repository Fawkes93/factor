"""cTrader 15-minute bars decode to a price and a tick count."""

from datetime import datetime, timezone

from factor.broker.ctrader_client import decode_trendbar, trendbar_blocked, trendbar_windows


def test_trendbar_prices_are_low_plus_delta():
    bar = decode_trendbar(
        {
            "low": "388650000",
            "deltaOpen": "12000",
            "deltaHigh": "50000",
            "deltaClose": "20000",
            "volume": "42",
            "utcTimestampInMinutes": "29800000",
        }
    )
    assert bar is not None
    assert bar["low"] == 3886.5
    assert bar["open"] == 3886.62
    assert bar["high"] == 3887.0
    assert bar["close"] == 3886.7
    assert bar["tick_count"] == 42
    expected = datetime.fromtimestamp(29800000 * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert bar["open_time"] == expected


def test_trendbar_without_a_time_is_skipped():
    assert decode_trendbar({"low": 1, "volume": 5}) is None


def test_trendbar_windows_start_with_the_newest_slice():
    day = 24 * 60 * 60 * 1000
    assert trendbar_windows(0, 50 * day, 21 * day) == [
        (29 * day, 50 * day),
        (8 * day, 29 * day),
        (0, 8 * day),
    ]


def test_trendbars_until_bounds_the_request(monkeypatch):
    from factor.services.ai_orders import list_ai_trendbars

    seen: dict = {}

    class Broker:
        def trendbars(self, broker_symbol, from_ms, to_ms):
            seen["symbol"] = broker_symbol
            seen["from"] = from_ms
            seen["to"] = to_ms
            return []

    monkeypatch.setattr(
        "factor.services.ai_orders.resolve_connection",
        lambda account_id=None: {"account_id": 1},
    )
    monkeypatch.setattr("factor.services.ai_orders.broker_for_connection", lambda conn: Broker())
    body = list_ai_trendbars(
        symbol="XAUUSD",
        since="2026-11-01T00:00:00Z",
        until="2026-11-08T00:00:00Z",
    )
    assert body["success"] is True
    assert seen["symbol"] == "XAUUSD"
    assert seen["to"] - seen["from"] == 7 * 24 * 60 * 60 * 1000


def test_rate_limit_is_a_blocked_trendbar():
    assert trendbar_blocked(
        {
            "payloadType": 2142,
            "payload": {"errorCode": "BLOCKED_PAYLOAD_TYPE", "description": "You are being rate limited"},
        }
    )
    assert not trendbar_blocked({"payloadType": 2138, "payload": {}})
