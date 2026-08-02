"""Supabase client (service role) for Factor backend."""

from __future__ import annotations

from typing import Any, Optional

from factor import config

_client: Any = None


def supabase_configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_ROLE_KEY)


def get_supabase():
    """
    Lazy singleton Supabase client.
    Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
    """
    global _client
    if _client is not None:
        return _client
    if not supabase_configured():
        raise RuntimeError(
            "Supabase not configured — set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
        )
    from supabase import create_client

    _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
    return _client


def reset_client() -> None:
    """Test helper."""
    global _client
    _client = None
