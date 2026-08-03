"""Policy helpers used by multi-user fan-out (no single-tenant engine)."""

from datetime import datetime, timedelta, timezone

from factor.inbound.models import AlertData
from factor.rules.filter import alert_age_seconds, alert_triggered_at


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
