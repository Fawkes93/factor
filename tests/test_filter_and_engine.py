from pathlib import Path

import pytest

from factor import config
from factor.broker.base import OrderResult
from factor.engine import process_alert
from factor.inbound.models import AlertData
from factor.rules.presets import OrderPlan
from factor.store.state import StateStore


class FakeBroker:
    """Test double — does not call cTrader."""

    name = "fake"

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        return OrderResult(
            ok=True,
            status="open",
            broker_order_id=f"fake-{plan.alert_id}",
            message="fake fill",
            detail=plan.as_dict(),
        )


@pytest.fixture()
def store(tmp_path: Path):
    return StateStore(tmp_path / "test.db")


@pytest.fixture(autouse=True)
def _enable_trading(monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "TRADE_KINDS", ["candle", "spike"])
    monkeypatch.setattr(config, "TRADE_SYMBOLS", ["XAU/USD"])
    monkeypatch.setattr(config, "MAX_OPEN_PER_SYMBOL", 5)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 100)
    monkeypatch.setattr(config, "COOLDOWN_SEC", 0)


def _alert(**kwargs):
    base = dict(
        alert_id=100,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=4000.0,
        timeframe_min=15,
    )
    base.update(kwargs)
    return AlertData(**base)


def test_trading_disabled(store, monkeypatch):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    r = process_alert(_alert(), store=store, broker=FakeBroker())
    assert r["action"] == "skip"
    assert r["reason"] == "trading_disabled"


def test_neutral_skipped(store):
    r = process_alert(
        _alert(side="NEUTRAL", kind="econ_calendar"),
        store=store,
        broker=FakeBroker(),
    )
    assert r["action"] == "skip"
    assert r["reason"] == "side_neutral"


def test_kind_filter(store):
    r = process_alert(
        _alert(kind="cooloff"),
        store=store,
        broker=FakeBroker(),
    )
    assert r["action"] == "skip"
    assert "kind_not_allowed" in r["reason"]


def test_trade_and_dedupe(store):
    broker = FakeBroker()
    r1 = process_alert(_alert(alert_id=200), store=store, broker=broker)
    assert r1["action"] == "traded"
    assert r1["plan"]["side"] == "BUY"
    assert r1["plan"]["sl_price"] == 3997.0

    r2 = process_alert(_alert(alert_id=200), store=store, broker=broker)
    assert r2["action"] == "skip"
    assert r2["reason"] == "duplicate_alert_id"

    trades = store.list_trades()
    assert len(trades) == 1
