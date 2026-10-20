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


def test_parse_crt_keeps_metrics():
    payload = {
        "specversion": "1.0",
        "id": "reaktor-alert-77",
        "time": "2026-10-19T07:00:00Z",
        "data": {
            "alert_id": 77,
            "kind": "crt",
            "symbol": "XAU/USD",
            "side": "UP",
            "price": 4000.0,
            "timeframe_min": 120,
            "triggered_at": "2026-10-19T07:00:00Z",
            "metrics": {
                "strategy": "crt",
                "order_type": "market",
                "entry_price": 4000.0,
                "sl_price": 3990.0,
                "tp_price": 4060.0,
                "tp1_price": 4010.0,
                "tp1_frac": 0.5,
                "tp1_rr": 1.0,
                "be_after_tp1": False,
                "max_hold_until": "2026-10-23T16:00:00Z",
                "max_hold_hours": 96,
                "weekly_flatten": "FRI 23:45 Europe/Chisinau",
                "htf_min": 120,
                "max_hold_htf": 48,
                "range_high": 4012.0,
                "range_low": 3988.0,
                "sweep_price": 3985.0,
                "fvg_top": 4004.0,
                "fvg_bottom": 3996.0,
            },
        },
    }
    alert = parse_cloudevent(payload)
    assert alert.kind == "crt"
    assert alert.metrics["strategy"] == "crt"
    assert alert.metrics["order_type"] == "market"
    assert alert.metrics["sl_price"] == 3990.0
    assert alert.metrics["tp_price"] == 4060.0
    assert alert.metrics["tp1_price"] == 4010.0
    assert alert.metrics["tp1_frac"] == 0.5
    assert alert.metrics["be_after_tp1"] is False
    assert alert.metrics["max_hold_until"] == "2026-10-23T16:00:00Z"
    assert alert.metrics["max_hold_hours"] == 96
    assert alert.metrics["htf_min"] == 120
    assert alert.metrics["max_hold_htf"] == 48
    assert alert.metrics["range_high"] == 4012.0
    assert alert.metrics["sweep_price"] == 3985.0
    assert alert.metrics["fvg_top"] == 4004.0
    assert alert.metrics["fvg_bottom"] == 3996.0
    assert alert.triggered_at == "2026-10-19T07:00:00Z"


def test_missing_alert_id():
    with pytest.raises(ValueError, match="alert_id"):
        parse_cloudevent({"kind": "candle", "symbol": "XAU/USD", "side": "UP", "price": 1})
