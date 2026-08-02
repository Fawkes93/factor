"""
Low-level cTrader Open API client (JSON over WebSocket, port 5036).

Docs: https://help.ctrader.com/open-api/
Demo host: demo.ctraderapi.com  · Live: live.ctraderapi.com
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from websocket import WebSocket, WebSocketTimeoutException, create_connection

# Payload types (ProtoOAPayloadType)
PT_APP_AUTH_REQ = 2100
PT_APP_AUTH_RES = 2101
PT_ACCOUNT_AUTH_REQ = 2102
PT_ACCOUNT_AUTH_RES = 2103
PT_NEW_ORDER_REQ = 2106
PT_SYMBOLS_LIST_REQ = 2114
PT_SYMBOLS_LIST_RES = 2115
PT_RECONCILE_REQ = 2124
PT_RECONCILE_RES = 2125
PT_EXECUTION_EVENT = 2126
PT_ORDER_ERROR_EVENT = 2132
PT_ERROR_RES = 2142
PT_HEARTBEAT = 51  # ProtoHeartbeatEvent (common)

# ProtoOAPositionStatus
POSITION_STATUS_OPEN = 1
POSITION_STATUS_CREATED = 2  # still treat as exposure if present

ORDER_TYPE_MARKET = 1
TRADE_SIDE_BUY = 1
TRADE_SIDE_SELL = 2

# Process-wide symbol map — avoid re-parsing 300+ symbols every order (RAM).
_symbol_map_cache: Dict[str, int] = {}
_symbol_map_lock = threading.Lock()
_symbol_map_account: Optional[int] = None


class CTraderError(Exception):
    pass


class CTraderClient:
    """
    Short-lived session: connect → app auth → account auth → trade → close.

    Safe for alert-driven use (one order at a time under a lock).
    Symbol list is cached process-wide after first successful load.
    """

    def __init__(
        self,
        *,
        host: str,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: int,
        timeout_sec: float = 20.0,
    ) -> None:
        self.host = host
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = int(account_id)
        self.timeout_sec = timeout_sec
        self._ws: Optional[WebSocket] = None
        self._symbol_ids: Dict[str, int] = {}

    def __enter__(self) -> "CTraderClient":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def connect(self) -> None:
        url = f"wss://{self.host}:5036"
        self._ws = create_connection(
            url,
            timeout=self.timeout_sec,
            sslopt={"cert_reqs": ssl.CERT_REQUIRED},
        )
        # Application auth
        res = self._request(
            PT_APP_AUTH_REQ,
            {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
            },
            expect=PT_APP_AUTH_RES,
        )
        if res.get("payloadType") == PT_ERROR_RES:
            raise CTraderError(f"app auth failed: {res}")

        # Account auth
        res = self._request(
            PT_ACCOUNT_AUTH_REQ,
            {
                "ctidTraderAccountId": self.account_id,
                "accessToken": self.access_token,
            },
            expect=PT_ACCOUNT_AUTH_RES,
        )
        if res.get("payloadType") == PT_ERROR_RES:
            raise CTraderError(f"account auth failed: {res}")

        self._load_symbols()

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Drop per-session reference; process cache keeps the map
        self._symbol_ids = {}

    def resolve_symbol_id(self, broker_symbol: str) -> int:
        name = broker_symbol.upper().replace("/", "")
        # Direct hit
        if name in self._symbol_ids:
            return self._symbol_ids[name]
        # Loose match: startswith / contains
        for k, sid in self._symbol_ids.items():
            if k.replace(".", "") == name or k.startswith(name):
                return sid
        # Try common variants
        for alt in (name, f"{name}.a", f"{name}.m", f"{name}m"):
            if alt in self._symbol_ids:
                return self._symbol_ids[alt]
        known = sorted(self._symbol_ids.keys())[:40]
        raise CTraderError(
            f"symbol {broker_symbol!r} not found on account "
            f"(sample: {known}…)"
        )

    def list_open_positions(self) -> list:
        """
        ProtoOAReconcileRes — open positions on this account.
        Returns list of position dicts (may be empty).
        """
        msg = self._request(
            PT_RECONCILE_REQ,
            {
                "ctidTraderAccountId": self.account_id,
                "returnProtectionOrders": False,
            },
            expect=PT_RECONCILE_RES,
        )
        if int(msg.get("payloadType") or 0) == PT_ERROR_RES:
            raise CTraderError(f"reconcile failed: {msg.get('payload') or msg}")
        payload = msg.get("payload") or {}
        positions = payload.get("position") or payload.get("positions") or []
        if not isinstance(positions, list):
            return []
        open_pos = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            # status: 1=OPEN; some JSON builds omit status when open
            status = p.get("positionStatus") or p.get("status")
            if status is None or int(status) in (
                POSITION_STATUS_OPEN,
                POSITION_STATUS_CREATED,
            ):
                vol = p.get("volume") or p.get("tradeData", {}).get("volume") or 0
                try:
                    if int(vol) == 0:
                        continue
                except (TypeError, ValueError):
                    pass
                open_pos.append(p)
        return open_pos

    def count_open_positions(self, broker_symbol: Optional[str] = None) -> int:
        """Count open positions, optionally filtered to broker_symbol (e.g. XAUUSD)."""
        positions = self.list_open_positions()
        if not broker_symbol:
            return len(positions)
        try:
            sid = self.resolve_symbol_id(broker_symbol)
        except CTraderError:
            # Unknown symbol on this account — treat as no matching positions
            return 0
        n = 0
        for p in positions:
            trade = p.get("tradeData") if isinstance(p.get("tradeData"), dict) else {}
            psid = p.get("symbolId") or trade.get("symbolId")
            try:
                if psid is not None and int(psid) == int(sid):
                    n += 1
            except (TypeError, ValueError):
                continue
        return n

    def place_market(
        self,
        *,
        broker_symbol: str,
        side: str,
        volume_protocol: int,
        relative_sl: int,
        relative_tp: int,
        client_order_id: str,
        comment: str = "",
        label: str = "factor",
    ) -> Dict[str, Any]:
        symbol_id = self.resolve_symbol_id(broker_symbol)
        trade_side = TRADE_SIDE_BUY if side.upper() == "BUY" else TRADE_SIDE_SELL
        payload: Dict[str, Any] = {
            "ctidTraderAccountId": self.account_id,
            "symbolId": symbol_id,
            "orderType": ORDER_TYPE_MARKET,
            "tradeSide": trade_side,
            "volume": int(volume_protocol),
            "clientOrderId": client_order_id[:50],
            "label": label[:100],
        }
        if comment:
            payload["comment"] = comment[:512]
        if relative_sl > 0:
            payload["relativeStopLoss"] = int(relative_sl)
        if relative_tp > 0:
            payload["relativeTakeProfit"] = int(relative_tp)

        # Execution comes as event 2126; errors as 2132 / 2142
        msg = self._request(
            PT_NEW_ORDER_REQ,
            payload,
            expect_any=(PT_EXECUTION_EVENT, PT_ORDER_ERROR_EVENT, PT_ERROR_RES),
        )
        pt = int(msg.get("payloadType") or 0)
        body = msg.get("payload") or {}
        if pt in (PT_ORDER_ERROR_EVENT, PT_ERROR_RES):
            raise CTraderError(f"order rejected: {body}")
        return body

    # --- internals ---

    def _load_symbols(self) -> None:
        global _symbol_map_cache, _symbol_map_account
        with _symbol_map_lock:
            if (
                _symbol_map_cache
                and _symbol_map_account == self.account_id
            ):
                self._symbol_ids = _symbol_map_cache
                return

        msg = self._request(
            PT_SYMBOLS_LIST_REQ,
            {
                "ctidTraderAccountId": self.account_id,
                "includeArchivedSymbols": False,
            },
            expect=PT_SYMBOLS_LIST_RES,
        )
        payload = msg.get("payload") or {}
        symbols = payload.get("symbol") or payload.get("symbols") or []
        mapping: Dict[str, int] = {}
        for s in symbols:
            if not isinstance(s, dict):
                continue
            name = (
                s.get("symbolName")
                or s.get("name")
                or s.get("symbol")
                or ""
            )
            sid = s.get("symbolId") or s.get("id")
            if name and sid is not None:
                mapping[str(name).upper()] = int(sid)
        # Free the raw list ASAP (large JSON payload)
        del symbols
        del payload
        del msg
        self._symbol_ids = mapping
        with _symbol_map_lock:
            _symbol_map_cache = mapping
            _symbol_map_account = self.account_id
        print(f"📡 cTrader symbols loaded: {len(mapping)} (cached)")

    def _request(
        self,
        payload_type: int,
        payload: Dict[str, Any],
        *,
        expect: Optional[int] = None,
        expect_any: Optional[Tuple[int, ...]] = None,
    ) -> Dict[str, Any]:
        client_msg_id = f"f-{uuid.uuid4().hex[:12]}"
        envelope = {
            "clientMsgId": client_msg_id,
            "payloadType": payload_type,
            "payload": payload,
        }
        assert self._ws is not None
        self._ws.send(json.dumps(envelope, separators=(",", ":")))
        deadline = time.time() + self.timeout_sec
        wanted = set()
        if expect is not None:
            wanted.add(expect)
        if expect_any:
            wanted.update(expect_any)
        if not wanted:
            wanted.add(expect or payload_type)

        while time.time() < deadline:
            try:
                raw = self._ws.recv()
            except WebSocketTimeoutException:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            pt = int(msg.get("payloadType") or 0)
            # Heartbeats — ignore (server may send; no strict reply required for short sessions)
            if pt == PT_HEARTBEAT:
                continue
            # Match our correlation id when present, or any expected payload type
            mid = msg.get("clientMsgId")
            if mid and mid != client_msg_id and pt not in (
                PT_EXECUTION_EVENT,
                PT_ORDER_ERROR_EVENT,
            ):
                # Unrelated message
                if pt == PT_ERROR_RES:
                    return msg
                continue
            if pt in wanted or pt == PT_ERROR_RES:
                return msg
        raise CTraderError(
            f"timeout waiting for response to payloadType={payload_type} "
            f"(wanted {sorted(wanted)})"
        )
