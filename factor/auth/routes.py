"""OAuth + session routes (mounted on FastAPI app)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from factor import config
from factor.auth.ctrader_oauth import (
    account_id_from_row,
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_accounts_any_host,
    host_for_account,
    pick_default_account,
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

    accounts, host_tried = fetch_accounts_any_host(access_token=access)
    # Prefer demo account when available; host follows the selected account
    account = pick_default_account(accounts, prefer_demo=True)
    host = host_for_account(account) if account else host_tried
    account_id = account_id_from_row(account) if account else None
    if account_id is None:
        raise HTTPException(
            400,
            "No trading account on this token. Grant trading scope and ensure a "
            "demo/live account is linked to your cTID.",
        )

    # Identity: prefer traderLogin / ctid fields if present
    ctid = None
    if account:
        ctid = (
            str(account.get("traderLogin") or account.get("ctidTraderId") or "")
            or None
        )
    if not ctid:
        ctid = f"account:{account_id}"

    display = None
    if account:
        display = str(
            account.get("brokerTitleTitle")
            or account.get("brokerName")
            or account.get("depositAsset")
            or ctid
        )

    user = repos.upsert_user_by_ctid(ctrader_ctid=ctid, display_name=display)
    user_id = user["id"]

    repos.upsert_ctrader_connection(
        user_id=user_id,
        account_id=account_id,
        host=host,
        access_token_enc=encrypt_token(access, config.FACTOR_TOKEN_ENC_KEY),
        refresh_token_enc=encrypt_token(refresh or access, config.FACTOR_TOKEN_ENC_KEY),
        access_expires_at=expires_at,
        scopes="trading",
        raw_meta={"accounts": accounts, "selected": account, "host": host},
    )

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


@router.get("/api/me")
def me(request: Request):
    uid = read_session_user_id(request)
    if not uid:
        raise HTTPException(401, "not logged in")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    user = repos.get_user(uid)
    if not user:
        raise HTTPException(401, "user not found")
    conn = repos.get_connection(uid)
    prefs = repos.list_prefs(uid)
    safe_conn = None
    if conn:
        safe_conn = {
            "account_id": conn.get("account_id"),
            "host": conn.get("host"),
            "access_expires_at": conn.get("access_expires_at"),
            "updated_at": conn.get("updated_at"),
        }
    return {
        "user": {
            "id": user["id"],
            "display_name": user.get("display_name"),
            "kill_switch": user.get("kill_switch"),
            "trading_enabled": user.get("trading_enabled"),
            "ctrader_ctid": user.get("ctrader_ctid"),
        },
        "connection": safe_conn,
        "prefs": prefs,
    }


@router.post("/api/me/kill")
def kill(request: Request):
    """
    Per-user kill switch: block new trades + close all open cTrader positions.
    """
    uid = read_session_user_id(request)
    if not uid:
        raise HTTPException(401, "not logged in")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    from factor.services.kill import activate_kill_switch

    try:
        return activate_kill_switch(uid)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"kill failed: {e}") from e


@router.post("/api/me/resume")
def resume(request: Request):
    """Clear kill switch; does not re-open positions."""
    uid = read_session_user_id(request)
    if not uid:
        raise HTTPException(401, "not logged in")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    from factor.services.kill import resume_trading

    try:
        return resume_trading(uid)
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
    uid = read_session_user_id(request)
    if not uid:
        raise HTTPException(401, "not logged in")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    orders = repos.list_orders(uid, limit=limit, status=status)
    return {"count": len(orders), "orders": orders}


@router.put("/api/me/prefs")
async def put_prefs(request: Request):
    """
    Replace/upsert alert prefs.
    Body: { "prefs": [ { kind, symbol, enabled, max_concurrent, max_per_day,
                          volume_lots, sl_points, tp_points }, ... ] }
    """
    uid = read_session_user_id(request)
    if not uid:
        raise HTTPException(401, "not logged in")
    if not supabase_configured():
        raise HTTPException(503, "Supabase not configured")
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    items = body.get("prefs") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise HTTPException(400, "prefs must be a list")
    try:
        # Full replace: keeps disabled prefs (toggle off without losing settings).
        # Delete in UI = omit from list. One row per (kind, symbol).
        saved = repos.replace_prefs_for_user(uid, items, only_enabled=False)
    except Exception as e:
        raise HTTPException(500, f"save prefs failed: {e}") from e
    return {"count": len(saved), "prefs": saved}
