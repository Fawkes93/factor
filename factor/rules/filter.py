"""
Decide whether an alert should become a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from factor import config
from factor.inbound.models import AlertData
from factor.store.state import StateStore


@dataclass
class Decision:
    ok: bool
    reason: str
    # when ok=False: skip | reject | disabled | duplicate | limit


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

    `when` should be timezone-aware (UTC preferred). Uses wall-clock "now" UTC
    if when is None.
    """
    if enabled is None:
        enabled = bool(getattr(config, "MARKET_OPEN_BLACKOUT", True))
    if not enabled:
        return None

    mins = int(minutes if minutes is not None else getattr(config, "MARKET_OPEN_BLACKOUT_MINUTES", 15) or 0)
    if mins <= 0:
        return None

    hhmm = _parse_hhmm(open_time if open_time is not None else getattr(config, "MARKET_OPEN_TIME", "01:05") or "01:05")
    if not hhmm:
        return None
    open_h, open_m = hhmm

    tz_s = (tz_name if tz_name is not None else getattr(config, "MARKET_OPEN_TZ", "Europe/Bucharest") or "Europe/Bucharest").strip()
    try:
        tz = ZoneInfo(tz_s)
    except Exception:
        # Fallback fixed +03 (Bucharest summer) if zoneinfo data missing
        tz = timezone(timedelta(hours=3), name=tz_s or "UTC+3")

    if when is None:
        when_utc = datetime.now(timezone.utc)
    else:
        when_utc = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        when_utc = when_utc.astimezone(timezone.utc)

    local = when_utc.astimezone(tz)

    wd_cfg = weekdays if weekdays is not None else getattr(config, "MARKET_OPEN_WEEKDAYS", None)
    if wd_cfg:
        try:
            allowed = {int(x) for x in wd_cfg}
        except (TypeError, ValueError):
            allowed = set()
        # Python: Monday=0 … Sunday=6
        if allowed and local.weekday() not in allowed:
            return None

    open_t = time(open_h, open_m)
    open_dt = datetime.combine(local.date(), open_t, tzinfo=tz)
    end_dt = open_dt + timedelta(minutes=mins)

    # Also cover open that fell on previous calendar day when window crosses midnight
    # (not needed for 15m after 01:05, but safe if open is 23:55)
    if local < open_dt:
        open_dt_prev = open_dt - timedelta(days=1)
        end_prev = open_dt_prev + timedelta(minutes=mins)
        if open_dt_prev <= local < end_prev:
            open_dt, end_dt = open_dt_prev, end_prev
        else:
            return None
    elif local >= end_dt:
        return None

    # local in [open_dt, end_dt)
    elapsed = (local - open_dt).total_seconds()
    left = max(0.0, mins * 60 - elapsed)
    return (
        f"market_open_blackout:"
        f"{open_h:02d}:{open_m:02d}-{end_dt.strftime('%H:%M')} "
        f"{tz_s} (~{left:.0f}s left)"
    )


def decide(alert: AlertData, store: StateStore) -> Decision:
    """
    Local policy only (no broker I/O).

    - de-dupe on alert_id (same webhook never opens twice)
    - kind / symbol / freshness filters
    - market-open blackout
    - optional local day/cooldown limits

    Open-position gate is **not** here — engine asks cTrader (reconcile)
    so closed SL/TP positions do not leave a stale SQLite "open" block.
    """
    if not config.TRADING_ENABLED:
        return Decision(False, "trading_disabled")

    if store.was_processed(alert.alert_id):
        return Decision(False, "duplicate_alert_id")

    if alert.side == "NEUTRAL" or not alert.is_directional:
        return Decision(False, "side_neutral")

    if config.TRADE_KINDS and alert.kind not in config.TRADE_KINDS:
        return Decision(False, f"kind_not_allowed:{alert.kind}")

    if config.TRADE_SYMBOLS and alert.symbol not in config.TRADE_SYMBOLS:
        return Decision(False, f"symbol_not_allowed:{alert.symbol}")

    if alert.symbol not in config.PRESETS:
        return Decision(False, f"no_preset:{alert.symbol}")

    # Freshness: only trade live-ish signals (default 5s).
    max_age = float(getattr(config, "MAX_ALERT_AGE_SEC", 5) or 5)
    if max_age > 0:
        age = alert_age_seconds(alert)
        if age is None:
            return Decision(False, "alert_age_unknown")
        if age < 0:
            # Clock skew slightly in the future — allow a small slop
            if age < -2.0:
                return Decision(False, f"alert_in_future:{age:.1f}s")
        elif age > max_age:
            return Decision(False, f"alert_too_old:{age:.1f}s>{max_age:g}s")

    # Market-open blackout (first N minutes after open)
    bo = market_open_blackout(alert_triggered_at(alert))
    if bo:
        return Decision(False, bo)

    day_n = store.count_trades_today()
    if day_n >= config.MAX_TRADES_PER_DAY:
        return Decision(False, f"max_trades_per_day:{day_n}")

    last_ts = store.last_trade_time(alert.symbol)
    if last_ts is not None and config.COOLDOWN_SEC > 0:
        age = store.seconds_since(last_ts)
        if age is not None and age < config.COOLDOWN_SEC:
            return Decision(
                False,
                f"cooldown:{int(config.COOLDOWN_SEC - age)}s_left",
            )

    return Decision(True, "ok")
