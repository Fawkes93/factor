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
from factor.rules.kinds import is_asia_box, is_crt, is_session_managed

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


def get_user_by_ctid(ctrader_ctid: str) -> Optional[Dict[str, Any]]:
    sb = get_supabase()
    res = (
        sb.table(T_USERS)
        .select("*")
        .eq("ctrader_ctid", ctrader_ctid)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def find_users_for_accounts(
    account_ids: List[int],
    trader_logins: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    All Factor users that own any of these cTrader accounts / trader logins.

    Pre-multi-account logins created one user per trading account. A later
    grant of all accounts must merge those rows or prefs stay invisible.
    """
    sb = get_supabase()
    found: Dict[str, Dict[str, Any]] = {}
    ids = []
    for raw in account_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if ids:
        res = (
            sb.table(T_CONN)
            .select("user_id")
            .in_("account_id", ids)
            .execute()
        )
        for row in res.data or []:
            uid = str(row.get("user_id") or "")
            if not uid or uid in found:
                continue
            user = get_user(uid)
            if user:
                found[uid] = user
    for login in trader_logins or []:
        if not login:
            continue
        user = get_user_by_ctid(str(login))
        if user:
            found[str(user["id"])] = user
    out = list(found.values())
    out.sort(key=lambda u: str(u.get("created_at") or ""))
    return out


def find_user_for_accounts(
    account_ids: List[int],
    trader_logins: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    users = find_users_for_accounts(account_ids, trader_logins)
    return users[0] if users else None


def merge_user_into(source_id: str, dest_id: str) -> None:
    """
    Move connections, prefs, orders, and claims from source_id onto dest_id,
    then delete the source user. No-op if ids match or source is gone.
    """
    if not source_id or not dest_id or str(source_id) == str(dest_id):
        return
    source = get_user(source_id)
    dest = get_user(dest_id)
    if not source or not dest:
        return
    sb = get_supabase()
    dest_id = str(dest["id"])
    source_id = str(source["id"])

    dest_account_ids = {
        int(c["account_id"])
        for c in list_connections(dest_id)
        if c.get("account_id") is not None
    }
    src_conns = list_connections(source_id)
    fallback_aid = None
    if len(src_conns) == 1 and src_conns[0].get("account_id") is not None:
        fallback_aid = int(src_conns[0]["account_id"])

    for conn in src_conns:
        aid = int(conn["account_id"]) if conn.get("account_id") is not None else None
        if aid is not None and aid in dest_account_ids:
            delete_connection(source_id, aid)
        else:
            sb.table(T_CONN).update({"user_id": dest_id}).eq("id", conn["id"]).execute()
            if aid is not None:
                dest_account_ids.add(aid)

    dest_pref_keys = {
        (int(p.get("account_id") or 0), str(p.get("kind") or ""), str(p.get("symbol") or ""))
        for p in list_prefs(dest_id)
    }
    for pref in list_prefs(source_id):
        raw_aid = pref.get("account_id")
        try:
            aid = int(raw_aid) if raw_aid is not None else 0
        except (TypeError, ValueError):
            aid = 0
        if aid == 0 and fallback_aid is not None:
            aid = fallback_aid
        key = (aid, str(pref.get("kind") or ""), str(pref.get("symbol") or ""))
        if key in dest_pref_keys:
            sb.table(T_PREFS).delete().eq("id", pref["id"]).execute()
            continue
        patch: Dict[str, Any] = {"user_id": dest_id}
        if int(pref.get("account_id") or 0) != aid:
            patch["account_id"] = aid
        sb.table(T_PREFS).update(patch).eq("id", pref["id"]).execute()
        dest_pref_keys.add(key)

    try:
        sb.table(T_ORDERS).update({"user_id": dest_id}).eq("user_id", source_id).execute()
    except Exception as e:
        print(f"⚠️  merge orders {source_id[:8]}→{dest_id[:8]}: {e}")

    try:
        sb.table(T_PROCESSED).delete().eq("user_id", source_id).execute()
    except Exception as e:
        print(f"⚠️  merge claims {source_id[:8]}: {e}")

    sb.table(T_USERS).delete().eq("id", source_id).execute()
    print(f"🔗 Merged Factor user {source_id[:8]} into {dest_id[:8]}")


def adopt_legacy_users(user_id: str) -> int:
    """
    Fold leftover 1-account Factor users into this user when they share
    a cTrader account id or trader login.
    """
    conns = list_connections(user_id)
    account_ids = [
        int(c["account_id"]) for c in conns if c.get("account_id") is not None
    ]
    logins = [str(c["trader_login"]) for c in conns if c.get("trader_login")]
    others = find_users_for_accounts(account_ids, logins)
    n = 0
    for other in others:
        oid = str(other["id"])
        if oid == str(user_id):
            continue
        merge_user_into(oid, str(user_id))
        n += 1
    return n


def get_or_create_user_for_accounts(
    *,
    account_ids: List[int],
    trader_logins: Optional[List[str]] = None,
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    found = find_user_for_accounts(account_ids, trader_logins)
    if found:
        if display_name and not found.get("display_name"):
            upd = (
                get_supabase()
                .table(T_USERS)
                .update({"display_name": display_name})
                .eq("id", found["id"])
                .execute()
            )
            return (upd.data or [found])[0]
        return found
    ctid = next((str(x) for x in (trader_logins or []) if x), None)
    if not ctid and account_ids:
        ctid = f"account:{int(account_ids[0])}"
    return upsert_user_by_ctid(ctrader_ctid=ctid, display_name=display_name)


def set_active_account(user_id: str, account_id: int) -> Dict[str, Any]:
    sb = get_supabase()
    conn = get_connection(user_id, int(account_id))
    if not conn:
        raise ValueError("account not found")
    res = (
        sb.table(T_USERS)
        .update({"active_account_id": int(account_id)})
        .eq("id", user_id)
        .execute()
    )
    if not res.data:
        raise RuntimeError("set_active_account failed")
    return res.data[0]


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
    """Legacy user-level kill. Prefer set_account_kill_switch."""
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


def set_account_kill_switch(
    user_id: str, account_id: int, value: bool
) -> Dict[str, Any]:
    sb = get_supabase()
    res = (
        sb.table(T_CONN)
        .update({"kill_switch": bool(value), "trading_enabled": not bool(value)})
        .eq("user_id", user_id)
        .eq("account_id", int(account_id))
        .execute()
    )
    if not res.data:
        raise RuntimeError("set_account_kill_switch failed")
    return res.data[0]


# ---------------------------------------------------------------------------
# cTrader connections
# ---------------------------------------------------------------------------


def connection_is_live(conn: Optional[Dict[str, Any]]) -> bool:
    if not conn:
        return False
    if conn.get("is_live") is True:
        return True
    if conn.get("is_live") is False:
        return False
    return str(conn.get("host") or "").lower().startswith("live")


def account_label(conn: Optional[Dict[str, Any]]) -> str:
    if not conn:
        return "cTrader"
    broker = str(conn.get("broker_name") or "cTrader").strip() or "cTrader"
    env = "Live" if connection_is_live(conn) else "Demo"
    ident = conn.get("trader_login") or conn.get("account_id") or ""
    return f"{broker} · {env} · {ident}"


def public_connection(conn: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Safe account payload for the dashboard (no tokens)."""
    if not conn:
        return None
    return {
        "account_id": conn.get("account_id"),
        "host": conn.get("host"),
        "trader_login": conn.get("trader_login"),
        "broker_name": conn.get("broker_name"),
        "is_live": connection_is_live(conn),
        "kill_switch": bool(conn.get("kill_switch")),
        "trading_enabled": conn.get("trading_enabled", True),
        "access_expires_at": conn.get("access_expires_at"),
        "updated_at": conn.get("updated_at"),
        "deposit_currency": conn.get("deposit_currency") or None,
        "label": account_label(conn),
    }


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
    trader_login: Optional[str] = None,
    broker_name: Optional[str] = None,
    is_live: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Upsert one trading account for the user.

    Tokens are the shared OAuth grant (same ciphertext on every account row).
    kill_switch / trading_enabled are omitted so re-login does not reset them.
    """
    sb = get_supabase()
    payload: Dict[str, Any] = {
        "user_id": user_id,
        "account_id": int(account_id),
        "host": host,
        "access_token_enc": access_token_enc,
        "refresh_token_enc": refresh_token_enc,
        "access_expires_at": access_expires_at,
        "scopes": scopes,
        "raw_meta": raw_meta or {},
    }
    if trader_login is not None:
        payload["trader_login"] = trader_login
    if broker_name is not None:
        payload["broker_name"] = broker_name
    if is_live is not None:
        payload["is_live"] = bool(is_live)
    res = (
        sb.table(T_CONN)
        .upsert(payload, on_conflict="user_id,account_id")
        .execute()
    )
    if not res.data:
        raise RuntimeError("upsert_ctrader_connection failed")
    return res.data[0]


def get_connection(
    user_id: str, account_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    sb = get_supabase()
    if account_id is not None:
        res = (
            sb.table(T_CONN)
            .select("*")
            .eq("user_id", user_id)
            .eq("account_id", int(account_id))
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    user = get_user(user_id)
    active = user.get("active_account_id") if user else None
    if active is not None:
        res = (
            sb.table(T_CONN)
            .select("*")
            .eq("user_id", user_id)
            .eq("account_id", int(active))
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    res = sb.table(T_CONN).select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def list_connections(user_id: str) -> List[Dict[str, Any]]:
    sb = get_supabase()
    res = sb.table(T_CONN).select("*").eq("user_id", user_id).execute()
    rows = list(res.data or [])

    def _sort_key(r: Dict[str, Any]) -> tuple:
        return (
            str(r.get("broker_name") or "").lower(),
            1 if connection_is_live(r) else 0,
            str(r.get("trader_login") or ""),
            int(r.get("account_id") or 0),
        )

    rows.sort(key=_sort_key)
    return rows


def delete_connection(user_id: str, account_id: Optional[int] = None) -> None:
    sb = get_supabase()
    q = sb.table(T_CONN).delete().eq("user_id", user_id)
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
    q.execute()


def prune_ungranted_connections(user_id: str, keep_account_ids: List[int]) -> int:
    """Delete connection rows not in the latest OAuth grant. Prefs are kept."""
    keep = set()
    for raw in keep_account_ids or []:
        try:
            keep.add(int(raw))
        except (TypeError, ValueError):
            continue
    if not keep:
        return 0
    n = 0
    for row in list_connections(user_id):
        aid = int(row.get("account_id") or 0)
        if aid not in keep:
            delete_connection(user_id, aid)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Prefs
# ---------------------------------------------------------------------------


def _normalize_pref_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Apply kind rules the dashboard and engine both rely on."""
    row = dict(row)
    kind_name = str(row.get("kind") or "")
    if is_asia_box(kind_name):
        row["breakeven_half_tp"] = False
        row["sl_tp_override"] = bool(row.get("sl_tp_override"))
    elif is_session_managed(kind_name) or is_crt(kind_name):
        row["breakeven_half_tp"] = False
        row["sl_tp_override"] = False
    else:
        if "breakeven_half_tp" not in row:
            row["breakeven_half_tp"] = True
        row["sl_tp_override"] = False
    if row.get("breakeven_fraction") is None:
        row["breakeven_fraction"] = 0.5
    return row


def list_prefs(
    user_id: str, account_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    sb = get_supabase()
    q = sb.table(T_PREFS).select("*").eq("user_id", user_id)
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
    res = q.execute()
    return [_normalize_pref_row(r) for r in (res.data or [])]


def get_pref(
    user_id: str, account_id: int, kind: str, symbol: str
) -> Optional[Dict[str, Any]]:
    sb = get_supabase()
    res = (
        sb.table(T_PREFS)
        .select("*")
        .eq("user_id", user_id)
        .eq("account_id", int(account_id))
        .eq("kind", str(kind).strip())
        .eq("symbol", str(symbol).strip())
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return _normalize_pref_row(res.data[0])


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# Columns a single-alert PATCH may change. kind/symbol identify the row.
_PREF_FIELD_CAST = {
    "enabled": _as_bool,
    "max_concurrent": lambda v: int(v),
    "max_per_day": lambda v: int(v),
    "volume_lots": lambda v: float(v),
    "sl_points": lambda v: int(v),
    "tp_points": lambda v: int(v),
    "sl_tp_override": _as_bool,
    "breakeven_half_tp": _as_bool,
    "breakeven_fraction": lambda v: _normalize_breakeven_fraction(v),
}


def update_pref(
    user_id: str,
    account_id: int,
    kind: str,
    symbol: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    """
    UPDATE one alert row. `fields` is a whitelist of changed columns.
    Raises LookupError when the row does not exist.
    """
    kind = str(kind or "").strip()
    symbol = str(symbol or "").strip()
    if not kind or not symbol:
        raise ValueError("kind and symbol required")
    if not isinstance(fields, dict):
        raise ValueError("no preference fields")
    clean: Dict[str, Any] = {}
    for key, cast in _PREF_FIELD_CAST.items():
        if key not in fields:
            continue
        try:
            clean[key] = cast(fields[key])
        except (TypeError, ValueError) as e:
            raise ValueError(f"invalid {key}") from e
    if not clean:
        raise ValueError("no preference fields")
    if "volume_lots" in clean and not (clean["volume_lots"] > 0):
        raise ValueError("volume_lots must be > 0")
    if "sl_points" in clean and clean["sl_points"] <= 0:
        raise ValueError("sl_points must be > 0")
    if "tp_points" in clean and clean["tp_points"] <= 0:
        raise ValueError("tp_points must be > 0")
    if "max_concurrent" in clean and clean["max_concurrent"] < 0:
        raise ValueError("max_concurrent must be >= 0")
    if "max_per_day" in clean and clean["max_per_day"] < 0:
        raise ValueError("max_per_day must be >= 0")
    if is_asia_box(kind):
        if "breakeven_half_tp" in clean:
            clean["breakeven_half_tp"] = False
    elif is_session_managed(kind) or is_crt(kind):
        if "breakeven_half_tp" in clean:
            clean["breakeven_half_tp"] = False
        if "sl_tp_override" in clean:
            clean["sl_tp_override"] = False
    else:
        if "sl_tp_override" in clean:
            clean["sl_tp_override"] = False

    existing = get_pref(user_id, int(account_id), kind, symbol)
    if not existing:
        raise LookupError("alert not found")

    sb = get_supabase()
    payload = dict(clean)
    optional = [
        col
        for col in ("sl_tp_override", "breakeven_fraction", "breakeven_half_tp")
        if col in payload
    ]
    dropped: List[str] = []
    last_err: Optional[Exception] = None
    while True:
        try:
            res = (
                sb.table(T_PREFS).update(payload).eq("id", existing["id"]).execute()
            )
            if not res.data:
                raise RuntimeError("update pref failed")
            if dropped:
                raise PrefSchemaError(
                    "That setting is not stored yet. Run the latest sql migrations in Supabase, then save again."
                )
            return _normalize_pref_row(res.data[0])
        except PrefSchemaError:
            raise
        except Exception as e:
            last_err = e
            if not optional:
                break
            col = optional.pop(0)
            payload.pop(col, None)
            dropped.append(col)
    raise RuntimeError(f"update pref failed: {last_err}")


def delete_pref(
    user_id: str, account_id: int, kind: str, symbol: str
) -> bool:
    """Delete one alert. Returns False when it was not stored."""
    existing = get_pref(user_id, int(account_id), kind, symbol)
    if not existing:
        return False
    sb = get_supabase()
    sb.table(T_PREFS).delete().eq("id", existing["id"]).execute()
    return True


def set_deposit_currency(user_id: str, account_id: int, currency: str) -> None:
    """Cache the account deposit currency on the connection row."""
    code = str(currency or "").strip().upper()
    if not code or not user_id:
        return
    sb = get_supabase()
    (
        sb.table(T_CONN)
        .update({"deposit_currency": code[:16]})
        .eq("user_id", user_id)
        .eq("account_id", int(account_id))
        .execute()
    )


class PrefSchemaError(RuntimeError):
    """A requested preference cannot be stored (database migration not applied)."""


def _echo_pref_row(
    row: Dict[str, Any],
    *,
    breakeven_half_tp: bool,
    be_frac: float,
    sl_tp_override: bool,
) -> Dict[str, Any]:
    row = dict(row)
    if "breakeven_half_tp" not in row:
        row["breakeven_half_tp"] = bool(breakeven_half_tp)
    if row.get("breakeven_fraction") is None:
        row["breakeven_fraction"] = be_frac
    if "sl_tp_override" not in row:
        if sl_tp_override:
            raise PrefSchemaError(
                "SL/TP override is not stored yet. Run sql/006_sl_tp_override.sql in Supabase, then save again."
            )
        row["sl_tp_override"] = False
    else:
        row["sl_tp_override"] = bool(row.get("sl_tp_override"))
    return row


def _normalize_breakeven_fraction(raw: Any, default: float = 0.5) -> float:
    try:
        f = float(raw)
    except (TypeError, ValueError):
        f = float(default)
    if f <= 0 or f > 1.0:
        f = float(default) if 0 < float(default) <= 1.0 else 0.5
    return f


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
    breakeven_fraction: float = 0.5,
    sl_tp_override: bool = False,
    account_id: int,
) -> Dict[str, Any]:
    """
    One row per (user_id, account_id, kind, symbol). Update if exists; otherwise insert.
    (Avoids PostgREST upsert quirks that can insert duplicates.)
    """
    sb = get_supabase()
    kind = str(kind).strip()
    symbol = str(symbol).strip()
    if is_asia_box(kind):
        breakeven_half_tp = False
    elif is_session_managed(kind) or is_crt(kind):
        # Dawn Cross and CRT: Reaktor SL/TP. No point override, no breakeven.
        breakeven_half_tp = False
        sl_tp_override = False
    else:
        # Override only changes Asia box alerts (Reaktor box vs user points).
        sl_tp_override = False
    be_frac = _normalize_breakeven_fraction(breakeven_fraction, 0.5)
    sl_tp_override = bool(sl_tp_override)
    account_id = int(account_id)
    base = {
        "user_id": user_id,
        "account_id": account_id,
        "kind": kind,
        "symbol": symbol,
        "enabled": bool(enabled),
        "max_concurrent": int(max_concurrent),
        "max_per_day": int(max_per_day),
        "volume_lots": float(volume_lots),
        "sl_points": int(sl_points),
        "tp_points": int(tp_points),
    }
    # Prefer full columns; fall back if migrations not applied yet
    payloads = [
        {
            **base,
            "breakeven_half_tp": bool(breakeven_half_tp),
            "breakeven_fraction": be_frac,
            "sl_tp_override": sl_tp_override,
        },
        {
            **base,
            "breakeven_half_tp": bool(breakeven_half_tp),
            "breakeven_fraction": be_frac,
        },
        {**base, "breakeven_half_tp": bool(breakeven_half_tp)},
        base,
    ]
    existing = (
        sb.table(T_PREFS)
        .select("id")
        .eq("user_id", user_id)
        .eq("account_id", account_id)
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
                return _echo_pref_row(
                    res.data[0],
                    breakeven_half_tp=bool(breakeven_half_tp),
                    be_frac=be_frac,
                    sl_tp_override=sl_tp_override,
                )
            res = sb.table(T_PREFS).insert(payload).execute()
            if not res.data:
                raise RuntimeError("insert pref failed")
            return _echo_pref_row(
                res.data[0],
                breakeven_half_tp=bool(breakeven_half_tp),
                be_frac=be_frac,
                sl_tp_override=sl_tp_override,
            )
        except PrefSchemaError:
            raise
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"upsert pref failed: {last_err}")


def replace_prefs_for_user(
    user_id: str,
    prefs: List[Dict[str, Any]],
    *,
    only_enabled: bool = False,
    account_id: int,
) -> List[Dict[str, Any]]:
    """
    Replace prefs for one of the user's trading accounts (full replace).

    Keeps disabled rows when only_enabled=False (default) so users can
    toggle off without losing SL/TP/caps. only_enabled=True drops disabled.
    One row per (account, kind, symbol); first occurrence wins if duplicates.
    """
    sb = get_supabase()
    account_id = int(account_id)
    # Wipe existing rows for this user+account, then insert clean set
    sb.table(T_PREFS).delete().eq("user_id", user_id).eq(
        "account_id", account_id
    ).execute()

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
        # Default BE on when key omitted; fraction defaults to half TP
        be_raw = raw.get("breakeven_half_tp", True)
        be_frac = raw.get("breakeven_fraction", 0.5)
        row = upsert_pref(
            user_id=user_id,
            account_id=account_id,
            kind=kind,
            symbol=symbol,
            enabled=enabled,
            max_concurrent=int(raw.get("max_concurrent") or 1),
            max_per_day=int(raw.get("max_per_day") or 5),
            volume_lots=float(raw.get("volume_lots") or 0.01),
            sl_points=int(raw.get("sl_points") or 1000),
            tp_points=int(raw.get("tp_points") or 1000),
            breakeven_half_tp=bool(be_raw),
            breakeven_fraction=_normalize_breakeven_fraction(be_frac, 0.5),
            sl_tp_override=bool(raw.get("sl_tp_override", False)),
        )
        saved.append(row)
    return saved


def list_candidates_for_alert(
    *,
    kind: str,
    symbol: str,
) -> List[Dict[str, Any]]:
    """
    Accounts that can receive this alert: enabled pref for that account,
    trading on, not killed, has a matching cTrader connection.
    """
    sb = get_supabase()
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
        account_id = pref.get("account_id")
        if account_id is not None:
            conn = get_connection(pref["user_id"], int(account_id))
        else:
            conn = get_connection(pref["user_id"])
        if not conn:
            continue
        if account_id is not None and int(conn.get("account_id") or 0) != int(
            account_id
        ):
            continue
        conn_killed = conn.get("kill_switch")
        if conn_killed or (conn_killed is None and user.get("kill_switch")):
            continue
        conn_enabled = conn.get("trading_enabled")
        if conn_enabled is False or (
            conn_enabled is None and not user.get("trading_enabled", True)
        ):
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
    account_id: Optional[int] = None,
) -> Dict[str, Any]:
    sb = get_supabase()
    payload: Dict[str, Any] = {
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
    if account_id is not None:
        payload["account_id"] = int(account_id)
    res = sb.table(T_ORDERS).insert(payload).execute()
    if not res.data:
        raise RuntimeError("insert_order failed")
    return res.data[0]


def list_orders(
    user_id: str,
    *,
    limit: int = 50,
    status: Optional[str] = None,
    account_id: Optional[int] = None,
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
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
    if status:
        q = q.eq("status", status)
    res = q.execute()
    return list(res.data or [])


def count_orders_today(
    user_id: str,
    *,
    kind: str,
    statuses: Optional[List[str]] = None,
    account_id: Optional[int] = None,
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
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
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
    account_id: Optional[int] = None,
) -> int:
    """
    Count Factor orders with status=open for concurrent caps.

    Scoped to account + kind + symbol so max_concurrent is per alert pref
    on that trading account, not across the user's other accounts.
    """
    sb = get_supabase()
    q = (
        sb.table(T_ORDERS)
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "open")
    )
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
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
    account_id: Optional[int] = None,
) -> int:
    """Mark status=open orders as closed. Optionally scoped to one account."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: Dict[str, Any] = {
        "status": "closed",
        "closed_at": now,
        "skip_reason": reason,
        "result": result,
    }

    def _run(fields: Dict[str, Any]):
        q = (
            sb.table(T_ORDERS)
            .update(fields)
            .eq("user_id", user_id)
            .eq("status", "open")
        )
        if account_id is not None:
            q = q.eq("account_id", int(account_id))
        return q.execute()

    try:
        res = _run(payload)
    except Exception:
        # Pre-migration: no result column yet
        fields = dict(payload)
        fields.pop("result", None)
        res = _run(fields)
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


def claim_alert(
    user_id: str,
    alert_id: int,
    reason: str = "claimed",
    account_id: Optional[int] = None,
) -> bool:
    """
    Try to claim alert for one trading account. Returns True if this call
    owns the claim. False if already processed (conflict).
    """
    sb = get_supabase()
    acc = int(account_id) if account_id is not None else 0
    payload = {
        "user_id": user_id,
        "alert_id": int(alert_id),
        "reason": reason,
        "account_id": acc,
    }
    try:
        res = sb.table(T_PROCESSED).insert(payload).execute()
        return bool(res.data)
    except Exception:
        # unique violation → already claimed
        return False


def was_processed(
    user_id: str, alert_id: int, account_id: Optional[int] = None
) -> bool:
    sb = get_supabase()
    q = (
        sb.table(T_PROCESSED)
        .select("alert_id")
        .eq("user_id", user_id)
        .eq("alert_id", int(alert_id))
    )
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
    res = q.limit(1).execute()
    return bool(res.data)


def update_processed_reason(
    user_id: str,
    alert_id: int,
    reason: str,
    account_id: Optional[int] = None,
) -> None:
    sb = get_supabase()
    q = (
        sb.table(T_PROCESSED)
        .update({"reason": reason})
        .eq("user_id", user_id)
        .eq("alert_id", int(alert_id))
    )
    if account_id is not None:
        q = q.eq("account_id", int(account_id))
    q.execute()
