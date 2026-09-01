from factor.inbound.models import AlertData
from factor.rules.presets import build_order_plan, calc_sl_tp


def test_calc_sl_tp_buy_xau():
    # 1000 pts * 0.01 = 10.0
    sl, tp = calc_sl_tp(
        action="BUY",
        entry=4000.0,
        sl_points=1000,
        tp_points=1000,
        point_size=0.01,
    )
    assert sl == 3990.0
    assert tp == 4010.0


def test_calc_sl_tp_sell_xau():
    sl, tp = calc_sl_tp(
        action="SELL",
        entry=4000.0,
        sl_points=1000,
        tp_points=1000,
        point_size=0.01,
    )
    assert sl == 4010.0
    assert tp == 3990.0


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
    assert plan.volume_lots == 0.01
    assert plan.sl_points == 1000
    assert plan.tp_points == 1000
    assert plan.sl_price == 3990.0
    assert plan.tp_price == 4010.0
    assert plan.broker_symbol == "XAUUSD"


def test_build_order_plan_rounds_entry_to_symbol_digits():
    """Candle SL/TP from a messy Reaktor price must still be 2-digit XAU."""
    alert = AlertData(
        alert_id=11,
        kind="candle",
        symbol="XAU/USD",
        side="DOWN",
        price=3456.789,
        timeframe_min=15,
    )
    plan = build_order_plan(alert)
    assert plan.entry_price == 3456.79
    assert plan.sl_price == 3466.79
    assert plan.tp_price == 3446.79
    from factor.broker.ctrader import price_distance_to_relative

    rel = price_distance_to_relative(
        abs(plan.entry_price - plan.sl_price), plan.point_size
    )
    assert rel == 1_000_000
    assert rel % 1000 == 0


def test_build_order_plan_uses_asia_metrics_sl_tp():
    alert = AlertData(
        alert_id=42,
        kind="asia_breakout",
        symbol="XAU/USD",
        side="UP",
        price=3310.0,
        timeframe_min=15,
        metrics={
            "sl_price": 3300.0,
            "tp_price": 3340.0,
            "sl_pts": 1000,
            "tp_pts": 3000,
            "trail_arm_pts": 400,
            "trail_pts": 500,
        },
    )
    plan = build_order_plan(alert, overrides={"volume_lots": 0.02})
    assert plan.side == "BUY"
    assert plan.volume_lots == 0.02
    assert plan.sl_price == 3300.0
    assert plan.tp_price == 3340.0
    assert plan.sl_points == 1000
    assert plan.tp_points == 3000
    assert plan.kind == "asia_breakout"
