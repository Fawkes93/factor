"""Policy helpers used by multi-user fan-out (no single-tenant engine)."""

from datetime import datetime, timedelta, timezone

from factor import config
from factor.inbound.models import AlertData
from factor.rules.filter import alert_age_seconds, alert_triggered_at, max_alert_age_seconds


def _iso_ago(seconds: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.isoformat().replace("+00:00", "Z")


def test_alert_age_and_triggered_at():
    alert = AlertData(
        alert_id=1,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=1.0,
        timeframe_min=15,
        triggered_at=_iso_ago(2.0),
    )
    ts = alert_triggered_at(alert)
    assert ts is not None
    age = alert_age_seconds(alert)
    assert age is not None
    assert 1.0 < age < 5.0


def test_asia_breakout_age_limit_is_20s(monkeypatch):
    monkeypatch.setattr(config, "MAX_ALERT_AGE_SEC", 5.0)
    monkeypatch.setattr(config, "MAX_ALERT_AGE_ASIA_SEC", 20.0)
    assert max_alert_age_seconds("asia_breakout") == 20.0
    assert max_alert_age_seconds("asia_breakout_h") == 20.0
    assert max_alert_age_seconds("dawn_cross") == 20.0
    assert max_alert_age_seconds("spike") == 5.0
    for kind in ("spike", "spike_neo", "candle", "impulse_retrace"):
        assert max_alert_age_seconds(kind) == 5.0


def test_missing_triggered_at():
    alert = AlertData(
        alert_id=2,
        kind="candle",
        symbol="XAU/USD",
        side="UP",
        price=1.0,
        timeframe_min=15,
        triggered_at=None,
    )
    assert alert_triggered_at(alert) is None
    assert alert_age_seconds(alert) is None
