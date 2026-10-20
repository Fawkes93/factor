from factor.engine_multi import _pref_overrides
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


def test_asia_without_override_ignores_user_points_when_reaktor_sends_prices():
    alert = AlertData(
        alert_id=43,
        kind="asia_breakout",
        symbol="XAU/USD",
        side="UP",
        price=3310.0,
        timeframe_min=15,
        metrics={"sl_price": 3300.0, "tp_price": 3340.0},
    )
    plan = build_order_plan(
        alert,
        overrides={"sl_points": 100, "tp_points": 200, "volume_lots": 0.03},
    )
    assert plan.sl_price == 3300.0
    assert plan.tp_price == 3340.0
    assert plan.volume_lots == 0.03


def test_asia_override_uses_user_points_not_reaktor_prices():
    alert = AlertData(
        alert_id=44,
        kind="asia_breakout",
        symbol="XAU/USD",
        side="UP",
        price=3310.0,
        timeframe_min=15,
        metrics={
            "sl_price": 3300.0,
            "tp_price": 3340.0,
            "trail_arm_pts": 400,
            "trail_pts": 500,
        },
    )
    plan = build_order_plan(
        alert,
        overrides={
            "sl_points": 800,
            "tp_points": 1600,
            "volume_lots": 0.05,
            "ignore_signal_sl_tp": True,
        },
    )
    # 800 pts * 0.01 = 8.0, 1600 pts * 0.01 = 16.0
    assert plan.side == "BUY"
    assert plan.sl_price == 3302.0
    assert plan.tp_price == 3326.0
    assert plan.sl_points == 800
    assert plan.tp_points == 1600
    assert plan.volume_lots == 0.05


def test_asia_override_sell_side():
    alert = AlertData(
        alert_id=45,
        kind="asia_breakout",
        symbol="XAU/USD",
        side="DOWN",
        price=3310.55,
        timeframe_min=15,
        metrics={"sl_price": 3320.0, "tp_price": 3280.0},
    )
    plan = build_order_plan(
        alert,
        overrides={
            "sl_points": 500,
            "tp_points": 1500,
            "ignore_signal_sl_tp": True,
        },
    )
    assert plan.entry_price == 3310.55
    assert plan.side == "SELL"
    assert plan.sl_price == 3315.55
    assert plan.tp_price == 3295.55


def test_build_order_plan_uses_crt_metrics_not_preset_distances():
    alert = AlertData(
        alert_id=80,
        kind="crt",
        symbol="XAU/USD",
        side="UP",
        price=4000.0,
        timeframe_min=120,
        metrics={
            "sl_price": 3985.0,
            "tp_price": 4075.0,
            "tp1_price": 4015.0,
            "entry_price": 4000.0,
        },
    )
    plan = build_order_plan(
        alert,
        overrides={"volume_lots": 0.02, "sl_points": 1000, "tp_points": 1000},
    )
    # Preset 1000 pts would be 3990 / 4010. Webhook absolutes win.
    assert plan.sl_price == 3985.0
    assert plan.tp_price == 4075.0
    assert plan.volume_lots == 0.02
    assert plan.kind == "crt"
    assert plan.point_size == 0.01

    eur = AlertData(
        alert_id=81,
        kind="crt",
        symbol="EUR/USD",
        side="DOWN",
        price=1.10000,
        timeframe_min=60,
        metrics={"sl_price": 1.10200, "tp_price": 1.09400, "tp1_price": 1.09900},
    )
    eur_plan = build_order_plan(
        eur, overrides={"sl_points": 150, "tp_points": 300, "volume_lots": 0.02}
    )
    # Preset 150/300 pts would be 1.10150 / 1.09700.
    assert eur_plan.side == "SELL"
    assert eur_plan.sl_price == 1.10200
    assert eur_plan.tp_price == 1.09400
    assert eur_plan.point_size == 0.00001
    assert eur_plan.volume_lots == 0.02


def test_pref_overrides_only_ignores_signal_for_asia_when_flag_on():
    pref = {"volume_lots": 0.02, "sl_points": 800, "tp_points": 1600, "sl_tp_override": True}
    asia = _pref_overrides(pref, kind="asia_breakout")
    assert asia["ignore_signal_sl_tp"] is True
    assert asia["sl_points"] == 800
    overnight = _pref_overrides(pref, kind="asia_breakout_h")
    assert overnight["ignore_signal_sl_tp"] is True
    dawn = _pref_overrides(pref, kind="dawn_cross")
    assert "ignore_signal_sl_tp" not in dawn
    crt = _pref_overrides(pref, kind="crt")
    assert "ignore_signal_sl_tp" not in crt
    candle = _pref_overrides(pref, kind="candle")
    assert "ignore_signal_sl_tp" not in candle
    off = _pref_overrides(
        {"volume_lots": 0.01, "sl_points": 1000, "tp_points": 1000},
        kind="asia_breakout",
    )
    assert "ignore_signal_sl_tp" not in off
