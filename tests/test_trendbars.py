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


def test_rate_limit_is_a_blocked_trendbar():
    assert trendbar_blocked(
        {
            "payloadType": 2142,
            "payload": {"errorCode": "BLOCKED_PAYLOAD_TYPE", "description": "You are being rate limited"},
        }
    )
    assert not trendbar_blocked({"payloadType": 2138, "payload": {}})
