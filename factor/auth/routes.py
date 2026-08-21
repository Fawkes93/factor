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
    accounts = [repos.public_connection(c) for c in repos.list_connections(uid)]
    conn = _selected_connection(uid)
    safe_conn = repos.public_connection(conn)
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
    return {"count": len(orders), "orders": orders}


@router.put("/api/me/prefs")
async def put_prefs(request: Request):
    """
    Replace/upsert alert prefs.
    Body: { "prefs": [ { kind, symbol, enabled, max_concurrent, max_per_day,
                          volume_lots, sl_points, tp_points }, ... ] }
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
