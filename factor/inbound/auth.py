"""
HMAC verification for Reaktor webhooks.

Reaktor signs: sha256=hex(HMAC-SHA256(secret, raw_body))
Header: X-Reaktor-Signature
"""

from __future__ import annotations

import hashlib
import hmac


def sign_body(secret: str, body: bytes) -> str:
    dig = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={dig}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret:
        return False
    expected = sign_body(secret, body)
    return hmac.compare_digest(expected, (header or "").strip())
