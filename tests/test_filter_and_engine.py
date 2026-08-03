from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from factor import config
from factor.broker.base import OrderResult
from factor.engine import process_alert
from factor.inbound.models import AlertData
from factor.rules.presets import OrderPlan
from factor.store.state import StateStore


def _iso_ago(seconds: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.isoformat().replace("+00:00", "Z")



class FakeBroker:
    """Test double — does not call cTrader."""

    name = "fake"

    def __init__(self, open_position: bool = False) -> None:
        self._open = open_position
        self.place_calls = 0

    def has_open_position(self, broker_symbol=None) -> bool:
        return bool(self._open)

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        self.place_calls += 1
        self._open = True  # after a fill, account has exposure
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
    monkeypatch.setattr(config, "MAX_OPEN_TOTAL", 1)
    monkeypatch.setattr(config, "MAX_OPEN_PER_SYMBOL", 5)
    monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 100)
    monkeypatch.setattr(config, "COOLDOWN_SEC", 0)
    monkeypatch.setattr(config, "MAX_ALERT_AGE_SEC", 5.0)
    # Avoid flaky skips when tests run near 01:05 local
    monkeypatch.setattr(config, "MARKET_OPEN_BLACKOUT", False)


def _alert(**kwargs):
    base = dict(
        alert_id=100,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=4000.0,
        timeframe_min=15,
        triggered_at=_iso_ago(0.5),  # fresh by default
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
    assert r1["plan"]["volume_lots"] == 0.01
    assert r1["plan"]["sl_price"] == 3990.0
    assert r1["plan"]["tp_price"] == 4010.0

    r2 = process_alert(_alert(alert_id=200), store=store, broker=broker)
    assert r2["action"] == "skip"
    assert r2["reason"] == "duplicate_alert_id"

    trades = store.list_trades()
    assert len(trades) == 1


def test_broker_open_position_blocks_further_alerts(store):
    """cTrader open position → skip new alerts; alert_id still de-duped."""
    broker = FakeBroker(open_position=False)
    r1 = process_alert(_alert(alert_id=300), store=store, broker=broker)
    assert r1["action"] == "traded"
    assert broker.place_calls == 1

    r2 = process_alert(_alert(alert_id=301, side="DOWN"), store=store, broker=broker)
    assert r2["action"] == "skip"
    assert r2["reason"] == "broker_has_open_position"
    assert broker.place_calls == 1  # no second order

    # Same alert_id never re-opens even after flat
    broker._open = False
    r3 = process_alert(_alert(alert_id=301, side="DOWN"), store=store, broker=broker)
    assert r3["action"] == "skip"
    assert r3["reason"] == "duplicate_alert_id"

    # New alert_id can trade when flat
    r4 = process_alert(_alert(alert_id=302), store=store, broker=broker)
    assert r4["action"] == "traded"
    assert broker.place_calls == 2


def test_same_alert_id_never_double_fires(store):
    broker = FakeBroker()
    assert process_alert(_alert(alert_id=500), store=store, broker=broker)["action"] == "traded"
    broker._open = False  # even if position closed
    r2 = process_alert(_alert(alert_id=500), store=store, broker=broker)
    assert r2["action"] == "skip"
    assert r2["reason"] == "duplicate_alert_id"
    assert broker.place_calls == 1


def test_stale_alert_skipped_no_order(store):
    """Catch-up / old alerts: log skip, never hit the broker."""
    broker = FakeBroker()
    r = process_alert(
        _alert(alert_id=400, triggered_at=_iso_ago(30)),
        store=store,
        broker=broker,
    )
    assert r["action"] == "skip"
    assert "alert_too_old" in r["reason"]
    assert store.list_trades() == []
    assert store.was_processed(400)


def test_fresh_alert_still_trades(store):
    broker = FakeBroker()
    r = process_alert(
        _alert(alert_id=401, triggered_at=_iso_ago(1)),
        store=store,
        broker=broker,
    )
    assert r["action"] == "traded"
