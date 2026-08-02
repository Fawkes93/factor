"""Signed HTTP-only session cookies (cTrader OAuth identity)."""

from __future__ import annotations

from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from factor import config

COOKIE_NAME = "factor_session"
MAX_AGE_SEC = 60 * 60 * 24 * 14  # 14 days


def _serializer() -> URLSafeTimedSerializer:
    secret = config.FACTOR_SESSION_SECRET or config.FACTOR_TOKEN_ENC_KEY
    if not secret:
        raise RuntimeError("FACTOR_SESSION_SECRET (or FACTOR_TOKEN_ENC_KEY) required")
    return URLSafeTimedSerializer(secret, salt="factor-session-v1")


def create_session_token(user_id: str) -> str:
    return _serializer().dumps({"uid": user_id})


def decode_session_token(token: str, *, max_age: int = MAX_AGE_SEC) -> Optional[str]:
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return str(uid) if uid else None


def read_session_user_id(request: Request) -> Optional[str]:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return decode_session_token(raw)
    except RuntimeError:
        return None


def set_session_cookie(response: Response, user_id: str) -> None:
    token = create_session_token(user_id)
    # Render sets PORT; use secure cookies in production
    import os

    secure = bool(os.getenv("PORT") or os.getenv("RENDER"))
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
