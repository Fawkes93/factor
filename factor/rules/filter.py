"""
Shared alert policy helpers (freshness, market-open blackout).

Multi-user execution lives in engine_multi; these pure helpers are shared.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from factor import config
from factor.inbound.models import AlertData
from factor.rules.kinds import is_session_managed


def alert_triggered_at(alert: AlertData) -> Optional[datetime]:
    """Parse alert.triggered_at as timezone-aware UTC datetime."""
    raw = (alert.triggered_at or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        return None


def max_alert_age_seconds(kind: str) -> float:
    """Seconds an alert may age before it is skipped.

    Spike-style alerts use the tight window. Session entries
    (Asia breakout, Asia Overnight H, Dawn Cross) use the longer one.
    """
    if is_session_managed(kind):
        raw = getattr(config, "MAX_ALERT_AGE_ASIA_SEC", 20)
    else:
        raw = getattr(config, "MAX_ALERT_AGE_SEC", 5)
    try:
        limit = float(raw if raw is not None else 0)
    except (TypeError, ValueError):
        limit = 5.0
    return limit


def alert_age_seconds(alert: AlertData) -> Optional[float]:
    """
    Seconds since alert trigger (UTC). None if timestamp missing/unparseable.
    """
    ts = alert_triggered_at(alert)
    if ts is None:
        return None
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds()


def _parse_hhmm(raw: str) -> Optional[Tuple[int, int]]:
    s = (raw or "").strip()
    if not s:
        return None
    parts = s.replace(".", ":").split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m
    except (TypeError, ValueError):
        return None


def market_open_blackout(
    when: Optional[datetime] = None,
    *,
    enabled: Optional[bool] = None,
    open_time: Optional[str] = None,
    tz_name: Optional[str] = None,
    minutes: Optional[int] = None,
    weekdays: Optional[list] = None,
) -> Optional[str]:
    """
    If `when` falls in [market open, open + N minutes) in the configured
    timezone, return a skip reason string. Otherwise None.
    """
    if enabled is None:
        enabled = bool(getattr(config, "MARKET_OPEN_BLACKOUT", True))
    if not enabled:
        return None

    mins = int(
        minutes
        if minutes is not None
        else getattr(config, "MARKET_OPEN_BLACKOUT_MINUTES", 15) or 0
    )
    if mins <= 0:
        return None

    hhmm = _parse_hhmm(
        open_time
        if open_time is not None
        else getattr(config, "MARKET_OPEN_TIME", "01:05") or "01:05"
    )
    if not hhmm:
        return None
    open_h, open_m = hhmm

    tz_s = (
        tz_name
        if tz_name is not None
        else getattr(config, "MARKET_OPEN_TZ", "Europe/Bucharest") or "Europe/Bucharest"
    ).strip()
    try:
        tz = ZoneInfo(tz_s)
    except Exception:
        tz = timezone(timedelta(hours=3), name=tz_s or "UTC+3")

    if when is None:
        when_utc = datetime.now(timezone.utc)
    else:
        when_utc = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        when_utc = when_utc.astimezone(timezone.utc)

    local = when_utc.astimezone(tz)

    wd_cfg = (
        weekdays
        if weekdays is not None
        else getattr(config, "MARKET_OPEN_WEEKDAYS", None)
    )
    if wd_cfg:
        try:
            allowed = {int(x) for x in wd_cfg}
        except (TypeError, ValueError):
            allowed = set()
        if allowed and local.weekday() not in allowed:
            return None

    open_t = time(open_h, open_m)
    open_dt = datetime.combine(local.date(), open_t, tzinfo=tz)
    end_dt = open_dt + timedelta(minutes=mins)

    if local < open_dt:
        open_dt_prev = open_dt - timedelta(days=1)
        end_prev = open_dt_prev + timedelta(minutes=mins)
        if open_dt_prev <= local < end_prev:
            open_dt, end_dt = open_dt_prev, end_prev
        else:
            return None
    elif local >= end_dt:
        return None

    elapsed = (local - open_dt).total_seconds()
    left = max(0.0, mins * 60 - elapsed)
    return (
        f"market_open_blackout:"
        f"{open_h:02d}:{open_m:02d}-{end_dt.strftime('%H:%M')} "
        f"{tz_s} (~{left:.0f}s left)"
    )
