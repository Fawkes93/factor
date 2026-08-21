"""
cTrader Open API OAuth (login + trading tokens).

Grant URL + token exchange (REST). Account list via short WebSocket session.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from factor import config
from factor.broker.ctrader_client import (
    CTraderClient,
    CTraderError,
    PT_APP_AUTH_REQ,
    PT_APP_AUTH_RES,
    PT_ERROR_RES,
)

# ProtoOAGetAccountListByAccessToken
PT_GET_ACCOUNTS_BY_TOKEN_REQ = 2149
PT_GET_ACCOUNTS_BY_TOKEN_RES = 2150

TOKEN_URL = "https://openapi.ctrader.com/apps/token"
GRANT_BASE = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"


def build_authorize_url(*, redirect_uri: str, scope: str = "trading") -> str:
    if not config.CTRADER_CLIENT_ID:
        raise RuntimeError("CTRADER_CLIENT_ID not set")
    q = urlencode(
        {
            "client_id": config.CTRADER_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "product": "web",
        }
    )
    return f"{GRANT_BASE}?{q}"


def exchange_code_for_tokens(
    *,
    code: str,
    redirect_uri: str,
) -> Dict[str, Any]:
    """
    Exchange authorization code for access + refresh tokens.
    Returns dict with accessToken, refreshToken, expiresIn, …
    """
    if not config.CTRADER_CLIENT_ID or not config.CTRADER_CLIENT_SECRET:
        raise RuntimeError("CTRADER_CLIENT_ID/SECRET not set")
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.CTRADER_CLIENT_ID,
        "client_secret": config.CTRADER_CLIENT_SECRET,
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(TOKEN_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    if data.get("errorCode"):
        raise RuntimeError(
            f"token exchange failed: {data.get('errorCode')} {data.get('description')}"
        )
    if not data.get("accessToken"):
        raise RuntimeError(f"token exchange missing accessToken: {data}")
    return data


def refresh_access_token(*, refresh_token: str) -> Dict[str, Any]:
    if not config.CTRADER_CLIENT_ID or not config.CTRADER_CLIENT_SECRET:
        raise RuntimeError("CTRADER_CLIENT_ID/SECRET not set")
    params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.CTRADER_CLIENT_ID,
        "client_secret": config.CTRADER_CLIENT_SECRET,
    }
    with httpx.Client(timeout=20.0) as client:
        # Spotware docs show GET and POST; GET is widely used
        resp = client.get(TOKEN_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    if data.get("errorCode") or not data.get("accessToken"):
        raise RuntimeError(f"refresh failed: {data}")
    return data


def fetch_accounts_for_token(
    *,
    access_token: str,
    host: str,
) -> List[Dict[str, Any]]:
    """
    Return list of trading accounts granted to this access token.
    Uses app-auth WebSocket + GetAccountListByAccessToken.
    """
    client = CTraderClient(
        host=host,
        client_id=config.CTRADER_CLIENT_ID,
        client_secret=config.CTRADER_CLIENT_SECRET,
        access_token=access_token,
        account_id=0,  # not needed for app auth + account list
    )
    # Partial connect: only app auth, not account auth
    url = f"wss://{host}:5036"
    import ssl
    from websocket import create_connection

    client._ws = create_connection(
        url,
        timeout=20.0,
        sslopt={"cert_reqs": ssl.CERT_REQUIRED},
    )
    try:
        res = client._request(
            PT_APP_AUTH_REQ,
            {
                "clientId": config.CTRADER_CLIENT_ID,
                "clientSecret": config.CTRADER_CLIENT_SECRET,
            },
            expect=PT_APP_AUTH_RES,
        )
        if res.get("payloadType") == PT_ERROR_RES:
            raise CTraderError(f"app auth failed: {res}")

        msg = client._request(
            PT_GET_ACCOUNTS_BY_TOKEN_REQ,
            {"accessToken": access_token},
            expect=PT_GET_ACCOUNTS_BY_TOKEN_RES,
        )
        if int(msg.get("payloadType") or 0) == PT_ERROR_RES:
            raise CTraderError(f"account list failed: {msg}")
        payload = msg.get("payload") or {}
        accounts = (
            payload.get("ctidTraderAccount")
            or payload.get("ctidTraderAccounts")
            or []
        )
        if not isinstance(accounts, list):
            return []
        return [a for a in accounts if isinstance(a, dict)]
    finally:
        client.close()


def _host_is_live(host: str) -> bool:
    return str(host or "").lower().startswith("live")


def account_is_live(account: Optional[Dict[str, Any]]) -> bool:
    """True for live/real accounts; False for demo."""
    if not account:
        return False
    if account.get("_is_live") is True or account.get("isLive") is True:
        return True
    if account.get("_is_live") is False or account.get("isLive") is False:
        return False
    env = str(account.get("environment") or "").lower()
    if env in ("live", "prod", "production", "real"):
        return True
    if env == "demo":
        return False
    return _host_is_live(str(account.get("_host") or ""))


def pick_default_account(
    accounts: List[Dict[str, Any]],
    *,
    prefer_demo: bool = True,
) -> Optional[Dict[str, Any]]:
    if not accounts:
        return None
    if prefer_demo:
        for a in accounts:
            if not account_is_live(a):
                return a
    return accounts[0]


def account_id_from_row(row: Dict[str, Any]) -> Optional[int]:
    for key in ("ctidTraderAccountId", "accountId", "traderAccountId", "id"):
        if row.get(key) is not None:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                continue
    return None


def trader_login_from_row(row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    raw = row.get("traderLogin") or row.get("trader_login")
    if raw is None or raw == "":
        return None
    return str(raw)


def broker_name_from_row(row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    for key in (
        "brokerTitleTitle",
        "brokerTitle",
        "brokerName",
        "broker",
        "broker_name",
    ):
        val = row.get(key)
        if val:
            return str(val)
    return None


def host_for_account(account: Optional[Dict[str, Any]]) -> str:
    """Pick demo/live API host from account flags."""
    if not account:
        return "demo.ctraderapi.com"
    annotated = account.get("_host")
    if annotated:
        return str(annotated)
    if account_is_live(account):
        return "live.ctraderapi.com"
    return "demo.ctraderapi.com"


def _annotate_account(account: Dict[str, Any], host: str) -> Dict[str, Any]:
    a = dict(account)
    live = account_is_live(a) if (
        a.get("isLive") is not None or a.get("environment")
    ) else _host_is_live(host)
    a["_is_live"] = bool(live)
    a["_host"] = "live.ctraderapi.com" if live else "demo.ctraderapi.com"
    # Prefer the host we actually queried when flags are missing
    if a.get("isLive") is None and not a.get("environment"):
        a["_host"] = host
        a["_is_live"] = _host_is_live(host)
    return a


def fetch_accounts_all_hosts(*, access_token: str) -> List[Dict[str, Any]]:
    """
    Query demo AND live hosts and merge by ctidTraderAccountId.

    Each row is annotated with `_host` and `_is_live`. Previously Factor
    stopped at the first host that returned anything, so live accounts were
    dropped whenever a demo account existed.
    """
    seen: Dict[int, Dict[str, Any]] = {}
    for host in ("demo.ctraderapi.com", "live.ctraderapi.com"):
        try:
            accounts = fetch_accounts_for_token(access_token=access_token, host=host)
        except Exception as e:
            print(f"⚠️  account list on {host}: {e}")
            continue
        for raw in accounts:
            aid = account_id_from_row(raw)
            if aid is None:
                continue
            annotated = _annotate_account(raw, host)
            prev = seen.get(aid)
            if prev is None:
                seen[aid] = annotated
                continue
            # Prefer the host that matches isLive when both hosts return it
            want_live = account_is_live(annotated)
            new_matches = _host_is_live(annotated["_host"]) == want_live
            prev_matches = _host_is_live(prev.get("_host") or "") == want_live
            if new_matches and not prev_matches:
                seen[aid] = annotated
    return list(seen.values())


def fetch_accounts_any_host(*, access_token: str) -> tuple:
    """
    Legacy: first non-empty host. Prefer fetch_accounts_all_hosts.
    Returns (accounts, host_used).
    """
    accounts = fetch_accounts_all_hosts(access_token=access_token)
    if not accounts:
        return [], "demo.ctraderapi.com"
    host = str(accounts[0].get("_host") or "demo.ctraderapi.com")
    return accounts, host