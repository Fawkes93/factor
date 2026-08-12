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
from typing import Any, Dict, List, Optional, Tuple

from websocket import WebSocket, WebSocketTimeoutException, create_connection

# Payload types (ProtoOAPayloadType)
PT_APP_AUTH_REQ = 2100
PT_APP_AUTH_RES = 2101
PT_ACCOUNT_AUTH_REQ = 2102
PT_ACCOUNT_AUTH_RES = 2103
PT_NEW_ORDER_REQ = 2106
PT_AMEND_POSITION_SLTP_REQ = 2110
PT_CLOSE_POSITION_REQ = 2111
PT_SYMBOLS_LIST_REQ = 2114
PT_SYMBOLS_LIST_RES = 2115
PT_RECONCILE_REQ = 2124
PT_RECONCILE_RES = 2125
PT_EXECUTION_EVENT = 2126
PT_SUBSCRIBE_SPOTS_REQ = 2127
PT_SUBSCRIBE_SPOTS_RES = 2128
PT_UNSUBSCRIBE_SPOTS_REQ = 2129
PT_UNSUBSCRIBE_SPOTS_RES = 2130
# Spotware: PROTO_OA_SPOT_EVENT = 2131 (was wrongly 2129 = unsubscribe req)
PT_SPOT_EVENT = 2131
PT_ORDER_ERROR_EVENT = 2132
PT_DEAL_LIST_REQ = 2133
PT_DEAL_LIST_RES = 2134
PT_ERROR_RES = 2142
PT_DEAL_LIST_BY_POSITION_ID_REQ = 2179
PT_DEAL_LIST_BY_POSITION_ID_RES = 2180
PT_GET_POSITION_UNREALIZED_PNL_REQ = 2187
PT_GET_POSITION_UNREALIZED_PNL_RES = 2188
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

    def close_position(self, *, position_id: int, volume: int) -> Dict[str, Any]:
        """
        Close (or partially close) a position.
        volume is protocol units (0.01 of base unit), same as order volume.
        """
        if volume <= 0:
            raise CTraderError(f"close volume must be > 0, got {volume}")
        payload = {
            "ctidTraderAccountId": self.account_id,
            "positionId": int(position_id),
            "volume": int(volume),
        }
        msg = self._request(
            PT_CLOSE_POSITION_REQ,
            payload,
            expect_any=(PT_EXECUTION_EVENT, PT_ORDER_ERROR_EVENT, PT_ERROR_RES),
        )
        pt = int(msg.get("payloadType") or 0)
        body = msg.get("payload") or {}
        if pt in (PT_ORDER_ERROR_EVENT, PT_ERROR_RES):
            raise CTraderError(f"close rejected: {body}")
        return body

    def amend_position_sl_tp(
        self,
        *,
        position_id: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        ProtoOAAmendPositionSLTPReq (2110) — absolute stopLoss / takeProfit prices.
        """
        payload: Dict[str, Any] = {
            "ctidTraderAccountId": self.account_id,
            "positionId": int(position_id),
        }
        if stop_loss is not None:
            payload["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            payload["takeProfit"] = float(take_profit)
        if "stopLoss" not in payload and "takeProfit" not in payload:
            raise CTraderError("amend requires stopLoss and/or takeProfit")
        msg = self._request(
            PT_AMEND_POSITION_SLTP_REQ,
            payload,
            expect_any=(PT_EXECUTION_EVENT, PT_ORDER_ERROR_EVENT, PT_ERROR_RES),
        )
        pt = int(msg.get("payloadType") or 0)
        body = msg.get("payload") or {}
        if pt in (PT_ORDER_ERROR_EVENT, PT_ERROR_RES):
            raise CTraderError(f"amend SL/TP rejected: {body}")
        return body

    @staticmethod
    def _normalize_spot_quote(raw: Any) -> Optional[float]:
        """
        ProtoOASpotEvent bid/ask are often uint64 in 1/100000 of price
        (e.g. 433952000 → 4339.52). Already-decimal prices pass through.
        """
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        # Scaled protocol ints are large; absolute gold ~4k, FX ~1.
        if v >= 1_000_000.0:
            v = v / 100_000.0
        return v

    def get_spot(self, broker_symbol: str) -> Dict[str, float]:
        """
        Subscribe to spots, wait for one ProtoOASpotEvent (2131), unsubscribe.
        Returns {bid, ask, symbol_id}.
        """
        symbol_id = self.resolve_symbol_id(broker_symbol)
        # Subscribe — RES (2128) then technical SPOT_EVENT (2131) with latest price
        sub = self._request(
            PT_SUBSCRIBE_SPOTS_REQ,
            {
                "ctidTraderAccountId": self.account_id,
                "symbolId": [int(symbol_id)],
            },
            expect_any=(PT_SUBSCRIBE_SPOTS_RES, PT_SPOT_EVENT, PT_ERROR_RES),
        )
        pt = int(sub.get("payloadType") or 0)
        if pt == PT_ERROR_RES:
            raise CTraderError(f"subscribe spots failed: {sub.get('payload') or sub}")

        # If subscribe response wasn't the spot, wait for spot event (2131)
        spot_msg = sub if pt == PT_SPOT_EVENT else self._wait_payload(
            wanted={PT_SPOT_EVENT, PT_ERROR_RES},
            timeout_sec=min(10.0, self.timeout_sec),
        )
        if int(spot_msg.get("payloadType") or 0) == PT_ERROR_RES:
            raise CTraderError(f"spot event error: {spot_msg.get('payload') or spot_msg}")
        body = spot_msg.get("payload") or {}
        bid = body.get("bid")
        ask = body.get("ask")
        if bid is None:
            bid = body.get("bidPrice") or body.get("b")
        if ask is None:
            ask = body.get("askPrice") or body.get("a")
        bid_f = self._normalize_spot_quote(bid)
        ask_f = self._normalize_spot_quote(ask)
        # One-sided quotes: mirror so BE mark still works
        if bid_f is None and ask_f is not None:
            bid_f = ask_f
        if ask_f is None and bid_f is not None:
            ask_f = bid_f
        if bid_f is None or ask_f is None:
            raise CTraderError(f"spot missing bid/ask: {body}")
        try:
            self._request(
                PT_UNSUBSCRIBE_SPOTS_REQ,
                {
                    "ctidTraderAccountId": self.account_id,
                    "symbolId": [int(symbol_id)],
                },
                expect_any=(
                    PT_UNSUBSCRIBE_SPOTS_RES,
                    PT_ERROR_RES,
                    PT_SPOT_EVENT,
                ),
            )
        except CTraderError:
            pass  # best-effort unsubscribe
        return {"bid": bid_f, "ask": ask_f, "symbol_id": int(symbol_id)}

    def list_deals_by_position_id(
        self,
        position_id: int,
        *,
        from_timestamp_ms: Optional[int] = None,
        to_timestamp_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        ProtoOADealListByPositionIdReq (2179) — deals for one position.
        Closing deals include closePositionDetail (grossProfit, etc.).
        """
        payload: Dict[str, Any] = {
            "ctidTraderAccountId": self.account_id,
            "positionId": int(position_id),
        }
        if from_timestamp_ms is not None:
            payload["fromTimestamp"] = int(from_timestamp_ms)
        if to_timestamp_ms is not None:
            payload["toTimestamp"] = int(to_timestamp_ms)
        msg = self._request(
            PT_DEAL_LIST_BY_POSITION_ID_REQ,
            payload,
            expect=PT_DEAL_LIST_BY_POSITION_ID_RES,
        )
        if int(msg.get("payloadType") or 0) == PT_ERROR_RES:
            raise CTraderError(f"deal list by position failed: {msg.get('payload') or msg}")
        body = msg.get("payload") or {}
        deals = body.get("deal") or body.get("deals") or []
        if not isinstance(deals, list):
            return []
        return [d for d in deals if isinstance(d, dict)]

    def list_open_positions_info(self) -> List[Dict[str, Any]]:
        """Normalized open positions for BE / management."""
        raw = self.list_open_positions()
        # Reverse symbol map for names
        id_to_name = {int(v): k for k, v in self._symbol_ids.items()}
        out: List[Dict[str, Any]] = []
        for p in raw:
            trade = p.get("tradeData") if isinstance(p.get("tradeData"), dict) else {}
            pid = p.get("positionId") or p.get("id")
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                continue
            side_raw = trade.get("tradeSide") or p.get("tradeSide") or p.get("side")
            try:
                side_i = int(side_raw) if side_raw is not None else 0
            except (TypeError, ValueError):
                side_i = 0
            if side_i == TRADE_SIDE_SELL or str(side_raw).upper() in ("SELL", "2"):
                side = "SELL"
            else:
                side = "BUY"
            entry = p.get("price") or p.get("entryPrice") or trade.get("price")
            sl = p.get("stopLoss") or p.get("stopLossPrice")
            tp = p.get("takeProfit") or p.get("takeProfitPrice")
            sid = p.get("symbolId") or trade.get("symbolId")
            broker_symbol = None
            try:
                if sid is not None:
                    broker_symbol = id_to_name.get(int(sid))
            except (TypeError, ValueError):
                broker_symbol = None
            out.append(
                {
                    "position_id": pid_i,
                    "side": side,
                    "entry_price": float(entry) if entry is not None else None,
                    "stop_loss": float(sl) if sl is not None else None,
                    "take_profit": float(tp) if tp is not None else None,
                    "symbol_id": int(sid) if sid is not None else None,
                    "broker_symbol": broker_symbol,
                    "volume": p.get("volume") or trade.get("volume"),
                    "raw": p,
                }
            )
        return out

    def close_all_positions(self) -> list:
        """
        Close every open position. Returns list of result dicts:
        {position_id, ok, message, detail?}
        """
        positions = self.list_open_positions()
        results = []
        for p in positions:
            trade = p.get("tradeData") if isinstance(p.get("tradeData"), dict) else {}
            pid = p.get("positionId") or p.get("id")
            vol = p.get("volume") or trade.get("volume") or 0
            try:
                pid_i = int(pid)
                vol_i = int(vol)
            except (TypeError, ValueError):
                results.append(
                    {
                        "position_id": pid,
                        "ok": False,
                        "message": "invalid position id/volume",
                    }
                )
                continue
            if vol_i <= 0:
                results.append(
                    {
                        "position_id": pid_i,
                        "ok": False,
                        "message": "zero volume",
                    }
                )
                continue
            try:
                body = self.close_position(position_id=pid_i, volume=vol_i)
                results.append(
                    {
                        "position_id": pid_i,
                        "ok": True,
                        "message": "closed",
                        "detail": body,
                    }
                )
                print(f"🛑 Closed position {pid_i} vol={vol_i}")
            except CTraderError as e:
                results.append(
                    {
                        "position_id": pid_i,
                        "ok": False,
                        "message": str(e),
                    }
                )
                print(f"⚠️  Close position {pid_i} failed: {e}")
        return results

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

    def _wait_payload(
        self,
        *,
        wanted: set,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        assert self._ws is not None
        deadline = time.time() + float(timeout_sec if timeout_sec is not None else self.timeout_sec)
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
            if pt == PT_HEARTBEAT:
                continue
            if pt in wanted or pt == PT_ERROR_RES:
                return msg
        raise CTraderError(f"timeout waiting for payload types {sorted(wanted)}")

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
        wanted = set()
        if expect is not None:
            wanted.add(expect)
        if expect_any:
            wanted.update(expect_any)
        if not wanted:
            wanted.add(expect or payload_type)

        deadline = time.time() + self.timeout_sec
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
            # Spots may arrive without our clientMsgId
            if pt == PT_SPOT_EVENT and PT_SPOT_EVENT in wanted:
                return msg
            # Match our correlation id when present, or any expected payload type
            mid = msg.get("clientMsgId")
            if mid and mid != client_msg_id and pt not in (
                PT_EXECUTION_EVENT,
                PT_ORDER_ERROR_EVENT,
                PT_SPOT_EVENT,
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
