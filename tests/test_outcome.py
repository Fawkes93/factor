"""Won/lost classification from cTrader close deals."""

from factor.services.outcome import (
    RESULT_BREAKEVEN,
    RESULT_LOST,
    RESULT_UNKNOWN,
    RESULT_WON,
    annotate_order_pnl,
    classify_net_pnl,
    money_to_float,
    summarize_close_deals,
)


def test_money_digits():
    assert money_to_float(150, 2) == 1.5
    assert money_to_float(-2500, 2) == -25.0
    assert money_to_float(10, None) == 10.0


def test_classify_net():
    assert classify_net_pnl(1.0) == RESULT_WON
    assert classify_net_pnl(-1.0) == RESULT_LOST
    assert classify_net_pnl(0.0) == RESULT_BREAKEVEN
    assert classify_net_pnl(0.005) == RESULT_BREAKEVEN


def test_summarize_won_deal():
    deals = [
        {
            "executionPrice": 2010.5,
            "closePositionDetail": {
                "grossProfit": 1500,  # 15.00 with moneyDigits=2
                "swap": -10,
                "commission": -50,
                "moneyDigits": 2,
            },
        }
    ]
    out = summarize_close_deals(deals)
    assert out["result"] == RESULT_WON
    assert out["net_pnl"] == 14.4  # 15 - 0.1 - 0.5
    assert out["close_price"] == 2010.5
    assert out["close_deals"] == 1


def test_summarize_lost_deal():
    deals = [
        {
            "executionPrice": 1990.0,
            "closePositionDetail": {
                "grossProfit": -2000,
                "swap": 0,
                "commission": -50,
                "moneyDigits": 2,
            },
        }
    ]
    out = summarize_close_deals(deals)
    assert out["result"] == RESULT_LOST
    assert out["net_pnl"] == -20.5


def test_summarize_no_close_detail():
    out = summarize_close_deals([{"executionPrice": 1.0}])
    assert out["result"] == RESULT_UNKNOWN
    assert out["close_deals"] == 0


def test_annotate_closed_open_and_blank():
    closed = annotate_order_pnl(
        {
            "status": "closed",
            "detail": {
                "outcome": {
                    "net_pnl": 14.4,
                    "gross_profit": 15.0,
                    "swap": -0.1,
                    "commission": -0.5,
                }
            },
        }
    )
    assert closed["pnl"] == 14.4
    assert closed["pnl_gross"] == 15.0
    assert closed["pnl_swap"] == -0.1
    assert closed["pnl_commission"] == -0.5
    assert closed["pnl_live"] is False

    blank = annotate_order_pnl({"status": "closed", "detail": {}})
    assert blank["pnl"] is None
    assert blank["pnl_live"] is False

    live = annotate_order_pnl(
        {"status": "open", "broker_position_id": 55, "detail": {}},
        {"55": {"net": -0.25, "gross": -0.2}},
    )
    assert live["pnl"] == -0.25
    assert live["pnl_gross"] == -0.2
    assert live["pnl_live"] is True

    missing = annotate_order_pnl(
        {"status": "open", "broker_position_id": "9", "detail": {}},
        {},
    )
    assert missing["pnl"] is None
    assert missing["pnl_live"] is False


def test_mark_closed_sets_result(monkeypatch):
    from factor.db import repos
    from factor.services.breakeven import _mark_order_closed_local

    updated = {}

    def fake_update(oid, **fields):
        updated["id"] = oid
        updated.update(fields)
        return {"id": oid, **fields}

    monkeypatch.setattr(repos, "update_order", fake_update)

    class FakeBroker:
        def position_close_outcome(self, position_id, from_timestamp_ms=None):
            return {
                "result": RESULT_WON,
                "net_pnl": 12.5,
                "gross_profit": 13.0,
                "swap": 0,
                "commission": -0.5,
                "close_price": 2010.0,
                "close_deals": 1,
                "position_id": position_id,
            }

    order = {
        "id": "ord-win",
        "kind": "candle",
        "symbol": "XAU/USD",
        "broker_position_id": "99",
        "created_at": "2026-08-06T10:00:00Z",
        "detail": {},
    }
    r = _mark_order_closed_local(order, "position_gone", broker=FakeBroker())
    assert r["action"] == "closed_local"
    assert r["result"] == RESULT_WON
    assert updated["status"] == "closed"
    assert updated["result"] == RESULT_WON
    assert updated["detail"]["outcome"]["result"] == RESULT_WON
    assert updated["detail"]["outcome"]["net_pnl"] == 12.5
