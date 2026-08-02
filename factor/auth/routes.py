"""OAuth + session routes (mounted on FastAPI app)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from factor import config
from factor.auth.ctrader_oauth import (
    account_id_from_row,
    build_authorize_url,
    exchange_code_for_tokens,
    fetch_accounts_for_token,
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


def _host_for_env(env: str) -> str:
    e = (env or "demo").strip().lower()
    if e in ("live", "prod", "production"):
        return "live.ctraderapi.com"
    return "demo.ctraderapi.com"


@router.get("/auth/ctrader/login")
def ctrader_login(env: str = Query(default="demo")):
    """Start cTrader OAuth (also serves as Factor login)."""
    if not config.CTRADER_CLIENT_ID:
        raise HTTPException(500, "CTRADER_CLIENT_ID not configured")
    url = build_authorize_url(redirect_uri=_redirect_uri(), scope="trading")
    # stash env in cookie-less query via state is ideal; use simple redirect cookie later
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        "factor_oauth_env",
        env if env in ("demo", "live") else "demo",
        max_age=600,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


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

    env = request.cookies.get("factor_oauth_env") or "demo"
    host = _host_for_env(env)

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

    try:
        accounts = fetch_accounts_for_token(access_token=access, host=host)
    except Exception as e:
        # Still create user with empty accounts — UI can show error
        accounts = []
        print(f"⚠️  OAuth account list failed: {e}")

    account = pick_default_account(accounts, prefer_demo=(env == "demo"))
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
        raw_meta={"accounts": accounts, "selected": account},
    )

    # Default pref: candle XAU disabled until user enables (safe)
    existing = repos.list_prefs(user_id)
    if not existing:
        repos.upsert_pref(
            user_id=user_id,
            kind="candle",
            symbol="XAU/USD",
            enabled=False,
            max_concurrent=1,
            max_per_day=5,
            volume_lots=0.01,
            sl_points=1000,
            tp_points=1000,
        )

    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, user_id)
    resp.delete_cookie("factor_oauth_env", path="/")
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
