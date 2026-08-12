"""
Move stop-loss to breakeven when price reaches a fraction of the TP distance.

Example: entry 2000, TP +1000 pts, fraction 0.5 → at +500 pts favorable,
set SL to entry (+ optional buffer points).

Multi-user: background poll over open factor_orders + cTrader amend SLTP.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from factor import config
from factor.broker.ctrader import broker_from_connection
from factor.crypto.tokens import decrypt_token
from factor.db import repos
from factor.db.client import supabase_configured
from factor.rules.presets import resolve_preset
from factor.services.outcome import RESULT_UNKNOWN

_be_stop = threading.Event()
_be_thread: Optional[threading.Thread] = None
_last_be: Dict[str, Any] = {"at": None, "checked": 0, "moved": 0, "error": None}


def last_breakeven_status() -> Dict[str, Any]:
    return dict(_last_be)


# ---------------------------------------------------------------------------
# Pure logic (unit-tested)
# ---------------------------------------------------------------------------


def favorable_distance(*, side: str, entry: float, mark: float) -> float:
    """How far price has moved in the trade's favor (price units, not points)."""
    side_u = (side or "").upper()
    if side_u in ("BUY", "UP", "LONG"):
        return float(mark) - float(entry)
    if side_u in ("SELL", "DOWN", "SHORT"):
        return float(entry) - float(mark)
    return 0.0


def tp_distance_price(
    *,
    side: str,
    entry: float,
    tp_price: Optional[float] = None,
    tp_points: Optional[float] = None,
    point_size: Optional[float] = None,
) -> float:
    """Absolute price distance from entry to TP."""
    if tp_price is not None and float(tp_price) > 0:
        return abs(float(tp_price) - float(entry))
    pts = float(tp_points or 0)
    psz = float(point_size or 0)
    if pts > 0 and psz > 0:
        return pts * psz
    return 0.0


def breakeven_sl_price(
    *,
    side: str,
    entry: float,
    buffer_points: float = 0.0,
    point_size: float = 0.01,
) -> float:
    """
    SL at entry, nudged slightly into profit by buffer_points so spread
    is less likely to stop you out at pure $0.
    """
    side_u = (side or "").upper()
    buf = max(0.0, float(buffer_points)) * max(0.0, float(point_size))
    entry_f = float(entry)
    if side_u in ("BUY", "UP", "LONG"):
        return entry_f + buf
    if side_u in ("SELL", "DOWN", "SHORT"):
        return entry_f - buf
    return entry_f


def sl_is_worse_than_be(*, side: str, current_sl: Optional[float], be_sl: float) -> bool:
    """True if current SL is missing or still worse than the proposed BE SL."""
    if current_sl is None:
        return True
    side_u = (side or "").upper()
    cur = float(current_sl)
    be = float(be_sl)
    # Tiny float tolerance
    eps = 1e-9
    if side_u in ("BUY", "UP", "LONG"):
        return cur < be - eps
    if side_u in ("SELL", "DOWN", "SHORT"):
        return cur > be + eps
    return True


def should_move_to_breakeven(
    *,
    side: str,
    entry: float,
    mark: float,
    tp_price: Optional[float] = None,
    tp_points: Optional[float] = None,
    point_size: Optional[float] = None,
    fraction: float = 0.5,
    already_applied: bool = False,
    current_sl: Optional[float] = None,
    buffer_points: float = 0.0,
) -> Tuple[bool, Optional[float], str]:
    """
    Returns (should_move, new_sl_price, reason).
    """
    if already_applied:
        return False, None, "already_applied"
    if entry is None or mark is None:
        return False, None, "missing_prices"
    try:
        entry_f = float(entry)
        mark_f = float(mark)
    except (TypeError, ValueError):
        return False, None, "bad_prices"

    frac = float(fraction)
    if frac <= 0 or frac > 1.0:
        return False, None, f"bad_fraction:{frac}"

    psz = float(point_size or 0.01)
    tp_dist = tp_distance_price(
        side=side,
        entry=entry_f,
        tp_price=tp_price,
        tp_points=tp_points,
        point_size=psz,
    )
    if tp_dist <= 0:
        return False, None, "no_tp_distance"

    fav = favorable_distance(side=side, entry=entry_f, mark=mark_f)
    trigger = tp_dist * frac
    if fav + 1e-12 < trigger:
        return False, None, f"not_yet fav={fav:.5f} need={trigger:.5f}"

    be_sl = breakeven_sl_price(
        side=side, entry=entry_f, buffer_points=buffer_points, point_size=psz
    )
    if not sl_is_worse_than_be(side=side, current_sl=current_sl, be_sl=be_sl):
        return False, None, "sl_already_at_or_better_than_be"

    return True, be_sl, f"trigger fav={fav:.5f} >= {trigger:.5f}"


def mark_price_for_side(*, side: str, bid: float, ask: float) -> float:
    """Conservative exit-side mark: BUY uses bid, SELL uses ask."""
    side_u = (side or "").upper()
    if side_u in ("BUY", "UP", "LONG"):
        return float(bid)
    if side_u in ("SELL", "DOWN", "SHORT"):
        return float(ask)
    return (float(bid) + float(ask)) / 2.0


# ---------------------------------------------------------------------------
# Order helpers
# ---------------------------------------------------------------------------


def _order_be_meta(order: Dict[str, Any]) -> Dict[str, Any]:
    detail = order.get("detail") if isinstance(order.get("detail"), dict) else {}
    be = detail.get("breakeven") if isinstance(detail.get("breakeven"), dict) else {}
    plan = {}
    broker_detail = detail.get("broker_detail") if isinstance(detail.get("broker_detail"), dict) else {}
    if isinstance(broker_detail.get("plan"), dict):
        plan = broker_detail["plan"]
    default_frac = float(getattr(config, "BREAKEVEN_FRACTION", 0.5) or 0.5)
    return {
        "enabled": bool(be.get("enabled", True)),
        "applied": bool(be.get("applied", False)),
        "fraction": clamp_breakeven_fraction(be.get("fraction"), default_frac),
        "buffer_points": float(
            be.get("buffer_points")
            if be.get("buffer_points") is not None
            else getattr(config, "BREAKEVEN_BUFFER_POINTS", 0) or 0
        ),
        "tp_points": be.get("tp_points") or plan.get("tp_points"),
        "sl_points": be.get("sl_points") or plan.get("sl_points"),
        "point_size": be.get("point_size") or plan.get("point_size"),
        "plan": plan,
    }


def _resolve_point_size(order: Dict[str, Any], meta: Dict[str, Any]) -> float:
    if meta.get("point_size"):
        try:
            return float(meta["point_size"])
        except (TypeError, ValueError):
            pass
    try:
        preset = resolve_preset(str(order.get("symbol") or ""), str(order.get("kind") or "candle"))
        return float(preset.get("point_size") or 0.01)
    except Exception:
        return 0.01


def _stored_position_id(order: Dict[str, Any]) -> Optional[int]:
    """Position id Factor recorded for this order (no fuzzy match)."""
    pid = order.get("broker_position_id")
    if pid is not None and str(pid).strip() and not str(pid).startswith("dry-"):
        try:
            return int(pid)
        except (TypeError, ValueError):
            pass
    detail = order.get("detail") if isinstance(order.get("detail"), dict) else {}
    be = detail.get("breakeven") if isinstance(detail.get("breakeven"), dict) else {}
    if be.get("position_id") is not None:
        try:
            return int(be["position_id"])
        except (TypeError, ValueError):
            pass
    broker_detail = detail.get("broker_detail") if isinstance(detail.get("broker_detail"), dict) else {}
    for key in ("position_id", "positionId"):
        if broker_detail.get(key) is not None:
            try:
                return int(broker_detail[key])
            except (TypeError, ValueError):
                pass
    return None


def _open_position_id_set(positions: List[Dict[str, Any]]) -> set:
    ids: set = set()
    for p in positions or []:
        try:
            ids.add(int(p["position_id"]))
        except (TypeError, ValueError, KeyError):
            continue
    return ids


def _extract_position_id(order: Dict[str, Any], positions: List[Dict[str, Any]]) -> Optional[int]:
    stored = _stored_position_id(order)
    if stored is not None:
        return stored
    # Match by side among open positions (best effort when only one open)
    side = str(order.get("side") or "").upper()
    want_buy = side in ("BUY", "UP", "LONG")
    candidates = []
    for p in positions:
        pside = str(p.get("side") or "").upper()
        is_buy = pside in ("BUY", "1", "LONG")
        if want_buy == is_buy or not pside:
            candidates.append(p)
    if len(candidates) == 1:
        try:
            return int(candidates[0]["position_id"])
        except (TypeError, ValueError, KeyError):
            return None
    if len(positions) == 1:
        try:
            return int(positions[0]["position_id"])
        except (TypeError, ValueError, KeyError):
            return None
    return None


def _order_created_ms(order: Dict[str, Any]) -> Optional[int]:
    raw = order.get("created_at")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _lookup_close_outcome(
    order: Dict[str, Any],
    *,
    broker: Any = None,
    position_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch won/lost from cTrader deals; fall back to unknown."""
    pid = position_id if position_id is not None else _stored_position_id(order)
    if pid is None or broker is None:
        return {"result": RESULT_UNKNOWN, "close_deals": 0, "reason": "no_position_or_broker"}
    try:
        if hasattr(broker, "position_close_outcome"):
            return broker.position_close_outcome(
                int(pid),
                from_timestamp_ms=_order_created_ms(order),
            )
    except Exception as e:
        print(f"⚠️  close outcome lookup failed pos={pid}: {e}")
        return {"result": RESULT_UNKNOWN, "close_deals": 0, "reason": f"lookup_failed:{e}"}
    return {"result": RESULT_UNKNOWN, "close_deals": 0}


def _mark_order_closed_local(
    order: Dict[str, Any],
    reason: str,
    *,
    broker: Any = None,
    outcome: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist status=closed (+ won/lost result) for a Factor order gone from cTrader."""
    order_id = order.get("id")
    if outcome is None:
        outcome = _lookup_close_outcome(order, broker=broker)
    result = str(outcome.get("result") or RESULT_UNKNOWN)

    detail = dict(order.get("detail") or {}) if isinstance(order.get("detail"), dict) else {}
    detail["outcome"] = {
        "result": result,
        "net_pnl": outcome.get("net_pnl"),
        "gross_profit": outcome.get("gross_profit"),
        "swap": outcome.get("swap"),
        "commission": outcome.get("commission"),
        "close_price": outcome.get("close_price"),
        "close_deals": outcome.get("close_deals"),
        "position_id": outcome.get("position_id") or _stored_position_id(order),
        "closed_reason": reason,
    }

    fields: Dict[str, Any] = {
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "skip_reason": order.get("skip_reason") or reason,
        "detail": detail,
        "result": result,
    }
    try:
        try:
            repos.update_order(str(order_id), **fields)
        except Exception as col_err:
            # Column may be missing before sql/003_order_result.sql
            if "result" in str(col_err).lower() or "column" in str(col_err).lower():
                fields.pop("result", None)
                repos.update_order(str(order_id), **fields)
            else:
                raise
        tag = {"won": "✅", "lost": "❌", "breakeven": "➖"}.get(result, "📕")
        pnl = outcome.get("net_pnl")
        pnl_s = f" pnl={pnl}" if pnl is not None else ""
        print(
            f"{tag} Closed local order={str(order_id)[:8]} "
            f"{order.get('kind')} {order.get('symbol')} "
            f"result={result}{pnl_s}: {reason}"
        )
    except Exception as e:
        print(f"⚠️  mark closed failed order={order_id}: {e}")
        return {
            "order_id": order_id,
            "action": "error",
            "reason": f"mark_closed_failed:{e}",
        }
    return {
        "order_id": order_id,
        "action": "closed_local",
        "reason": reason,
        "result": result,
        "outcome": outcome,
    }


def reconcile_order_vs_positions(
    order: Dict[str, Any],
    positions: List[Dict[str, Any]],
    *,
    dry: bool = False,
    broker: Any = None,
) -> Optional[Dict[str, Any]]:
    """
    If the Factor open order is no longer on cTrader, mark it closed.

    Returns a result dict when reconciled (closed / error), else None if still open
    or not determinable.
    """
    if dry:
        # Dry-run never has live positions — do not wipe local open rows
        return None

    open_ids = _open_position_id_set(positions)
    stored = _stored_position_id(order)

    if not positions:
        return _mark_order_closed_local(order, "position_gone", broker=broker)

    if stored is not None and stored not in open_ids:
        return _mark_order_closed_local(
            order, f"position_gone:{stored}", broker=broker
        )

    return None


def clamp_breakeven_fraction(raw: Any, default: float = 0.5) -> float:
    """Fraction of TP distance that triggers BE; clamped to (0, 1]."""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        f = float(default)
    if f <= 0 or f > 1.0:
        f = float(default) if 0 < float(default) <= 1.0 else 0.5
    return f


def build_breakeven_order_detail(
    *,
    plan: Any,
    pref: Optional[Dict[str, Any]] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Snapshot BE settings into order.detail at place time."""
    pref = pref or {}
    if enabled is None:
        enabled = bool(getattr(config, "BREAKEVEN_ENABLED", True)) and bool(
            pref.get("breakeven_half_tp", True)
        )
    default_frac = float(getattr(config, "BREAKEVEN_FRACTION", 0.5) or 0.5)
    # Per-pref fraction wins; fall back to server default (legacy 0.5)
    if pref.get("breakeven_fraction") is not None:
        frac = clamp_breakeven_fraction(pref.get("breakeven_fraction"), default_frac)
    else:
        frac = clamp_breakeven_fraction(default_frac, 0.5)
    return {
        "enabled": bool(enabled),
        "applied": False,
        "fraction": frac,
        "buffer_points": float(getattr(config, "BREAKEVEN_BUFFER_POINTS", 0) or 0),
        "tp_points": getattr(plan, "tp_points", None),
        "sl_points": getattr(plan, "sl_points", None),
        "point_size": getattr(plan, "point_size", None),
    }


# ---------------------------------------------------------------------------
# Runtime cycle
# ---------------------------------------------------------------------------


def _sane_absolute_sl(
    *,
    side: str,
    entry: float,
    candidate: Optional[float],
    fallback: Optional[float] = None,
) -> Optional[float]:
    """
    Prefer absolute SL prices near the market. Drop relative-protocol leftovers
    (e.g. 1_000_000) that would make BUY look already better-than-BE.
    """
    for raw in (candidate, fallback):
        if raw is None:
            continue
        try:
            sl = float(raw)
        except (TypeError, ValueError):
            continue
        if sl <= 0:
            continue
        # Absolute XAU ~4k, FX ~1; relative protocol is price_dist * 100_000
        if sl > max(abs(entry) * 5.0, 1e5):
            continue
        return sl
    return None


def _be_reason_key(reason: str) -> str:
    """Collapse noisy reasons (not_yet fav=…) so we don't write every poll."""
    r = (reason or "").strip()
    if r.startswith("not_yet"):
        return "not_yet"
    if r.startswith("spot_failed"):
        return "spot_failed"
    if r.startswith("amend_failed"):
        return "amend_failed"
    if ":" in r:
        return r.split(":", 1)[0]
    return r


def _persist_be_check(
    order: Dict[str, Any],
    *,
    reason: str,
    mark: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write last BE poll diagnostics into order.detail (no schema change)."""
    order_id = order.get("id")
    if not order_id:
        return
    detail = dict(order.get("detail") or {}) if isinstance(order.get("detail"), dict) else {}
    be = dict(detail.get("breakeven") or {}) if isinstance(detail.get("breakeven"), dict) else {}
    key = _be_reason_key(reason)
    prev_key = str(be.get("last_reason_key") or _be_reason_key(str(be.get("last_reason") or "")))
    force = bool((extra or {}).get("force"))
    # Avoid spamming Supabase when still the same skip class (e.g. not_yet)
    if prev_key == key and not force:
        return
    be["last_reason"] = reason
    be["last_reason_key"] = key
    be["last_check_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if mark is not None:
        be["last_mark"] = float(mark)
    if extra:
        for k, v in extra.items():
            if k == "force":
                continue
            be[k] = v
    detail["breakeven"] = be
    try:
        repos.update_order(str(order_id), detail=detail)
        # Keep in-memory order in sync for later steps in same cycle
        order["detail"] = detail
    except Exception as e:
        print(f"⚠️  BE last_check persist failed order={order_id}: {e}")


def process_open_order_for_be(
    order: Dict[str, Any],
    *,
    broker: Any,
    positions: Optional[List[Dict[str, Any]]] = None,
    be_enabled_global: bool = True,
) -> Dict[str, Any]:
    """
    Reconcile one open order vs cTrader, then amend SL if BE trigger hit.

    Always marks factor_orders closed when the cTrader position is gone
    (including after BE already applied). BE amend only when enabled and not applied.

    broker must implement list_open_positions_info / get_spot / amend_position_sl_tp
    """
    order_id = order.get("id")
    meta = _order_be_meta(order)

    if positions is None:
        try:
            positions = broker.list_open_positions_info()
        except Exception as e:
            return {"order_id": order_id, "action": "skip", "reason": f"list_pos_failed:{e}"}

    dry = bool(getattr(broker, "_dry_run", False))
    if dry and not positions:
        return {"order_id": order_id, "action": "skip", "reason": "dry_run_no_positions"}

    # Reconcile first — critical for max_concurrent (DB open rows)
    closed = reconcile_order_vs_positions(
        order, positions or [], dry=dry, broker=broker
    )
    if closed is not None:
        return closed

    if not be_enabled_global or not meta["enabled"]:
        return {"order_id": order_id, "action": "skip", "reason": "be_disabled"}
    if meta["applied"]:
        return {"order_id": order_id, "action": "skip", "reason": "already_applied"}

    pos_id = _extract_position_id(order, positions)
    pos = None
    if pos_id is not None:
        for p in positions:
            try:
                if int(p.get("position_id")) == int(pos_id):
                    pos = p
                    break
            except (TypeError, ValueError):
                continue
    if pos is None and len(positions) == 1 and _stored_position_id(order) is None:
        pos = positions[0]
        pos_id = int(pos["position_id"])

    if pos is None:
        # Stored id already handled by reconcile; ambiguous match only
        return {"order_id": order_id, "action": "skip", "reason": "position_not_matched"}

    side = str(order.get("side") or pos.get("side") or "BUY")
    # Prefer live position VWAP/entry when present (alert price can differ from fill)
    entry = None
    pos_entry = pos.get("entry_price")
    try:
        if pos_entry is not None and float(pos_entry) > 0:
            entry = float(pos_entry)
    except (TypeError, ValueError):
        entry = None
    if entry is None:
        entry = order.get("entry_price")
    try:
        entry_f = float(entry)
    except (TypeError, ValueError):
        return {"order_id": order_id, "action": "skip", "reason": "no_entry"}

    # Spot for mark
    broker_symbol = pos.get("broker_symbol")
    if not broker_symbol:
        # fall back to plan / symbol without slash
        plan = meta.get("plan") or {}
        broker_symbol = plan.get("broker_symbol") or str(order.get("symbol") or "").replace("/", "")

    try:
        spot = broker.get_spot(str(broker_symbol))
        mark = mark_price_for_side(
            side=side, bid=float(spot["bid"]), ask=float(spot["ask"])
        )
    except Exception as e:
        reason = f"spot_failed:{e}"
        _persist_be_check(order, reason=reason)
        return {"order_id": order_id, "action": "skip", "reason": reason}

    psz = _resolve_point_size(order, meta)
    order_sl = None
    try:
        order_sl = float(order["sl_price"]) if order.get("sl_price") is not None else None
    except (TypeError, ValueError):
        order_sl = None
    current_sl = _sane_absolute_sl(
        side=side,
        entry=entry_f,
        candidate=pos.get("stop_loss"),
        fallback=order_sl,
    )

    tp_price = order.get("tp_price")
    if tp_price is None:
        tp_price = pos.get("take_profit")

    ok, be_sl, reason = should_move_to_breakeven(
        side=side,
        entry=entry_f,
        mark=mark,
        tp_price=float(tp_price) if tp_price is not None else None,
        tp_points=float(meta["tp_points"]) if meta.get("tp_points") is not None else None,
        point_size=psz,
        fraction=meta["fraction"],
        already_applied=False,
        current_sl=float(current_sl) if current_sl is not None else None,
        buffer_points=meta["buffer_points"],
    )
    if not ok or be_sl is None:
        fav = favorable_distance(side=side, entry=entry_f, mark=mark)
        tp_dist = tp_distance_price(
            side=side,
            entry=entry_f,
            tp_price=float(tp_price) if tp_price is not None else None,
            tp_points=float(meta["tp_points"]) if meta.get("tp_points") is not None else None,
            point_size=psz,
        )
        _persist_be_check(
            order,
            reason=reason,
            mark=mark,
            extra={
                "last_fav": round(fav, 5),
                "last_trigger": round(tp_dist * float(meta["fraction"]), 5),
                "entry_used": entry_f,
            },
        )
        return {"order_id": order_id, "action": "skip", "reason": reason, "mark": mark}

    # Keep existing TP when amending
    tp_keep = None
    if tp_price is not None:
        try:
            tp_keep = float(tp_price)
        except (TypeError, ValueError):
            tp_keep = None
    if tp_keep is None and pos.get("take_profit") is not None:
        try:
            tp_keep = float(pos["take_profit"])
        except (TypeError, ValueError):
            tp_keep = None

    try:
        amend = broker.amend_position_sl_tp(
            position_id=int(pos_id),
            stop_loss=float(be_sl),
            take_profit=tp_keep,
        )
    except Exception as e:
        print(f"❌ BE amend failed order={order_id} pos={pos_id}: {e}")
        _persist_be_check(
            order,
            reason=f"amend_failed:{e}",
            mark=mark,
            extra={"force": True, "be_sl_attempt": float(be_sl)},
        )
        return {"order_id": order_id, "action": "error", "reason": str(e)}

    # Persist
    detail = dict(order.get("detail") or {})
    be = dict(detail.get("breakeven") or {})
    be.update(
        {
            "enabled": True,
            "applied": True,
            "applied_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "be_sl": float(be_sl),
            "mark_at_apply": mark,
            "position_id": int(pos_id),
            "reason": reason,
            "last_reason": reason,
            "last_check_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_mark": mark,
            "fraction": meta["fraction"],
            "buffer_points": meta["buffer_points"],
            "tp_points": meta.get("tp_points"),
            "point_size": psz,
            "entry_used": entry_f,
        }
    )
    detail["breakeven"] = be
    try:
        repos.update_order(
            str(order_id),
            sl_price=float(be_sl),
            broker_position_id=str(pos_id),
            detail=detail,
        )
    except Exception as e:
        print(f"⚠️  BE update_order failed order={order_id}: {e}")

    print(
        f"🛡️  BE applied order={str(order_id)[:8]} pos={pos_id} "
        f"{side} {order.get('symbol')} SL→{be_sl} mark={mark} ({reason})"
    )
    return {
        "order_id": order_id,
        "action": "moved",
        "be_sl": be_sl,
        "mark": mark,
        "position_id": pos_id,
        "reason": reason,
        "amend": amend,
    }


def run_breakeven_cycle() -> Dict[str, Any]:
    """
    One pass over all open multi-user orders:
    1) Reconcile factor_orders vs cTrader (mark closed when position gone)
    2) Optionally move SL to breakeven when half-TP is hit
    """
    out: Dict[str, Any] = {
        "checked": 0,
        "moved": 0,
        "closed": 0,
        "errors": 0,
        "results": [],
    }
    be_global = bool(getattr(config, "BREAKEVEN_ENABLED", True))
    if not supabase_configured():
        out["reason"] = "supabase_not_configured"
        return out
    if not config.FACTOR_TOKEN_ENC_KEY:
        out["reason"] = "token_enc_key_missing"
        return out

    try:
        open_orders = repos.list_open_orders_all(limit=100)
    except Exception as e:
        out["error"] = str(e)
        print(f"⚠️  BE list_open_orders_all failed: {e}")
        return out

    # Group by user_id — include ALL open rows (even BE already applied)
    by_user: Dict[str, List[Dict[str, Any]]] = {}
    for o in open_orders:
        uid = str(o.get("user_id") or "")
        if not uid:
            continue
        by_user.setdefault(uid, []).append(o)

    for uid, orders in by_user.items():
        conn = None
        try:
            conn = repos.get_connection(uid)
        except Exception as e:
            print(f"⚠️  BE get_connection {uid[:8]}: {e}")
            continue
        if not conn:
            continue
        try:
            access = decrypt_token(
                conn["access_token_enc"], config.FACTOR_TOKEN_ENC_KEY
            )
            broker = broker_from_connection(
                access_token=access,
                account_id=int(conn["account_id"]),
                host=str(conn.get("host") or "demo.ctraderapi.com"),
                dry_run=config.CTRADER_DRY_RUN,
            )
        except Exception as e:
            print(f"⚠️  BE broker build {uid[:8]}: {e}")
            out["errors"] += 1
            continue

        # One WebSocket per user per cycle: list + spot + amend
        try:
            with broker.session() as session:
                try:
                    positions = session.list_open_positions_info()
                except Exception as e:
                    print(f"⚠️  BE list positions {uid[:8]}: {e}")
                    out["errors"] += 1
                    continue

                for order in orders:
                    out["checked"] += 1
                    try:
                        r = process_open_order_for_be(
                            order,
                            broker=session,
                            positions=positions,
                            be_enabled_global=be_global,
                        )
                        out["results"].append(r)
                        if r.get("action") == "moved":
                            out["moved"] += 1
                        elif r.get("action") == "closed_local":
                            out["closed"] += 1
                        elif r.get("action") == "error":
                            out["errors"] += 1
                    except Exception as e:
                        out["errors"] += 1
                        print(f"⚠️  BE process order failed: {e}")
        except Exception as e:
            print(f"⚠️  BE session run {uid[:8]}: {e}")
            out["errors"] += 1

    return out


def _be_loop() -> None:
    interval = max(5.0, float(getattr(config, "BREAKEVEN_POLL_SEC", 15) or 15))
    be_on = bool(getattr(config, "BREAKEVEN_ENABLED", True))
    print(
        f"🛡️  Position reconcile + BE poll every {interval:g}s "
        f"(be_enabled={be_on} fraction={getattr(config, 'BREAKEVEN_FRACTION', 0.5)})"
    )
    while not _be_stop.wait(interval):
        try:
            stats = run_breakeven_cycle()
            _last_be.update(
                {
                    "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "checked": stats.get("checked", 0),
                    "moved": stats.get("moved", 0),
                    "closed": stats.get("closed", 0),
                    "error": stats.get("error") or (
                        f"{stats.get('errors')} errors" if stats.get("errors") else None
                    ),
                    "reason": stats.get("reason"),
                }
            )
            if stats.get("moved") or stats.get("closed"):
                print(
                    f"🛡️  BE cycle moved={stats.get('moved', 0)} "
                    f"closed={stats.get('closed', 0)} checked={stats.get('checked', 0)}"
                )
        except Exception as e:
            _last_be["error"] = str(e)
            print(f"⚠️  BE cycle error: {e}")


def start_breakeven_loop() -> None:
    """Start background poll: always reconciles open orders; BE amend if enabled."""
    global _be_thread
    if not supabase_configured():
        print("ℹ️  Position/BE poll skipped — Supabase not configured")
        return
    if not getattr(config, "BREAKEVEN_ENABLED", True):
        print("ℹ️  BREAKEVEN_ENABLED=false — poll still runs for position reconcile only")
    if _be_thread and _be_thread.is_alive():
        return
    _be_stop.clear()
    _be_thread = threading.Thread(
        target=_be_loop, name="breakeven-poll", daemon=True
    )
    _be_thread.start()


def stop_breakeven_loop() -> None:
    _be_stop.set()
