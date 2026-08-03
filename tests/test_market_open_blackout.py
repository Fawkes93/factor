"""Market-open blackout: ignore alerts in first N minutes after open."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from factor.rules.filter import market_open_blackout


def _at_bucharest(hour: int, minute: int, day: int = 3, month: int = 8, year: int = 2026):
    """Build a UTC datetime that is hour:minute in Europe/Bucharest."""
    local = datetime(year, month, day, hour, minute, 0, tzinfo=ZoneInfo("Europe/Bucharest"))
    return local.astimezone(timezone.utc)


def test_inside_blackout_window():
    # Open 01:05, blackout 15m → 01:05..01:19:59
    when = _at_bucharest(1, 10)
    reason = market_open_blackout(
        when,
        enabled=True,
        open_time="01:05",
        tz_name="Europe/Bucharest",
        minutes=15,
        weekdays=[],
    )
    assert reason is not None
    assert reason.startswith("market_open_blackout:")


def test_at_exact_open_is_blocked():
    when = _at_bucharest(1, 5)
    reason = market_open_blackout(
        when,
        enabled=True,
        open_time="01:05",
        tz_name="Europe/Bucharest",
        minutes=15,
        weekdays=[],
    )
    assert reason is not None


def test_after_window_allowed():
    when = _at_bucharest(1, 21)
    reason = market_open_blackout(
        when,
        enabled=True,
        open_time="01:05",
        tz_name="Europe/Bucharest",
        minutes=15,
        weekdays=[],
    )
    assert reason is None


def test_before_open_allowed():
    when = _at_bucharest(1, 0)
    reason = market_open_blackout(
        when,
        enabled=True,
        open_time="01:05",
        tz_name="Europe/Bucharest",
        minutes=15,
        weekdays=[],
    )
    assert reason is None


def test_disabled():
    when = _at_bucharest(1, 10)
    assert (
        market_open_blackout(
            when,
            enabled=False,
            open_time="01:05",
            tz_name="Europe/Bucharest",
            minutes=15,
        )
        is None
    )


def test_weekday_filter():
    # 2026-08-03 is a Monday (weekday 0)
    when = _at_bucharest(1, 10, day=3, month=8, year=2026)
    # Only Sunday (6) — Monday alert should pass
    assert (
        market_open_blackout(
            when,
            enabled=True,
            open_time="01:05",
            tz_name="Europe/Bucharest",
            minutes=15,
            weekdays=["6"],
        )
        is None
    )
    # Monday allowed in filter → blocked
    assert (
        market_open_blackout(
            when,
            enabled=True,
            open_time="01:05",
            tz_name="Europe/Bucharest",
            minutes=15,
            weekdays=["0"],
        )
        is not None
    )
