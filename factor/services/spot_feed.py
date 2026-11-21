"""One cTrader spot stream per account. Callers read the latest bid and ask."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from factor.broker.ctrader_client import (
    PT_ERROR_RES,
    PT_SPOT_EVENT,
    CTraderError,
    parse_spot_event,
)

_STALE_SEC = 120.0
_HEARTBEAT_SEC = 10.0
_READ_SEC = 5.0

_feeds_lock = threading.Lock()
_feeds: Dict[tuple, "SpotFeed"] = {}


class SpotFeed:
    """Stay subscribed. A dropped socket reconnects. Orders use their own sessions."""

    def __init__(self, factory: Callable[[], Any], symbol: str) -> None:
        self._symbol = symbol.upper()
        self._factory = factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._at = 0.0
        self._last_error = ""

    def set_factory(self, factory: Callable[[], Any]) -> None:
        with self._lock:
            self._factory = factory

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name=f"ctrader-spot-{self._symbol}",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def quote(self, *, timeout_sec: float = 20.0) -> Dict[str, float]:
        self.start()
        if not self._ready.wait(timeout_sec):
            raise CTraderError(self._last_error or "spot timed out")
        with self._lock:
            age = time.monotonic() - self._at
            bid = self._bid
            ask = self._ask
        if bid is None or ask is None or age > _STALE_SEC:
            raise CTraderError(self._last_error or "spot is stale")
        return {"bid": bid, "ask": ask, "age_sec": round(age, 3)}

    def _factory_call(self) -> Any:
        with self._lock:
            factory = self._factory
        return factory()

    def _store(self, bid: float, ask: float) -> None:
        with self._lock:
            self._bid = float(bid)
            self._ask = float(ask)
            self._at = time.monotonic()
            self._last_error = ""
        self._ready.set()

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            client = None
            try:
                client = self._factory_call()
                client.connect()
                first = client.subscribe_spots(self._symbol)
                if first:
                    self._store(float(first["bid"]), float(first["ask"]))
                backoff = 1.0
                last_hb = time.monotonic()
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now - last_hb >= _HEARTBEAT_SEC:
                        client.send_heartbeat()
                        last_hb = now
                    msg = client.read_message(_READ_SEC)
                    if not msg:
                        continue
                    pt = int(msg.get("payloadType") or 0)
                    if pt == PT_ERROR_RES:
                        self._last_error = "spot stream error"
                        break
                    if pt != PT_SPOT_EVENT:
                        continue
                    parsed = parse_spot_event(msg)
                    self._store(float(parsed["bid"]), float(parsed["ask"]))
            except Exception as exc:
                self._last_error = str(exc)[:300]
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass


def live_quote(broker: Any, symbol: str, *, timeout_sec: float = 20.0) -> Dict[str, float]:
    """Latest bid and ask for this broker account. The first call opens the stream."""
    key = (
        str(getattr(broker, "_host", "") or ""),
        str(getattr(broker, "_account_id", "") or ""),
        symbol.upper(),
    )
    with _feeds_lock:
        feed = _feeds.get(key)
        if feed is None:
            feed = SpotFeed(lambda: broker._new_client(), symbol)
            _feeds[key] = feed
        else:
            feed.set_factory(lambda: broker._new_client())
    return feed.quote(timeout_sec=timeout_sec)


def reset_live_quotes() -> None:
    with _feeds_lock:
        feeds = list(_feeds.values())
        _feeds.clear()
    for feed in feeds:
        feed.stop()
