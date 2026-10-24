"""
HTTP API for the AI orchestrator.

Auth is the same admin token as /api/kill (Bearer or X-Api-Key). These
routes stay closed when FACTOR_ADMIN_TOKEN is unset.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from factor import config
from factor.db import repos
from factor.services.ai_orders import (
    AI_ORDER_LABEL,
    AiOrderError,
    ai_kill_status,
    list_ai_positions,
    manage_ai_position,
    place_ai_order,
)

router = APIRouter(tags=["ai"])


def _require_ai_auth(
    authorization: Optional[str], x_api_key: Optional[str]
) -> None:
    token = config.FACTOR_ADMIN_TOKEN
    if not token:
        raise HTTPException(
            status_code=503, detail="FACTOR_ADMIN_TOKEN not configured"
        )
    got = None
    if authorization and authorization.lower().startswith("bearer "):
        got = authorization[7:].strip()
    elif x_api_key:
        got = x_api_key.strip()
    if not got or not secrets.compare_digest(got, token):
        raise HTTPException(status_code=401, detail="admin auth required")


def _error(exc: AiOrderError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise AiOrderError("invalid JSON")
    if not isinstance(payload, dict):
        raise AiOrderError("JSON object required")
    return payload


@router.post("/api/ai/orders")
@router.post("/api/orders")
async def post_ai_order(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Any:
    _require_ai_auth(authorization, x_api_key)
    try:
        payload = await _json_object(request)
        return place_ai_order(payload)
    except AiOrderError as exc:
        return _error(exc)


@router.post("/api/ai/positions/manage")
@router.post("/api/positions/manage")
async def post_ai_manage(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Any:
    _require_ai_auth(authorization, x_api_key)
    try:
        payload = await _json_object(request)
        return manage_ai_position(payload)
    except AiOrderError as exc:
        return _error(exc)


@router.get("/api/ai/positions")
@router.get("/api/positions")
def get_ai_positions(
    label: str = AI_ORDER_LABEL,
    account_id: Optional[int] = None,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Any:
    _require_ai_auth(authorization, x_api_key)
    try:
        return list_ai_positions(label=label, account_id=account_id)
    except AiOrderError as exc:
        return _error(exc)


@router.get("/api/ai/subscribers")
def get_ai_subscribers(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Any:
    """Accounts that turned the AI trader alert on and are not killed."""
    _require_ai_auth(authorization, x_api_key)
    try:
        subscribers = repos.list_ai_subscribers()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"subscribers failed: {exc}") from exc
    return {"success": True, "count": len(subscribers), "subscribers": subscribers}


@router.get("/api/ai/kill")
def get_ai_kill(
    account_id: Optional[int] = None,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Any:
    _require_ai_auth(authorization, x_api_key)
    try:
        return ai_kill_status(account_id)
    except AiOrderError as exc:
        return _error(exc)
