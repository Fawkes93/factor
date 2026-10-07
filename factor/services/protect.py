"""
Post-entry management for session alerts (trail + flatten).

Asia breakout, Asia Overnight H, and Dawn Cross send flatten_hour (and
Asia also sends trail distances) on the webhook. Factor stores them on
factor_orders.detail.protect and amends / closes from the position poll.
flatten_hour 0 means no clock flatten (hold for SL/TP, or max_hold if set).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from factor.rules.presets import round_price
from factor.services.breakeven import favorable_distance


def _session_defaults(kind: str) -> tuple[int, float, str]:
    """(flatten_hour, max_hold_hours, timezone) when the webhook omits them.

    Dawn Cross uses flatten 0 (off) unless Reaktor sends an hour. Asia
    keeps the previous 22:00 / 14h Bucharest fallback.
    """
    if str(kind or "").strip() == "dawn_cross":
        return 0, 0.0, "Europe/Chisinau"
    return 22, 14.0, "Europe/Bucharest"


def _metric_number(metrics: Dict[str, Any], key: str, default: float) -> float:
    if key not in metrics:
        return float(default)
    raw = metrics.get(key)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def protect_detail_from_metrics(
    metrics: Optional[Dict[str, Any]],
    *,
    triggered_at: Optional[str] = None,
    kind: str = "asia_breakout",
) -> Dict[str, Any]:
    m = metrics if isinstance(metrics, dict) else {}
    default_hour, default_hold, default_tz = _session_defaults(kind)
    hour = int(_metric_number(m, "flatten_hour", default_hour))
    return {
        "enabled": True,
        "kind": str(kind or "asia_breakout"),
        "trail_arm_pts": float(m.get("trail_arm_pts") or 0),
        "trail_pts": float(m.get("trail_pts") or 0),
        "trail_armed": False,
        "best_price": None,
        "flatten_hour": hour,
        "flatten_tz": str(m.get("flatten_tz") or default_tz),
        "max_hold_hours": _metric_number(m, "max_hold_hours", default_hold),
        "entry_at": triggered_at,
        "flattened": False,
    }


def trail_stop_price(
    *,
    side: str,
    best: float,
    trail_pts: float,
    point_size: float,
) -> float:
    dist = float(trail_pts) * float(point_size)
    side_u = (side or "").upper()
    if side_u in ("BUY", "UP", "LONG"):
        return float(best) - dist
    return float(best) + dist


def should_arm_trail(
    *,
    side: str,
    entry: float,
    mark: float,
    trail_arm_pts: float,
    point_size: float,
) -> bool:
    if trail_arm_pts <= 0 or point_size <= 0:
        return False
    fav = favorable_distance(side=side, entry=entry, mark=mark)
    return fav + 1e-12 >= float(trail_arm_pts) * float(point_size)


def trail_never_loosens(*, side: str, new_sl: float, current_sl: Optional[float]) -> float:
    if current_sl is None:
        return new_sl
    side_u = (side or "").upper()
    if side_u in ("BUY", "UP", "LONG"):
        return max(float(new_sl), float(current_sl))
    return min(float(new_sl), float(current_sl))


def flatten_due(
    now: datetime,
    *,
    entry_at: Optional[datetime],
    flatten_hour: int,
    tz_name: str = "Europe/Bucharest",
    max_hold_hours: float = 14.0,
) -> Optional[str]:
    """Return reason if the position should be flattened, else None.

    Clock flatten is the flatten hour on the entry's local date. A check
    that misses 23:00 still closes after midnight, because the deadline
    stays on that entry date. Hour 0 (or outside 1–23) turns the clock off.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    try:
        tz = ZoneInfo(tz_name or "Europe/Bucharest")
    except Exception:
        tz = ZoneInfo("Europe/Bucharest")
    hour = int(flatten_hour)
    if 1 <= hour <= 23:
        if entry_at is not None:
            anchor = entry_at
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            anchor_local = anchor.astimezone(tz)
            deadline = datetime.combine(
                anchor_local.date(), time(hour, 0), tzinfo=tz
            )
        else:
            local = now.astimezone(tz)
            deadline = datetime.combine(local.date(), time(hour, 0), tzinfo=tz)
        if now >= deadline.astimezone(timezone.utc):
            return f"flatten_hour:{hour:02d}:00 {tz_name}"
    if entry_at is not None and max_hold_hours > 0:
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=timezone.utc)
        hold_end = entry_at.astimezone(timezone.utc) + timedelta(hours=float(max_hold_hours))
        if now >= hold_end:
            return f"max_hold:{max_hold_hours:g}h"
    return None


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def next_trail_sl(
    *,
    side: str,
    entry: float,
    mark: float,
    point_size: float,
    trail_arm_pts: float,
    trail_pts: float,
    current_sl: Optional[float],
    best_price: Optional[float],
    already_armed: bool,
) -> tuple[bool, Optional[float], float, bool, str]:
    """
    Returns (amend, new_sl, best, armed, reason).
    best is the most favorable mark seen (including this tick).
    """
    side_u = (side or "").upper()
    is_buy = side_u in ("BUY", "UP", "LONG")
    if best_price is None:
        best = float(mark)
    else:
        best = max(float(best_price), float(mark)) if is_buy else min(float(best_price), float(mark))

    armed = bool(already_armed)
    if not armed:
        if should_arm_trail(
            side=side,
            entry=entry,
            mark=mark,
            trail_arm_pts=trail_arm_pts,
            point_size=point_size,
        ):
            armed = True
        else:
            return False, None, best, False, "trail_not_armed"

    if trail_pts <= 0 or point_size <= 0:
        return False, None, best, armed, "trail_pts_off"

    raw = trail_stop_price(
        side=side, best=best, trail_pts=trail_pts, point_size=point_size
    )
    new_sl = trail_never_loosens(side=side, new_sl=raw, current_sl=current_sl)
    new_sl = round_price(new_sl, point_size)
    if current_sl is not None and abs(new_sl - float(current_sl)) < (point_size * 0.4):
        return False, None, best, armed, "trail_unchanged"
    # Must still be better than current
    if current_sl is not None:
        if is_buy and new_sl <= float(current_sl) + 1e-12:
            return False, None, best, armed, "trail_unchanged"
        if not is_buy and new_sl >= float(current_sl) - 1e-12:
            return False, None, best, armed, "trail_unchanged"
    return True, new_sl, best, True, "trail_amend"


def process_open_order_for_protect(
    order: Dict[str, Any],
    *,
    broker: Any,
    pos: Dict[str, Any],
    mark: Optional[float],
    point_size: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Flatten or trail one open session order.
    Caller has already matched `pos`. `mark` may be missing; flatten
    still runs, and the trail waits for a quote.
    """
    from factor.db import repos

    order_id = order.get("id")
    detail = dict(order.get("detail") or {})
    prot = dict(detail.get("protect") or {})
    if not prot.get("enabled"):
        return {"order_id": order_id, "action": "skip", "reason": "protect_off"}

    now = now or datetime.now(timezone.utc)
    entry_at = _parse_iso(prot.get("entry_at")) or _parse_iso(order.get("created_at"))
    raw_hour = prot.get("flatten_hour")
    flatten_hour = 22 if raw_hour is None or raw_hour == "" else int(raw_hour)
    raw_hold = prot.get("max_hold_hours")
    max_hold = 14.0 if raw_hold is None or raw_hold == "" else float(raw_hold)
    reason = flatten_due(
        now,
        entry_at=entry_at,
        flatten_hour=flatten_hour,
        tz_name=str(prot.get("flatten_tz") or "Europe/Bucharest"),
        max_hold_hours=max_hold,
    )
    pos_id = int(pos["position_id"])
    side = str(order.get("side") or pos.get("side") or "BUY")
    volume = pos.get("volume")

    if reason and not prot.get("flattened"):
        try:
            vol = int(volume) if volume is not None else None
            if vol is None or vol <= 0:
                return {
                    "order_id": order_id,
                    "action": "skip",
                    "reason": "flatten_no_volume",
                }
            broker.close_position(position_id=pos_id, volume=vol)
        except Exception as e:
            return {
                "order_id": order_id,
                "action": "error",
                "reason": f"flatten_failed:{e}",
            }
        prot["flattened"] = True
        prot["flatten_reason"] = reason
        prot["flattened_at"] = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        detail["protect"] = prot
        try:
            repos.update_order(
                str(order_id),
                detail=detail,
                status="closed",
                closed_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                skip_reason=reason,
            )
        except Exception:
            try:
                repos.update_order(str(order_id), detail=detail)
            except Exception:
                pass
        print(
            f"🌅 flatten order={str(order_id)[:8]} pos={pos_id} {reason}"
        )
        return {
            "order_id": order_id,
            "action": "flattened",
            "reason": reason,
            "position_id": pos_id,
        }

    if mark is None:
        return {"order_id": order_id, "action": "skip", "reason": "spot_unavailable"}

    entry = pos.get("entry_price") or order.get("entry_price")
    try:
        entry_f = float(entry)
    except (TypeError, ValueError):
        return {"order_id": order_id, "action": "skip", "reason": "no_entry"}

    current_sl = pos.get("stop_loss")
    if current_sl is None:
        current_sl = order.get("sl_price")
    try:
        current_sl_f = float(current_sl) if current_sl is not None else None
    except (TypeError, ValueError):
        current_sl_f = None

    amend, new_sl, best, armed, trail_reason = next_trail_sl(
        side=side,
        entry=entry_f,
        mark=mark,
        point_size=point_size,
        trail_arm_pts=float(prot.get("trail_arm_pts") or 0),
        trail_pts=float(prot.get("trail_pts") or 0),
        current_sl=current_sl_f,
        best_price=prot.get("best_price"),
        already_armed=bool(prot.get("trail_armed")),
    )
    prot["best_price"] = best
    prot["trail_armed"] = armed
    prot["last_reason"] = trail_reason
    prot["last_mark"] = mark
    if not amend or new_sl is None:
        detail["protect"] = prot
        try:
            repos.update_order(str(order_id), detail=detail)
        except Exception:
            pass
        return {
            "order_id": order_id,
            "action": "skip",
            "reason": trail_reason,
            "mark": mark,
        }

    tp_keep = pos.get("take_profit")
    if tp_keep is None:
        tp_keep = order.get("tp_price")
    try:
        tp_keep_f = round_price(float(tp_keep), point_size) if tp_keep is not None else None
    except (TypeError, ValueError):
        tp_keep_f = None
    sl_r = round_price(float(new_sl), point_size)
    try:
        broker.amend_position_sl_tp(
            position_id=pos_id,
            stop_loss=sl_r,
            take_profit=tp_keep_f,
        )
    except Exception as e:
        return {
            "order_id": order_id,
            "action": "error",
            "reason": f"trail_amend_failed:{e}",
        }
    prot["trail_sl"] = sl_r
    detail["protect"] = prot
    try:
        repos.update_order(str(order_id), sl_price=float(sl_r), detail=detail)
    except Exception:
        pass
    print(
        f"🌅 trail order={str(order_id)[:8]} pos={pos_id} SL→{sl_r} mark={mark}"
    )
    return {
        "order_id": order_id,
        "action": "trailed",
        "sl": sl_r,
        "mark": mark,
        "position_id": pos_id,
        "reason": trail_reason,
    }
