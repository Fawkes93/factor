"""
Repository helpers for Factor multi-user tables.

All table names are factor_* (shared Supabase-safe).
Uses service-role Supabase client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from factor.db.client import get_supabase

T_USERS = "factor_users"
T_CONN = "factor_ctrader_connections"
T_PREFS = "factor_user_alert_prefs"
T_ORDERS = "factor_orders"
T_PROCESSED = "factor_processed_alerts"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def upsert_user_by_ctid(
    *,
    ctrader_ctid: Optional[str],
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    sb = get_supabase()
    if ctrader_ctid:
        existing = (
            sb.table(T_USERS)
            .select("*")
            .eq("ctrader_ctid", ctrader_ctid)
            .limit(1)
            .execute()
        )
        if existing.data:
            row = existing.data[0]
            if display_name and display_name != row.get("display_name"):
                upd = (
                    sb.table(T_USERS)
                    .update({"display_name": display_name})
                    .eq("id", row["id"])
                    .execute()
                )
                return (upd.data or [row])[0]
            return row

    payload: Dict[str, Any] = {
        "display_name": display_name,
    }
    if ctrader_ctid:
        payload["ctrader_ctid"] = ctrader_ctid
    ins = sb.table(T_USERS).insert(payload).execute()
    if not ins.data:
        raise RuntimeError("failed to insert factor_users row")
    return ins.data[0]


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    sb = get_supabase()
    res = sb.table(T_USERS).select("*").eq("id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def set_kill_switch(user_id: str, value: bool) -> Dict[str, Any]:
    sb = get_supabase()
    res = (
        sb.table(T_USERS)
        .update({"kill_switch": value, "trading_enabled": not value})
        .eq("id", user_id)
        .execute()
    )
    if not res.data:
        raise RuntimeError("set_kill_switch failed")
    return res.data[0]


def set_trading_enabled(user_id: str, value: bool) -> Dict[str, Any]:
    sb = get_supabase()
    res = (
        sb.table(T_USERS)
        .update({"trading_enabled": value})
        .eq("id", user_id)
        .execute()
    )
    if not res.data:
        raise RuntimeError("set_trading_enabled failed")
    return res.data[0]


# ---------------------------------------------------------------------------
# cTrader connections
# ---------------------------------------------------------------------------


def upsert_ctrader_connection(
    *,
    user_id: str,
    account_id: int,
    host: str,
    access_token_enc: str,
    refresh_token_enc: str,
    access_expires_at: Optional[str] = None,
    scopes: Optional[str] = None,
    raw_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sb = get_supabase()
    payload = {
        "user_id": user_id,
        "account_id": int(account_id),
        "host": host,
        "access_token_enc": access_token_enc,
        "refresh_token_enc": refresh_token_enc,
        "access_expires_at": access_expires_at,
        "scopes": scopes,
        "raw_meta": raw_meta or {},
    }
    # upsert on user_id unique
    res = (
        sb.table(T_CONN)
        .upsert(payload, on_conflict="user_id")
        .execute()
    )
    if not res.data:
        raise RuntimeError("upsert_ctrader_connection failed")
    return res.data[0]


def get_connection(user_id: str) -> Optional[Dict[str, Any]]:
    sb = get_supabase()
    res = sb.table(T_CONN).select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def delete_connection(user_id: str) -> None:
    sb = get_supabase()
    sb.table(T_CONN).delete().eq("user_id", user_id).execute()


# ---------------------------------------------------------------------------
# Prefs
# ---------------------------------------------------------------------------


def list_prefs(user_id: str) -> List[Dict[str, Any]]:
    sb = get_supabase()
    res = sb.table(T_PREFS).select("*").eq("user_id", user_id).execute()
    rows = []
    for r in res.data or []:
        row = dict(r)
        if "breakeven_half_tp" not in row:
            row["breakeven_half_tp"] = True
        rows.append(row)
    return rows


def upsert_pref(
    *,
    user_id: str,
    kind: str,
    symbol: str,
    enabled: bool = False,
    max_concurrent: int = 1,
    max_per_day: int = 5,
    volume_lots: float = 0.01,
    sl_points: int = 1000,
    tp_points: int = 1000,
    breakeven_half_tp: bool = True,
) -> Dict[str, Any]:
    """
    One row per (user_id, kind, symbol). Update if exists; otherwise insert.
    (Avoids PostgREST upsert quirks that can insert duplicates.)
    """
    sb = get_supabase()
    kind = str(kind).strip()
    symbol = str(symbol).strip()
    base = {
        "user_id": user_id,
        "kind": kind,
        "symbol": symbol,
        "enabled": bool(enabled),
        "max_concurrent": int(max_concurrent),
        "max_per_day": int(max_per_day),
        "volume_lots": float(volume_lots),
        "sl_points": int(sl_points),
        "tp_points": int(tp_points),
    }
    # Prefer with BE column (sql/002); fall back if column not migrated yet
    payloads = [
        {**base, "breakeven_half_tp": bool(breakeven_half_tp)},
        base,
    ]
    existing = (
        sb.table(T_PREFS)
        .select("id")
        .eq("user_id", user_id)
        .eq("kind", kind)
        .eq("symbol", symbol)
        .limit(1)
        .execute()
    )
    last_err: Optional[Exception] = None
    for payload in payloads:
        try:
            if existing.data:
                pref_id = existing.data[0]["id"]
                res = sb.table(T_PREFS).update(payload).eq("id", pref_id).execute()
                if not res.data:
                    raise RuntimeError("update pref failed")
                # echo BE into response if column missing so UI still shows choice
                row = res.data[0]
                if "breakeven_half_tp" not in row:
                    row = dict(row)
                    row["breakeven_half_tp"] = bool(breakeven_half_tp)
                return row
            res = sb.table(T_PREFS).insert(payload).execute()
            if not res.data:
                raise RuntimeError("insert pref failed")
            row = res.data[0]
            if "breakeven_half_tp" not in row:
                row = dict(row)
                row["breakeven_half_tp"] = bool(breakeven_half_tp)
            return row
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"upsert pref failed: {last_err}")


def replace_prefs_for_user(
    user_id: str,
    prefs: List[Dict[str, Any]],
    *,
    only_enabled: bool = False,
) -> List[Dict[str, Any]]:
    """
    Replace the user's prefs with the given list (full replace).

    Keeps disabled rows when only_enabled=False (default) so users can
    toggle off without losing SL/TP/caps. only_enabled=True drops disabled.
    One row per (kind, symbol); first occurrence wins if duplicates in payload.
    """
    sb = get_supabase()
    # Wipe existing rows for this user, then insert clean set
    sb.table(T_PREFS).delete().eq("user_id", user_id).execute()

    saved: List[Dict[str, Any]] = []
    seen = set()
    for raw in prefs:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        symbol = str(raw.get("symbol") or "").strip()
        if not kind or not symbol:
            continue
        enabled = bool(raw.get("enabled", False))
        if only_enabled and not enabled:
            continue
        key = (kind, symbol)
        if key in seen:
            continue
        seen.add(key)
        # Default BE on when key omitted
        be_raw = raw.get("breakeven_half_tp", True)
        row = upsert_pref(
            user_id=user_id,
            kind=kind,
            symbol=symbol,
            enabled=enabled,
            max_concurrent=int(raw.get("max_concurrent") or 1),
            max_per_day=int(raw.get("max_per_day") or 5),
            volume_lots=float(raw.get("volume_lots") or 0.01),
            sl_points=int(raw.get("sl_points") or 1000),
            tp_points=int(raw.get("tp_points") or 1000),
            breakeven_half_tp=bool(be_raw),
        )
        saved.append(row)
    return saved


def list_candidates_for_alert(
    *,
    kind: str,
    symbol: str,
) -> List[Dict[str, Any]]:
    """
    Users who can receive this alert: enabled pref, trading on, not killed,
    has a cTrader connection. Returns joined-ish rows (user + pref + connection ids).
    """
    sb = get_supabase()
    # Prefs enabled for kind/symbol
    prefs = (
        sb.table(T_PREFS)
        .select("*")
        .eq("kind", kind)
        .eq("symbol", symbol)
        .eq("enabled", True)
        .execute()
    )
    out: List[Dict[str, Any]] = []
    for pref in prefs.data or []:
        user = get_user(pref["user_id"])
        if not user:
            continue
        if user.get("kill_switch"):
            continue
        if not user.get("trading_enabled", True):
            continue
        conn = get_connection(pref["user_id"])
        if not conn:
            continue
        out.append({"user": user, "pref": pref, "connection": conn})
    return out


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def insert_order(
    *,
    user_id: str,
    reaktor_alert_id: int,
    kind: str,
    symbol: str,
    side: str,
    status: str,
    skip_reason: Optional[str] = None,
    entry_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    volume_lots: Optional[float] = None,
    broker_order_id: Optional[str] = None,
    broker_position_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sb = get_supabase()
    payload = {
        "user_id": user_id,
        "reaktor_alert_id": int(reaktor_alert_id),
        "kind": kind,
        "symbol": symbol,
        "side": side,
        "status": status,
        "skip_reason": skip_reason,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "volume_lots": volume_lots,
        "broker_order_id": broker_order_id,
        "broker_position_id": broker_position_id,
        "detail": detail or {},
    }
    res = sb.table(T_ORDERS).insert(payload).execute()
    if not res.data:
        raise RuntimeError("insert_order failed")
    return res.data[0]


def list_orders(
    user_id: str,
    *,
    limit: int = 50,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    sb = get_supabase()
    limit = max(1, min(int(limit), 200))
    q = (
        sb.table(T_ORDERS)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status:
        q = q.eq("status", status)
    res = q.execute()
    return list(res.data or [])


def count_orders_today(
    user_id: str,
    *,
    kind: str,
    statuses: Optional[List[str]] = None,
) -> int:
    """Count orders created since UTC midnight for kind (for daily cap)."""
    sb = get_supabase()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    q = (
        sb.table(T_ORDERS)
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("kind", kind)
        .gte("created_at", day)
    )
    if statuses:
        q = q.in_("status", statuses)
    else:
        # count successful opens / fills toward daily cap
        q = q.in_("status", ["open", "filled"])
    res = q.execute()
    if res.count is not None:
        return int(res.count)
    return len(res.data or [])


def count_open_orders_db(
    user_id: str,
    *,
    kind: Optional[str] = None,
    symbol: Optional[str] = None,
) -> int:
    """
    Count Factor orders with status=open for concurrent caps.

    Scoped to kind and/or symbol so max_concurrent is per alert pref
    (kind + symbol), not account-wide.
    """
    sb = get_supabase()
    q = (
        sb.table(T_ORDERS)
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "open")
    )
    if kind:
        q = q.eq("kind", kind)
    if symbol:
        q = q.eq("symbol", symbol)
    res = q.execute()
    if res.count is not None:
        return int(res.count)
    return len(res.data or [])


def mark_open_orders_closed(
    user_id: str,
    *,
    reason: str = "kill_switch",
    result: str = "unknown",
) -> int:
    """Mark all status=open orders for user as closed. Returns rows updated (best-effort)."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: Dict[str, Any] = {
        "status": "closed",
        "closed_at": now,
        "skip_reason": reason,
        "result": result,
    }
    try:
        res = (
            sb.table(T_ORDERS)
            .update(payload)
            .eq("user_id", user_id)
            .eq("status", "open")
            .execute()
        )
    except Exception:
        # Pre-migration: no result column yet
        payload.pop("result", None)
        res = (
            sb.table(T_ORDERS)
            .update(payload)
            .eq("user_id", user_id)
            .eq("status", "open")
            .execute()
        )
    return len(res.data or [])


def list_open_orders_all(*, limit: int = 100) -> List[Dict[str, Any]]:
    """Open orders across all users (breakeven poll). Newest first."""
    sb = get_supabase()
    limit = max(1, min(int(limit), 500))
    res = (
        sb.table(T_ORDERS)
        .select("*")
        .eq("status", "open")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(res.data or [])


def update_order(order_id: str, **fields: Any) -> Dict[str, Any]:
    """Patch fields on a factor_orders row. Returns updated row."""
    if not fields:
        raise ValueError("update_order requires fields")
    sb = get_supabase()
    res = sb.table(T_ORDERS).update(fields).eq("id", order_id).execute()
    if not res.data:
        raise RuntimeError(f"update_order failed for {order_id}")
    return res.data[0]


# ---------------------------------------------------------------------------
# Processed alerts (claim-before-place)
# ---------------------------------------------------------------------------


def claim_alert(user_id: str, alert_id: int, reason: str = "claimed") -> bool:
    """
    Try to claim alert for user. Returns True if this call owns the claim.
    False if already processed (conflict).
    """
    sb = get_supabase()
    try:
        res = (
            sb.table(T_PROCESSED)
            .insert(
                {
                    "user_id": user_id,
                    "alert_id": int(alert_id),
                    "reason": reason,
                }
            )
            .execute()
        )
        return bool(res.data)
    except Exception:
        # unique violation → already claimed
        return False


def was_processed(user_id: str, alert_id: int) -> bool:
    sb = get_supabase()
    res = (
        sb.table(T_PROCESSED)
        .select("alert_id")
        .eq("user_id", user_id)
        .eq("alert_id", int(alert_id))
        .limit(1)
        .execute()
    )
    return bool(res.data)


def update_processed_reason(user_id: str, alert_id: int, reason: str) -> None:
    sb = get_supabase()
    sb.table(T_PROCESSED).update({"reason": reason}).eq("user_id", user_id).eq(
        "alert_id", int(alert_id)
    ).execute()
