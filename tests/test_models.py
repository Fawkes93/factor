import pytest

from factor.inbound.models import parse_cloudevent


def test_parse_cloudevent_envelope():
    payload = {
        "specversion": "1.0",
        "id": "reaktor-alert-1842",
        "source": "reaktor",
        "type": "com.reaktor.alert.v1",
        "data": {
            "alert_id": 1842,
            "kind": "candle",
            "symbol": "XAU/USD",
            "side": "UP",
            "price": 4061.4085,
            "timeframe_min": 15,
        },
    }
    a = parse_cloudevent(payload)
    assert a.alert_id == 1842
    assert a.kind == "candle"
    assert a.symbol == "XAU/USD"
    assert a.side == "UP"
    assert a.is_directional


def test_parse_bare_data():
    a = parse_cloudevent(
        {
            "alert_id": 1,
            "kind": "spike",
            "symbol": "EUR/USD",
            "side": "DOWN",
            "price": 1.1,
        }
    )
    assert a.side == "DOWN"


def test_missing_alert_id():
    with pytest.raises(ValueError, match="alert_id"):
        parse_cloudevent({"kind": "candle", "symbol": "XAU/USD", "side": "UP", "price": 1})
