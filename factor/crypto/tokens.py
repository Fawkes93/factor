"""Encrypt/decrypt cTrader tokens at rest (Fernet)."""

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _fernet_from_key(raw: str) -> Fernet:
    """
    Accept either a standard Fernet key, or any passphrase (hashed to 32 bytes).
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("FACTOR_TOKEN_ENC_KEY is empty")
    try:
        # Valid Fernet key is urlsafe b64 of 32 bytes
        return Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode("utf-8")


def encrypt_token(plaintext: str, key: str) -> str:
    if plaintext is None:
        raise ValueError("plaintext token is required")
    f = _fernet_from_key(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str, key: str) -> str:
    if not ciphertext:
        raise ValueError("ciphertext is required")
    f = _fernet_from_key(key)
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("token decrypt failed — wrong FACTOR_TOKEN_ENC_KEY?") from e
