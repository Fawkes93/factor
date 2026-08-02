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


def pick_default_account(
    accounts: List[Dict[str, Any]],
    *,
    prefer_demo: bool = True,
) -> Optional[Dict[str, Any]]:
    if not accounts:
        return None
    if prefer_demo:
        for a in accounts:
            # isLive is False for demo on many payloads
            if a.get("isLive") is False or str(a.get("environment", "")).lower() == "demo":
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
