"""
Post-entry management for CRT (kind=crt).

Reaktor sends one market alert after the limit level is reached, with
absolute sl_price, tp_price (runner) and tp1_price (1R partial). Factor
does not move the stop after the partial unless metrics.be_after_tp1 is
true. The original stop stays for the whole trade. There is no trail.

Flatten whichever comes first: max_hold_until (or open time + max_hold_hours
when the timestamp is missing; calendar time, weekends included) or the
weekly clock. The weekly clock is Factor config, copied onto the order
when it is placed. Default Friday 23:45 Europe/Chisinau.

A position opened at or after that Friday's clock waits until the next
Friday. A poll that lands later still closes it: the deadline stays on
the entry's Friday, including across the weekend.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from factor import config
from factor.rules.presets import round_price

_WEEKDAY = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}
_WEEKDAY_NAME = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def parse_weekly_flatten(raw: Optional[str]) -> Tuple[int, str, str]:
    """'FRI 23:45 Europe/Chisinau' → (4, '23:45', 'Europe/Chisinau')."""
    text = (raw or "").strip()
    parts = text.split()
    if len(parts) < 3:
        return 4, "23:45", "Europe/Chisinau"
    weekday = _WEEKDAY.get(parts[0].upper(), 4)
    hhmm = parts[1]
    hour, minute = _parse_hhmm(hhmm)
    if hour is None:
        hhmm = "23:45"
    else:
        hhmm = f"{hour:02d}:{minute:02d}"
    tz_name = parts[2] or "Europe/Chisinau"
    return weekday, hhmm, tz_name


def _parse_hhmm(raw: str) -> Tuple[Optional[int], int]:
    parts = str(raw or "").strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return None, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, 0
    return hour, minute


def _metric_float(metrics: Dict[str, Any], key: str) -> Optional[float]:
    if key not in metrics:
        return None
    raw = metrics.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _metric_bool(metrics: Dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in metrics:
        return default
    raw = metrics.get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def crt_detail_from_metrics(
    metrics: Optional[Dict[str, Any]],
    triggered_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Protect blob stored on factor_orders.detail.protect.

    Weekly flatten comes from CRT_WEEKLY_FLATTEN, not metrics.weekly_flatten.
    """
    m = metrics if isinstance(metrics, dict) else {}
    weekday, hhmm, tz_name = parse_weekly_flatten(
        getattr(config, "CRT_WEEKLY_FLATTEN", None) or "FRI 23:45 Europe/Chisinau"
    )
    until_raw = m.get("max_hold_until") if "max_hold_until" in m else None
    until_iso = None
    if until_raw not in (None, ""):
        parsed = _parse_iso(str(until_raw))
        until_iso = _iso_z(parsed) if parsed is not None else str(until_raw)
    hours = _metric_float(m, "max_hold_hours")
    tp1 = _metric_float(m, "tp1_price")
    frac = _metric_float(m, "tp1_frac")
    if frac is None or frac <= 0:
        frac = 0.5
    if frac > 1:
        frac = 1.0
    return {
        "enabled": True,
        "kind": "crt",
        "tp1_price": tp1,
        "tp1_frac": float(frac),
        "be_after_tp1": _metric_bool(m, "be_after_tp1", False),
        "max_hold_until": until_iso,
        "max_hold_hours": hours,
        "flatten_weekday": weekday,
        "flatten_time": hhmm,
        "flatten_tz": tz_name,
        "partial_done": False,
        "flattened": False,
        "entry_at": triggered_at,
    }


def crt_mark(side: str, bid: float, ask: float) -> float:
    """Exit-side mark: bid for a BUY, ask for a SELL."""
    from factor.services.breakeven import mark_price_for_side

    return mark_price_for_side(side=side, bid=float(bid), ask=float(ask))


def tp1_reached(side: str, mark: float, tp1: float) -> bool:
    """True when the exit-side mark has reached tp1.

    `mark` is the bid for a BUY and the ask for a SELL (see crt_mark).
    """
    side_u = (side or "").upper()
    if side_u in ("BUY", "UP", "LONG"):
        return float(mark) + 1e-12 >= float(tp1)
    if side_u in ("SELL", "DOWN", "SHORT"):
        return float(mark) - 1e-12 <= float(tp1)
    return False


def crt_stale_fill_reason(
    *,
    side: str,
    mark: float,
    sl_price: float,
    tp1_price: Optional[float],
    entry_price: float,
    point_size: float,
    max_slip_pts: Optional[float] = None,
) -> Optional[str]:
    """Reject a market that is already through the stop or the partial.

    Absolute sl_price / tp1_price are not shifted when the mark differs
    from entry_price. max_slip_pts is optional; None or <= 0 leaves it off.
    """
    side_u = (side or "").upper()
    is_buy = side_u in ("BUY", "UP", "LONG")
    px = float(mark)
    sl = float(sl_price)
    if is_buy:
        if px <= sl:
            return "crt_stale_fill:beyond_sl"
        if tp1_price is not None and px >= float(tp1_price):
            return "crt_stale_fill:beyond_tp1"
    else:
        if px >= sl:
            return "crt_stale_fill:beyond_sl"
        if tp1_price is not None and px <= float(tp1_price):
            return "crt_stale_fill:beyond_tp1"
    cap = float(max_slip_pts or 0)
    psz = float(point_size or 0)
    if cap > 0 and psz > 0:
        slip_pts = abs(px - float(entry_price)) / psz
        if slip_pts > cap + 1e-9:
            return "crt_stale_fill:slip"
    return None


def crt_prices_from_metrics(
    metrics: Optional[Dict[str, Any]],
) -> Optional[Tuple[float, float]]:
    """(sl, tp) when both webhook prices are present and positive."""
    m = metrics if isinstance(metrics, dict) else {}
    sl = _metric_float(m, "sl_price")
    tp = _metric_float(m, "tp_price")
    if sl is None or tp is None or sl <= 0 or tp <= 0:
        return None
    return sl, tp


def crt_spot_block_reason(broker: Any, plan: Any, metrics: Optional[Dict[str, Any]]) -> Optional[str]:
    """Live stale-fill check. Dry-run has no quote, so it does not block."""
    if getattr(broker, "_dry_run", False):
        return None
    m = metrics if isinstance(metrics, dict) else {}
    try:
        spot = broker.get_spot(str(plan.broker_symbol))
        mark = crt_mark(plan.side, float(spot["bid"]), float(spot["ask"]))
    except Exception:
        return "crt_stale_fill:no_spot"
    tp1 = _metric_float(m, "tp1_price")
    entry = _metric_float(m, "entry_price")
    if entry is None:
        entry = float(plan.entry_price)
    # A scale bug (EUR protocol 112308 vs entry 1.12308) must not look like
    # a real beyond_tp1 / beyond_sl skip. 5% is far outside any live slip.
    if entry > 0 and abs(mark - float(entry)) / float(entry) > 0.05:
        return "crt_stale_fill:bad_spot"
    cap_raw = getattr(config, "CRT_MAX_ENTRY_SLIP_PTS", 0) or 0
    try:
        cap = float(cap_raw)
    except (TypeError, ValueError):
        cap = 0.0
    return crt_stale_fill_reason(
        side=str(plan.side),
        mark=mark,
        sl_price=float(plan.sl_price),
        tp1_price=tp1,
        entry_price=float(entry),
        point_size=float(plan.point_size or 0),
        max_slip_pts=cap if cap > 0 else None,
    )


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "Europe/Chisinau")
    except Exception:
        return ZoneInfo("Europe/Chisinau")


def _weekly_deadline(
    entry_at: datetime,
    *,
    weekday: int,
    hour: int,
    minute: int,
    tz: ZoneInfo,
) -> datetime:
    """First configured clock strictly after the entry, in local wall time.

    Built with datetime.combine so a DST week still lands on 23:45 local,
    not 23:45 plus or minus the offset change. Adding 7×24 hours would
    miss the wall clock across the last Sunday of March or October.
    """
    entry_local = _as_utc(entry_at).astimezone(tz)
    monday = entry_local.date() - timedelta(days=entry_local.weekday())
    target = monday + timedelta(days=int(weekday))
    deadline = datetime.combine(target, time(hour, minute), tzinfo=tz)
    # Opened at or after this week's clock → the next one. Equal counts
    # as not-before, so a fill exactly at 23:45 waits until next week.
    if entry_local >= deadline:
        deadline = datetime.combine(
            target + timedelta(days=7), time(hour, minute), tzinfo=tz
        )
    return deadline


def _max_hold_deadline(
    detail: Dict[str, Any], entry_at: Optional[datetime]
) -> Optional[datetime]:
    raw = detail.get("max_hold_until")
    if raw not in (None, ""):
        parsed = _parse_iso(str(raw))
        if parsed is not None:
            return parsed
    hours = detail.get("max_hold_hours")
    try:
        hold = float(hours) if hours not in (None, "") else 0.0
    except (TypeError, ValueError):
        hold = 0.0
    if hold > 0 and entry_at is not None:
        return _as_utc(entry_at) + timedelta(hours=hold)
    return None


def crt_flatten_due(
    now: datetime,
    detail: Optional[Dict[str, Any]],
    entry_at: Optional[datetime],
) -> Optional[str]:
    """Weekly clock or max-hold, whichever deadline is already due.

    Weekly is skipped when CRT_WEEKLY_FLATTEN_ENABLED is off. A trade
    opened at or after this week's Friday clock is not due until the next
    Friday. Thursday never matches a Friday deadline.
    """
    blob = detail if isinstance(detail, dict) else {}
    now_utc = _as_utc(now)
    due: list[Tuple[datetime, str]] = []

    weekly_on = bool(getattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", True))
    if weekly_on and entry_at is not None:
        weekday = blob.get("flatten_weekday")
        if weekday is None or weekday == "":
            weekday = 4
        try:
            weekday_i = int(weekday)
        except (TypeError, ValueError):
            weekday_i = 4
        hhmm = str(blob.get("flatten_time") or "23:45")
        hour, minute = _parse_hhmm(hhmm)
        if hour is None:
            hour, minute = 23, 45
            hhmm = "23:45"
        tz_name = str(blob.get("flatten_tz") or "Europe/Chisinau")
        deadline = _weekly_deadline(
            entry_at,
            weekday=weekday_i,
            hour=hour,
            minute=minute,
            tz=_zone(tz_name),
        )
        if now_utc >= deadline.astimezone(timezone.utc):
            name = _WEEKDAY_NAME[weekday_i] if 0 <= weekday_i <= 6 else "FRI"
            due.append((deadline.astimezone(timezone.utc), f"weekly:{name} {hhmm} {tz_name}"))

    hold = _max_hold_deadline(blob, entry_at)
    if hold is not None and now_utc >= hold:
        due.append((hold, f"max_hold:{_iso_z(hold)}"))
    if not due:
        return None
    due.sort(key=lambda item: item[0])
    return due[0][1]


def round_down_to_step(volume: float, step: int) -> int:
    step_i = int(step)
    if step_i <= 0:
        return int(math.floor(float(volume) + 1e-6))
    units = int(math.floor(float(volume) + 1e-6))
    return (units // step_i) * step_i


def crt_partial_volume(
    volume: Any,
    frac: float,
    step: int,
    min_volume: int,
) -> Tuple[Optional[int], str]:
    """(close_units, 'partial' | 'full' | 'skip').

    Half the position is rounded down to the volume step. Below the broker
    minimum, or not a whole step, is a skip. If that close would leave a
    stub under the minimum, close the whole position instead.
    """
    try:
        vol = int(float(volume))
    except (TypeError, ValueError):
        return None, "skip"
    if vol <= 0:
        return None, "skip"
    try:
        step_i = int(step)
    except (TypeError, ValueError):
        step_i = 1
    if step_i <= 0:
        step_i = 1
    try:
        min_v = int(min_volume)
    except (TypeError, ValueError):
        min_v = step_i
    if min_v <= 0:
        min_v = step_i
    try:
        frac_f = float(frac)
    except (TypeError, ValueError):
        frac_f = 0.5
    if frac_f <= 0:
        return None, "skip"
    raw = float(vol) * frac_f
    partial = round_down_to_step(raw, step_i)
    if partial <= 0 or partial < min_v:
        return None, "skip"
    remainder = vol - partial
    if remainder == 0 or remainder < min_v or (remainder % step_i) != 0:
        return vol, "full"
    return partial, "partial"


def _pos_int(pos: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        if key not in pos:
            continue
        raw = pos.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _volume_constraints(
    pos: Dict[str, Any], order: Dict[str, Any], broker: Any
) -> Tuple[int, int]:
    step = _pos_int(pos, "volume_step", "step")
    min_v = _pos_int(pos, "min_volume", "min_volume_units")
    if step is None:
        raw = getattr(broker, "volume_step", None)
        try:
            if raw is not None and not callable(raw):
                step = int(float(raw))
        except (TypeError, ValueError):
            step = None
    if min_v is None:
        raw = getattr(broker, "min_volume", None)
        try:
            if raw is not None and not callable(raw):
                min_v = int(float(raw))
        except (TypeError, ValueError):
            min_v = None
    if step is None or min_v is None:
        fallback = 1
        try:
            from factor.broker.ctrader import lots_to_protocol_volume
            from factor.rules.presets import resolve_preset

            preset = resolve_preset(
                str(order.get("symbol") or ""), str(order.get("kind") or "crt")
            )
            fallback = lots_to_protocol_volume(
                0.01, float(preset.get("lot_units") or 100)
            )
        except Exception:
            fallback = 1
        if step is None:
            step = fallback
        if min_v is None:
            min_v = fallback
    if step <= 0:
        step = 1
    if min_v <= 0:
        min_v = step
    return step, min_v


def _persist(order: Dict[str, Any], detail: Dict[str, Any], **fields: Any) -> None:
    from factor.db import repos

    order["detail"] = detail
    order_id = order.get("id")
    if not order_id:
        return
    try:
        repos.update_order(str(order_id), detail=detail, **fields)
    except Exception:
        try:
            repos.update_order(str(order_id), detail=detail)
        except Exception:
            pass


def _position_volume(pos: Dict[str, Any]) -> Optional[int]:
    raw = pos.get("volume")
    try:
        vol = int(float(raw))
    except (TypeError, ValueError):
        return None
    if vol <= 0:
        return None
    return vol


def _fill_price(order: Dict[str, Any], pos: Dict[str, Any]) -> Optional[float]:
    for raw in (pos.get("entry_price"), order.get("entry_price")):
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


def _keep_tp(order: Dict[str, Any], pos: Dict[str, Any], point_size: float) -> Optional[float]:
    for raw in (pos.get("take_profit"), order.get("tp_price")):
        if raw is None or raw == "":
            continue
        try:
            return round_price(float(raw), point_size)
        except (TypeError, ValueError):
            continue
    return None


def _move_sl_to_fill(
    order: Dict[str, Any],
    *,
    broker: Any,
    pos: Dict[str, Any],
    prot: Dict[str, Any],
    detail: Dict[str, Any],
    point_size: float,
    pos_id: int,
) -> Dict[str, Any]:
    order_id = order.get("id")
    fill = _fill_price(order, pos)
    if fill is None:
        return {"order_id": order_id, "action": "skip", "reason": "be_no_fill"}
    sl_r = round_price(fill, point_size)
    tp_keep = _keep_tp(order, pos, point_size)
    current = pos.get("stop_loss")
    if current is None:
        current = order.get("sl_price")
    try:
        current_f = float(current) if current is not None else None
    except (TypeError, ValueError):
        current_f = None
    if current_f is not None and abs(current_f - sl_r) < max(point_size, 1e-9) * 0.4:
        prot["be_sl_set"] = True
        prot["be_sl"] = sl_r
        detail["protect"] = prot
        _persist(order, detail, sl_price=float(sl_r))
        return {
            "order_id": order_id,
            "action": "skip",
            "reason": "sl_already_at_fill",
            "sl": sl_r,
        }
    try:
        broker.amend_position_sl_tp(
            position_id=pos_id,
            stop_loss=sl_r,
            take_profit=tp_keep,
        )
    except Exception as e:
        prot["be_sl_error"] = str(e)
        detail["protect"] = prot
        _persist(order, detail)
        return {
            "order_id": order_id,
            "action": "error",
            "reason": f"be_amend_failed:{e}",
        }
    prot["be_sl_set"] = True
    prot["be_sl"] = sl_r
    detail["protect"] = prot
    _persist(order, detail, sl_price=float(sl_r))
    print(f"CRT SL→fill order={str(order_id)[:8]} pos={pos_id} SL→{sl_r}")
    return {
        "order_id": order_id,
        "action": "sl_to_fill",
        "sl": sl_r,
        "tp": tp_keep,
        "position_id": pos_id,
    }


def process_open_order_for_crt(
    order: Dict[str, Any],
    broker: Any,
    pos: Dict[str, Any],
    mark: Optional[float],
    point_size: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Flatten, or take the TP1 partial once. Never trails the stop."""
    order_id = order.get("id")
    detail = dict(order.get("detail") or {})
    prot = dict(detail.get("protect") or {})
    if not prot.get("enabled"):
        return {"order_id": order_id, "action": "skip", "reason": "protect_off"}
    if str(prot.get("kind") or "") != "crt":
        return {"order_id": order_id, "action": "skip", "reason": "not_crt"}

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    entry_at = _parse_iso(prot.get("entry_at")) or _parse_iso(order.get("created_at"))
    try:
        pos_id = int(pos["position_id"])
    except (TypeError, ValueError, KeyError):
        return {"order_id": order_id, "action": "skip", "reason": "no_position"}
    volume = _position_volume(pos)
    side = str(order.get("side") or pos.get("side") or "BUY")

    if not prot.get("flattened"):
        reason = crt_flatten_due(now, prot, entry_at)
        if reason:
            if volume is None:
                return {
                    "order_id": order_id,
                    "action": "skip",
                    "reason": "flatten_no_volume",
                }
            try:
                broker.close_position(position_id=pos_id, volume=int(volume))
            except Exception as e:
                return {
                    "order_id": order_id,
                    "action": "error",
                    "reason": f"flatten_failed:{e}",
                }
            prot["flattened"] = True
            prot["flatten_reason"] = reason
            prot["flattened_at"] = _iso_z(now)
            detail["protect"] = prot
            _persist(
                order,
                detail,
                status="closed",
                closed_at=_iso_z(now),
                skip_reason=reason,
            )
            print(f"CRT flatten order={str(order_id)[:8]} pos={pos_id} {reason}")
            return {
                "order_id": order_id,
                "action": "flattened",
                "reason": reason,
                "position_id": pos_id,
                "volume": int(volume),
            }

    if prot.get("partial_done") and _metric_bool(prot, "be_after_tp1", False) and not prot.get(
        "be_sl_set"
    ):
        return _move_sl_to_fill(
            order,
            broker=broker,
            pos=pos,
            prot=prot,
            detail=detail,
            point_size=float(point_size or 0.01),
            pos_id=pos_id,
        )
    if prot.get("partial_skipped"):
        return {
            "order_id": order_id,
            "action": "skip",
            "reason": "partial_skipped:min_volume",
        }
    if prot.get("partial_done") or prot.get("flattened"):
        return {"order_id": order_id, "action": "skip", "reason": "partial_done"}

    if mark is None:
        return {"order_id": order_id, "action": "skip", "reason": "spot_unavailable"}

    tp1 = prot.get("tp1_price")
    try:
        tp1_f = float(tp1) if tp1 is not None and tp1 != "" else None
    except (TypeError, ValueError):
        tp1_f = None
    if tp1_f is None or not tp1_reached(side, float(mark), tp1_f):
        return {"order_id": order_id, "action": "skip", "reason": "tp1_not_yet", "mark": mark}

    if volume is None:
        return {"order_id": order_id, "action": "skip", "reason": "partial_no_volume"}

    step, min_v = _volume_constraints(pos, order, broker)
    frac = prot.get("tp1_frac")
    try:
        frac_f = float(frac) if frac is not None else 0.5
    except (TypeError, ValueError):
        frac_f = 0.5
    close_vol, how = crt_partial_volume(volume, frac_f, step, min_v)
    if how == "skip" or close_vol is None:
        prot["partial_skipped"] = "min_volume"
        prot["partial_reason"] = "partial_skipped:min_volume"
        detail["protect"] = prot
        _persist(order, detail)
        print(
            f"CRT partial skip order={str(order_id)[:8]} pos={pos_id} "
            f"vol={volume} step={step} min={min_v}"
        )
        return {
            "order_id": order_id,
            "action": "skip",
            "reason": "partial_skipped:min_volume",
            "position_id": pos_id,
        }

    try:
        broker.close_position(position_id=pos_id, volume=int(close_vol))
    except Exception as e:
        return {
            "order_id": order_id,
            "action": "error",
            "reason": f"partial_failed:{e}",
        }

    prot["partial_done"] = True
    prot["partial_price"] = float(mark)
    prot["partial_at"] = _iso_z(now)
    prot["partial_volume"] = int(close_vol)
    detail["protect"] = prot

    if how == "full":
        prot["partial_full"] = True
        detail["protect"] = prot
        _persist(
            order,
            detail,
            status="closed",
            closed_at=_iso_z(now),
            skip_reason="crt_partial_full",
        )
        print(
            f"CRT partial full order={str(order_id)[:8]} pos={pos_id} vol={close_vol}"
        )
        return {
            "order_id": order_id,
            "action": "closed",
            "reason": "crt_partial_full",
            "position_id": pos_id,
            "volume": int(close_vol),
        }

    _persist(order, detail)
    print(
        f"CRT partial order={str(order_id)[:8]} pos={pos_id} "
        f"vol={close_vol} mark={mark}"
    )
    result: Dict[str, Any] = {
        "order_id": order_id,
        "action": "partial",
        "reason": "tp1",
        "position_id": pos_id,
        "volume": int(close_vol),
        "mark": float(mark),
    }
    # Stop stays at the original SL unless this alert asked for BE after TP1.
    if _metric_bool(prot, "be_after_tp1", False):
        moved = _move_sl_to_fill(
            order,
            broker=broker,
            pos=pos,
            prot=prot,
            detail=detail,
            point_size=float(point_size or 0.01),
            pos_id=pos_id,
        )
        result["sl_action"] = moved.get("action")
        result["sl"] = moved.get("sl")
        result["tp"] = moved.get("tp")
        if moved.get("action") == "error":
            result["sl_error"] = moved.get("reason")
    return result
