"""
Pull missed alerts from Reaktor GET /api/v1/alerts?since_id=…

Used on boot and on a background poll loop (backup for webhooks).

Memory: small page size (CATCHUP_LIMIT), no result list accumulation,
gc after batch — safe for Render 512MB.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from factor import config
from factor.broker.base import Broker
from factor.engine import process_alert
from factor.inbound.models import parse_cloudevent
from factor.memory_util import maybe_gc
from factor.store.state import StateStore

_poll_stop = threading.Event()
_poll_thread: Optional[threading.Thread] = None
_last_poll: Dict[str, Any] = {"at": None, "ok": None, "count": 0, "error": None}


def last_poll_status() -> Dict[str, Any]:
    return dict(_last_poll)


def fetch_alerts_since(
    since_id: int,
    *,
    base_url: str,
    api_key: str,
    limit: Optional[int] = None,
    kind: Optional[str] = None,
    symbol: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not base_url or not api_key:
        return []
    lim = int(limit if limit is not None else getattr(config, "CATCHUP_LIMIT", 50) or 50)
    lim = max(1, min(lim, 100))
    url = f"{base_url.rstrip('/')}/api/v1/alerts"
    headers = {"Authorization": f"Bearer {api_key}"}
    params: Dict[str, Any] = {"since_id": since_id, "limit": lim}
    if kind:
        params["kind"] = kind
    if symbol:
        params["symbol"] = symbol
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        body = resp.json()
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return []
    return events


def run_catchup(
    store: StateStore,
    broker: Broker,
    *,
    force: bool = False,
    quiet: bool = False,
) -> Dict[str, Any]:
    """
    Pull and process alerts since last_processed_alert_id.

    force=True: used by background poll (ignores CATCHUP_ON_START).
    Does not retain per-alert result dicts (memory).
    """
    if not force and not config.CATCHUP_ON_START:
        return {"skipped": True, "reason": "CATCHUP_ON_START=false"}
    if not config.REAKTOR_BASE_URL or not config.REAKTOR_API_KEY:
        return {
            "skipped": True,
            "reason": "REAKTOR_BASE_URL or REAKTOR_API_KEY not set",
        }

    since = store.last_processed_alert_id()
    # If only one kind/symbol configured, narrow the pull; engine still re-filters.
    kind = config.TRADE_KINDS[0] if len(config.TRADE_KINDS) == 1 else None
    symbol = config.TRADE_SYMBOLS[0] if len(config.TRADE_SYMBOLS) == 1 else None

    if not quiet:
        print(f"🔄 Catch-up from Reaktor since_id={since} …")
    try:
        events = fetch_alerts_since(
            since,
            base_url=config.REAKTOR_BASE_URL,
            api_key=config.REAKTOR_API_KEY,
            kind=kind,
            symbol=symbol,
        )
    except Exception as e:
        if not quiet:
            print(f"⚠️  Catch-up failed: {e}")
        _last_poll.update(
            {
                "at": time.time(),
                "ok": False,
                "count": 0,
                "error": str(e),
                "since_id": since,
            }
        )
        return {"ok": False, "error": str(e), "since_id": since}

    count = 0
    skipped = 0
    traded = 0
    for ev in events:
        try:
            alert = parse_cloudevent(ev)
        except Exception as e:
            if not quiet:
                print(f"⚠️  Catch-up parse skip: {e}")
            skipped += 1
            continue
        result = process_alert(alert, store=store, broker=broker)
        count += 1
        if result.get("action") == "traded":
            traded += 1
        elif result.get("action") == "skip":
            skipped += 1
        # Drop large event payload ASAP
        del alert
        del result

    del events
    maybe_gc(float(getattr(config, "MEMORY_WARN_MB", 380) or 380))

    if not quiet or count:
        print(
            f"🔄 Catch-up done: {count} event(s) since_id={since} "
            f"(traded={traded} skipped≈{skipped})"
        )

    _last_poll.update(
        {
            "at": time.time(),
            "ok": True,
            "count": count,
            "error": None,
            "since_id": since,
            "traded": traded,
        }
    )
    return {
        "ok": True,
        "since_id": since,
        "count": count,
        "traded": traded,
    }


def start_poll_loop(store: StateStore, broker: Broker) -> bool:
    """
    Background thread: pull Reaktor every POLL_INTERVAL_SEC.
    Returns True if started.
    """
    global _poll_thread
    interval = int(config.POLL_INTERVAL_SEC or 0)
    if interval <= 0:
        print("ℹ️  POLL_INTERVAL_SEC=0 — background pull disabled")
        return False
    if not config.REAKTOR_BASE_URL or not config.REAKTOR_API_KEY:
        print("ℹ️  Poll loop not started (set REAKTOR_BASE_URL + REAKTOR_API_KEY)")
        return False
    if _poll_thread and _poll_thread.is_alive():
        return True

    _poll_stop.clear()

    def _loop() -> None:
        print(f"🔁 Poll loop every {interval}s → {config.REAKTOR_BASE_URL}")
        # First tick after interval (boot catch-up already ran)
        while not _poll_stop.wait(interval):
            try:
                run_catchup(store, broker, force=True, quiet=True)
            except Exception as e:
                print(f"⚠️  Poll loop error: {e}")

    _poll_thread = threading.Thread(
        target=_loop, name="factor-poll", daemon=True
    )
    _poll_thread.start()
    return True


def stop_poll_loop() -> None:
    _poll_stop.set()
