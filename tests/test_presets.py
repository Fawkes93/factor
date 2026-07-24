from factor.inbound.models import AlertData
from factor.rules.presets import build_order_plan, calc_sl_tp


def test_calc_sl_tp_buy_xau():
    # 300 pts * 0.01 = 3.0
    sl, tp = calc_sl_tp(
        action="BUY",
        entry=4000.0,
        sl_points=300,
        tp_points=600,
        point_size=0.01,
    )
    assert sl == 3997.0
    assert tp == 4006.0


def test_calc_sl_tp_sell_xau():
    sl, tp = calc_sl_tp(
        action="SELL",
        entry=4000.0,
        sl_points=300,
        tp_points=600,
        point_size=0.01,
    )
    assert sl == 4003.0
    assert tp == 3994.0


def test_build_order_plan_buy():
    alert = AlertData(
        alert_id=10,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=4000.0,
        timeframe_min=15,
    )
    plan = build_order_plan(alert)
    assert plan.side == "BUY"
    assert plan.sl_price == 3997.0
    assert plan.tp_price == 4006.0
    assert plan.broker_symbol == "XAUUSD"
