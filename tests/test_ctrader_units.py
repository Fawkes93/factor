import pytest

from factor.broker import ctrader_client as cc
from factor.broker.ctrader import lots_to_protocol_volume, price_distance_to_relative
from factor.broker.ctrader_client import CTraderClient


def test_lots_to_volume_xau():
    # 0.01 lot × 100 oz = 1 oz → protocol 100
    assert lots_to_protocol_volume(0.01, 100) == 100


def test_lots_to_volume_eur():
    # 0.01 lot × 100_000 = 1000 units → protocol 100_000
    assert lots_to_protocol_volume(0.01, 100_000) == 100_000


def test_relative_from_price_distance():
    # 300 pts × 0.01 = $3.00 → relative 300_000
    assert price_distance_to_relative(3.0) == 300_000


def test_relative_rejects_zero():
    with pytest.raises(ValueError):
        price_distance_to_relative(0)


def test_relative_snaps_to_symbol_digits():
    """
    Live reject: unrounded alert price vs rounded SL → 10.001 distance →
    relative 1_000_100, which XAU (2 digits, step 1000) refuses.
    """
    assert price_distance_to_relative(10.001, 0.01) == 1_000_000
    assert price_distance_to_relative(10.006, 0.01) == 1_001_000
    # EUR 5 digits: 1 pip = 0.00010 → relative 10; leftover 6th digit snaps
    assert price_distance_to_relative(0.00010, 0.00001) == 10
    assert price_distance_to_relative(0.000101, 0.00001) == 10
    # BTC 0 digits: $500 → 50_000_000, leftover cents snap off
    assert price_distance_to_relative(500.4, 1.0) == 50_000_000


def test_spot_event_payload_type_is_2131():
    """
    Regression: PT_SPOT_EVENT was 2129 (actually UNSUBSCRIBE_SPOTS_REQ),
    so get_spot never saw prices and breakeven never applied.
    """
    assert cc.PT_SUBSCRIBE_SPOTS_REQ == 2127
    assert cc.PT_SUBSCRIBE_SPOTS_RES == 2128
    assert cc.PT_UNSUBSCRIBE_SPOTS_REQ == 2129
    assert cc.PT_UNSUBSCRIBE_SPOTS_RES == 2130
    assert cc.PT_SPOT_EVENT == 2131
    assert cc.PT_ORDER_ERROR_EVENT == 2132
    assert cc.PT_ASSET_LIST_REQ == 2112
    assert cc.PT_ASSET_LIST_RES == 2113
    assert cc.PT_TRADER_REQ == 2121
    assert cc.PT_TRADER_RES == 2122
    assert cc.PT_GET_POSITION_UNREALIZED_PNL_REQ == 2187
    assert cc.PT_GET_POSITION_UNREALIZED_PNL_RES == 2188


def test_get_unrealized_pnl_scales_money_digits():
    client = CTraderClient(
        host="demo.ctraderapi.com",
        client_id="id",
        client_secret="secret",
        access_token="token",
        account_id=42,
    )
    seen = {}

    def fake_request(payload_type, payload, expect=None, expect_any=None):
        seen["type"] = payload_type
        seen["payload"] = payload
        return {
            "payloadType": 2188,
            "payload": {
                "moneyDigits": 2,
                "positionUnrealizedPnL": [
                    {
                        "positionId": 55,
                        "grossUnrealizedPnL": 150,
                        "netUnrealizedPnL": -25,
                    }
                ],
            },
        }

    client._request = fake_request
    rows = client.get_unrealized_pnl()
    assert seen["type"] == 2187
    assert seen["payload"] == {"ctidTraderAccountId": 42}
    assert rows == [{"position_id": 55, "gross": 1.5, "net": -0.25}]


def test_get_deposit_currency_maps_asset_name():
    client = CTraderClient(
        host="demo.ctraderapi.com",
        client_id="id",
        client_secret="secret",
        access_token="token",
        account_id=7,
    )

    def fake_request(payload_type, payload, expect=None, expect_any=None):
        if payload_type == cc.PT_TRADER_REQ:
            return {"payloadType": 2122, "payload": {"trader": {"depositAssetId": 7}}}
        if payload_type == cc.PT_ASSET_LIST_REQ:
            return {
                "payloadType": 2113,
                "payload": {
                    "asset": [
                        {"assetId": 1, "name": "EUR"},
                        {"assetId": 7, "name": "USD"},
                    ]
                },
            }
        raise AssertionError(payload_type)

    client._request = fake_request
    assert client.get_deposit_currency() == "USD"


def test_normalize_spot_quote_scaled_and_decimal():
    # XAU 4339.52 and EUR 1.12308 are both 1/100000 integers. The old
    # >= 1_000_000 cutoff left EUR unscaled (112308).
    assert CTraderClient._normalize_spot_quote(112308) == pytest.approx(1.12308)
    assert CTraderClient._normalize_spot_quote(433_952_000) == pytest.approx(4339.52)
    assert CTraderClient._normalize_spot_quote("112308") == pytest.approx(1.12308)
    assert CTraderClient._normalize_spot_quote(1.12308) == pytest.approx(1.12308)
    assert CTraderClient._normalize_spot_quote(4339.52) == pytest.approx(4339.52)
    assert CTraderClient._normalize_spot_quote(None) is None
    assert CTraderClient._normalize_spot_quote(True) is None
