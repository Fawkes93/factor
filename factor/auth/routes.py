"""OAuth + session routes (mounted on FastAPI app)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from factor import config
from factor.auth.ctrader_oauth import (
    account_id_from_row,
    account_is_live,
    broker_name_from_row,
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_accounts_all_hosts,
    host_for_account,
    pick_default_account,
    trader_login_from_row,
)
from factor.auth.session import clear_session_cookie, read_session_user_id, set_session_cookie
from factor.crypto.tokens import encrypt_token
from factor.db import repos
from factor.db.client import supabase_configured
from factor.services import live_pnl
from factor.services.outcome import annotate_order_pnl

router = APIRouter(tags=["auth"])


def _redirect_uri() -> str:
    if config.CTRADER_REDIRECT_URI:
        return config.CTRADER_REDIRECT_URI
    # Dev fallback
    return f"http://{config.HOST}:{config.PORT}/auth/ctrader/callback"


@router.get("/auth/ctrader/login")
def ctrader_login():
    """Start cTrader OAuth (also serves as Factor login)."""
    if not config.CTRADER_CLIENT_ID:
        raise HTTPException(500, "CTRADER_CLIENT_ID not configured")
    url = build_authorize_url(redirect_uri=_redirect_uri(), scope="trading")
    return RedirectResponse(url, status_code=302)


@router.get("/auth/ctrader/callback")
def ctrader_callback(
    request: Request,
    code: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        raise HTTPException(400, f"OAuth error: {error}")
    if not code:
        raise HTTPException(400, "missing code")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    if not config.FACTOR_TOKEN_ENC_KEY:
        raise HTTPException(503, "FACTOR_TOKEN_ENC_KEY not configured")

    try:
        tokens = exchange_code_for_tokens(code=code, redirect_uri=_redirect_uri())
    except Exception as e:
        raise HTTPException(400, f"token exchange failed: {e}") from e

    access = tokens.get("accessToken") or ""
    refresh = tokens.get("refreshToken") or ""
    expires_in = int(tokens.get("expiresIn") or 0)
    expires_at = None
    if expires_in > 0:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat().replace("+00:00", "Z")

    accounts = fetch_accounts_all_hosts(access_token=access)
    parsed: list = []
    for acc in accounts:
        aid = account_id_from_row(acc)
        if aid is None:
            continue
        parsed.append((aid, acc))
    if not parsed:
        raise HTTPException(
            400,
            "No trading account on this token. Grant trading scope and ensure a "
            "demo/live account is linked to your cTID.",
        )

    account_ids = [aid for aid, _ in parsed]
    trader_logins = [trader_login_from_row(acc) for _, acc in parsed]
    default_acc = pick_default_account([a for _, a in parsed], prefer_demo=True)
    display = broker_name_from_row(default_acc) if default_acc else None

    # Prefer attaching extra accounts to the already-logged-in session user
    # (Add / refresh accounts). Otherwise match by existing account ids / logins.
    session_uid = read_session_user_id(request)
    user = repos.get_user(session_uid) if session_uid else None
    if not user:
        user = repos.get_or_create_user_for_accounts(
            account_ids=account_ids,
            trader_logins=[x for x in trader_logins if x],
            display_name=display,
        )
    user_id = user["id"]
    # Fold leftover 1-account users (old OAuth created one user per login)
    # so prefs/orders follow the accounts into this session.
    for other in repos.find_users_for_accounts(
        account_ids, [x for x in trader_logins if x]
    ):
        if str(other["id"]) != str(user_id):
            repos.merge_user_into(str(other["id"]), str(user_id))

    access_enc = encrypt_token(access, config.FACTOR_TOKEN_ENC_KEY)
    refresh_enc = encrypt_token(refresh or access, config.FACTOR_TOKEN_ENC_KEY)

    for aid, acc in parsed:
        repos.upsert_ctrader_connection(
            user_id=user_id,
            account_id=aid,
            host=host_for_account(acc),
            access_token_enc=access_enc,
            refresh_token_enc=refresh_enc,
            access_expires_at=expires_at,
            scopes="trading",
            trader_login=trader_login_from_row(acc),
            broker_name=broker_name_from_row(acc),
            is_live=account_is_live(acc),
            raw_meta={"account": acc, "host": host_for_account(acc)},
        )

    repos.prune_ungranted_connections(user_id, account_ids)

    granted = set(account_ids)
    active = user.get("active_account_id")
    try:
        active_int = int(active) if active is not None else None
    except (TypeError, ValueError):
        active_int = None
    if active_int not in granted:
        pick = default_acc or parsed[0][1]
        active_int = account_id_from_row(pick) or account_ids[0]
        repos.set_active_account(user_id, active_int)

    # Prefs are created from the Alerts UI (add / toggle / delete).
    # Disabled prefs stay in DB so users can pause without losing settings.

    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, user_id)
    return resp


@router.post("/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return resp


def _require_user_id(request: Request) -> str:
    uid = read_session_user_id(request)
    if not uid:
        raise HTTPException(401, "not logged in")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    return uid


def _selected_connection(uid: str) -> Optional[dict]:
    return repos.get_connection(uid)


@router.get("/api/me")
def me(request: Request):
    uid = _require_user_id(request)
    user = repos.get_user(uid)
    if not user:
        raise HTTPException(401, "user not found")
    try:
        repos.adopt_legacy_users(uid)
        user = repos.get_user(uid) or user
    except Exception as e:
        print(f"⚠️  adopt_legacy_users: {e}")
    accounts = [repos.public_connection(c) for c in repos.list_connections(uid)]
    conn = _selected_connection(uid)
    deposit_currency = None
    if conn:
        try:
            deposit_currency = live_pnl.deposit_currency_for(conn)
        except Exception as e:
            print(f"⚠️  deposit currency: {e}")
            deposit_currency = conn.get("deposit_currency") or None
    safe_conn = repos.public_connection(conn)
    if safe_conn is not None and deposit_currency:
        safe_conn["deposit_currency"] = deposit_currency
    account_id = int(conn["account_id"]) if conn and conn.get("account_id") is not None else None
    prefs = repos.list_prefs(uid, account_id) if account_id is not None else []
    # Surface the selected account's kill flags on user so existing UI keeps working
    kill = (conn or {}).get("kill_switch")
    if kill is None:
        kill = user.get("kill_switch")
    trading = (conn or {}).get("trading_enabled")
    if trading is None:
        trading = user.get("trading_enabled")
    return {
        "user": {
            "id": user["id"],
            "display_name": user.get("display_name"),
            "kill_switch": bool(kill),
            "trading_enabled": True if trading is None else bool(trading),
            "ctrader_ctid": user.get("ctrader_ctid"),
            "active_account_id": account_id,
        },
        "accounts": accounts,
        "connection": safe_conn,
        "prefs": prefs,
        "deposit_currency": deposit_currency,
    }


@router.post("/api/me/account")
async def select_account(request: Request):
    """Switch the dashboard to another granted cTrader account."""
    uid = _require_user_id(request)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    raw = body.get("account_id") if isinstance(body, dict) else None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "account_id required")
    try:
        user = repos.set_active_account(uid, account_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    conn = repos.get_connection(uid, account_id)
    return {
        "ok": True,
        "account_id": account_id,
        "user": {
            "id": user["id"],
            "active_account_id": user.get("active_account_id"),
        },
        "connection": repos.public_connection(conn),
    }


@router.post("/api/me/kill")
def kill(request: Request):
    """
    Per-user kill switch: block new trades + close all open cTrader positions.
    """
    uid = _require_user_id(request)
    conn = _selected_connection(uid)
    if not conn:
        raise HTTPException(400, "no cTrader account")
    from factor.services.kill import activate_kill_switch

    try:
        return activate_kill_switch(uid, int(conn["account_id"]))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"kill failed: {e}") from e


@router.post("/api/me/resume")
def resume(request: Request):
    """Clear kill switch; does not re-open positions."""
    uid = _require_user_id(request)
    conn = _selected_connection(uid)
    if not conn:
        raise HTTPException(400, "no cTrader account")
    from factor.services.kill import resume_trading

    try:
        return resume_trading(uid, int(conn["account_id"]))
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"resume failed: {e}") from e


@router.get("/api/me/orders")
def my_orders(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    status: Optional[str] = None,
):
    uid = _require_user_id(request)
    conn = _selected_connection(uid)
    account_id = int(conn["account_id"]) if conn and conn.get("account_id") is not None else None
    orders = repos.list_orders(uid, limit=limit, status=status, account_id=account_id)
    live: dict = {}
    if conn and any(str(o.get("status") or "").lower() == "open" for o in orders):
        live = live_pnl.unrealized_for_connection(conn)
    annotated = [annotate_order_pnl(o, live) for o in orders]
    deposit_currency = None
    if conn:
        try:
            deposit_currency = live_pnl.deposit_currency_for(conn)
        except Exception as e:
            print(f"⚠️  deposit currency: {e}")
            deposit_currency = conn.get("deposit_currency") or None
    return {
        "count": len(annotated),
        "orders": annotated,
        "deposit_currency": deposit_currency,
    }


@router.put("/api/me/prefs")
async def put_prefs(request: Request):
    """
    Replace/upsert alert prefs.
    Body: { "prefs": [ { kind, symbol, enabled, max_concurrent, max_per_day,
                          volume_lots, sl_points, tp_points, sl_tp_override }, ... ] }
    """
    uid = _require_user_id(request)
    conn = _selected_connection(uid)
    if not conn:
        raise HTTPException(400, "no cTrader account")
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    items = body.get("prefs") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise HTTPException(400, "prefs must be a list")
    try:
        # Full replace for the selected account only.
        # Delete in UI = omit from list. One row per (account, kind, symbol).
        saved = repos.replace_prefs_for_user(
            uid,
            items,
            only_enabled=False,
            account_id=int(conn["account_id"]),
        )
    except Exception as e:
        raise HTTPException(500, f"save prefs failed: {e}") from e
    return {"count": len(saved), "prefs": saved}


def _selected_account(request: Request) -> tuple:
    """Logged-in user and the dashboard's cTrader account id."""
    uid = _require_user_id(request)
    conn = _selected_connection(uid)
    if not conn or conn.get("account_id") is None:
        raise HTTPException(400, "no cTrader account")
    return uid, int(conn["account_id"])


def _pref_json(request_body: object) -> dict:
    if request_body is None:
        return {}
    if not isinstance(request_body, dict):
        raise HTTPException(400, "body must be an object")
    return request_body


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    return _pref_json(body)


def _body_int(body: dict, key: str, default: int) -> int:
    if key not in body or body.get(key) is None or body.get(key) == "":
        return default
    return int(body[key])


def _body_float(body: dict, key: str, default: float) -> float:
    if key not in body or body.get(key) is None or body.get(key) == "":
        return default
    return float(body[key])


_PREF_PATCH_KEYS = (
    "enabled",
    "max_concurrent",
    "max_per_day",
    "volume_lots",
    "sl_points",
    "tp_points",
    "sl_tp_override",
    "breakeven_half_tp",
    "breakeven_fraction",
)


@router.patch("/api/me/prefs/{kind}/{symbol:path}")
async def patch_pref(kind: str, symbol: str, request: Request):
    """Update one alert. Body contains only the changed fields."""
    uid, account_id = _selected_account(request)
    body = await _read_json(request)
    fields = {key: body[key] for key in _PREF_PATCH_KEYS if key in body}
    if not fields:
        raise HTTPException(400, "no preference fields")
    try:
        row = repos.update_pref(uid, account_id, kind, symbol, fields)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except repos.PrefSchemaError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"save pref failed: {e}") from e
    return {"pref": row}


@router.post("/api/me/prefs/{kind}/{symbol:path}")
async def create_pref(kind: str, symbol: str, request: Request):
    """Create one alert. 409 when that kind and symbol already exist."""
    uid, account_id = _selected_account(request)
    ctype = (request.headers.get("content-type") or "").lower()
    body = await _read_json(request) if "application/json" in ctype else {}
    kind = str(kind or "").strip()
    symbol = str(symbol or "").strip()
    if not kind or not symbol:
        raise HTTPException(400, "kind and symbol required")
    if repos.get_pref(uid, account_id, kind, symbol):
        raise HTTPException(409, "alert already exists")
    try:
        row = repos.upsert_pref(
            user_id=uid,
            account_id=account_id,
            kind=kind,
            symbol=symbol,
            enabled=bool(body.get("enabled", True)),
            max_concurrent=_body_int(body, "max_concurrent", 1),
            max_per_day=_body_int(body, "max_per_day", 5),
            volume_lots=_body_float(body, "volume_lots", 0.01),
            sl_points=_body_int(body, "sl_points", 1000),
            tp_points=_body_int(body, "tp_points", 1000),
            breakeven_half_tp=bool(body.get("breakeven_half_tp", True)),
            breakeven_fraction=float(body.get("breakeven_fraction", 0.5) or 0.5),
            sl_tp_override=bool(body.get("sl_tp_override", False)),
        )
    except repos.PrefSchemaError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"save pref failed: {e}") from e
    return {"pref": row}


@router.delete("/api/me/prefs/{kind}/{symbol:path}")
def delete_pref(kind: str, symbol: str, request: Request):
    """Delete one alert."""
    uid, account_id = _selected_account(request)
    if not repos.delete_pref(uid, account_id, kind, symbol):
        raise HTTPException(404, "alert not found")
    return {"ok": True}
