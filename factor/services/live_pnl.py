"""
Live unrealized P/L and deposit currency.

Each broker read for an account runs at most once per 60 seconds.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from factor.db import repos

_TTL_SEC = 60.0
_lock = threading.Lock()
# (kind, account_id) -> Event while a fetch is in flight
_inflight: Dict[Tuple[str, int], threading.Event] = {}
# account_id -> (monotonic timestamp, position_id -> {net, gross})
_pnl_cache: Dict[int, Tuple[float, Dict[str, Dict[str, float]]]] = {}
# account_id -> (monotonic timestamp, currency or None)
_ccy_cache: Dict[int, Tuple[float, Optional[str]]] = {}


def reset_caches() -> None:
    with _lock:
        _inflight.clear()
        _pnl_cache.clear()
        _ccy_cache.clear()


def _cached(
    kind: str,
    account_id: int,
    cache: Dict[int, Tuple[float, Any]],
    producer: Callable[[], Any],
    *,
    now: Callable[[], float],
    empty: Any,
) -> Any:
    """Return a cached value, fetching at most once per TTL per account."""
    t = float(now())
    key = (kind, int(account_id))
    with _lock:
        hit = cache.get(int(account_id))
        if hit is not None and t - hit[0] < _TTL_SEC:
            return hit[1]
        ev = _inflight.get(key)
        if ev is None:
            ev = threading.Event()
            _inflight[key] = ev
            leader = True
        else:
            leader = False
    if not leader:
        ev.wait(8.0)
        with _lock:
            hit = cache.get(int(account_id))
            return hit[1] if hit is not None else empty
    try:
        try:
            value = producer()
        except Exception as e:
            print(f"⚠️  {kind} account {account_id}: {e}")
            value = empty
        if value is None:
            value = empty
        with _lock:
            cache[int(account_id)] = (t, value)
        return value
    finally:
        with _lock:
            _inflight.pop(key, None)
        ev.set()


def unrealized_for_connection(
    conn: Optional[Dict[str, Any]],
    *,
    fetch: Optional[Callable[[Dict[str, Any]], Dict[str, Dict[str, float]]]] = None,
    now: Optional[Callable[[], float]] = None,
) -> Dict[str, Dict[str, float]]:
    """Map broker position id -> {net, gross}. Empty when unknown."""
    if not conn or conn.get("account_id") is None:
        return {}
    aid = int(conn["account_id"])
    clock = now or time.time

    def produce() -> Dict[str, Dict[str, float]]:
        raw = fetch(conn) if fetch else _fetch_unrealized(conn)
        out: Dict[str, Dict[str, float]] = {}
        for key, row in (raw or {}).items():
            if not isinstance(row, dict) or row.get("net") is None:
                continue
            out[str(key)] = {
                "net": float(row["net"]),
                "gross": float(row["gross"]) if row.get("gross") is not None else None,
            }
        return out

    value = _cached("pnl", aid, _pnl_cache, produce, now=clock, empty={})
    return dict(value or {})


def deposit_currency_for(
    conn: Optional[Dict[str, Any]],
    *,
    fetch: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    now: Optional[Callable[[], float]] = None,
) -> Optional[str]:
    """
    Deposit currency code. Uses the connection row when it is already stored.
    A missing value is fetched at most once per 60s and written back.
    """
    if not conn or conn.get("account_id") is None:
        return None
    existing = str(conn.get("deposit_currency") or "").strip().upper()
    if existing:
        return existing
    aid = int(conn["account_id"])
    clock = now or time.time

    def produce() -> Optional[str]:
        raw = fetch(conn) if fetch else _fetch_currency(conn)
        code = str(raw or "").strip().upper() or None
        if code:
            uid = conn.get("user_id")
            if uid:
                try:
                    repos.set_deposit_currency(str(uid), aid, code)
                except Exception as e:
                    print(f"⚠️  deposit_currency save account {aid}: {e}")
            conn["deposit_currency"] = code
        return code

    return _cached("ccy", aid, _ccy_cache, produce, now=clock, empty=None)


def _open_client(conn: Dict[str, Any]):
    from factor import config
    from factor.broker.ctrader_client import CTraderClient
    from factor.crypto.tokens import decrypt_token

    if config.CTRADER_DRY_RUN:
        return None
    if not (
        config.CTRADER_CLIENT_ID
        and config.CTRADER_CLIENT_SECRET
        and config.FACTOR_TOKEN_ENC_KEY
    ):
        return None
    access = decrypt_token(conn["access_token_enc"], config.FACTOR_TOKEN_ENC_KEY)
    return CTraderClient(
        host=str(conn.get("host") or "demo.ctraderapi.com"),
        client_id=config.CTRADER_CLIENT_ID,
        client_secret=config.CTRADER_CLIENT_SECRET,
        access_token=access,
        account_id=int(conn["account_id"]),
        timeout_sec=8.0,
    )


def _fetch_unrealized(conn: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    client = _open_client(conn)
    if client is None:
        return {}
    with client:
        rows = client.get_unrealized_pnl()
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        out[str(row["position_id"])] = {"net": row["net"], "gross": row["gross"]}
    return out


def _fetch_currency(conn: Dict[str, Any]) -> Optional[str]:
    client = _open_client(conn)
    if client is None:
        return None
    with client:
        return client.get_deposit_currency()
