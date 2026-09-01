"""
cTrader Open API broker (any Spotware/cTrader broker, e.g. IC Markets).

Places market orders with relative SL/TP (demo or live host).
Supports env credentials (single-tenant) or per-user constructor args (multi-user).
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from factor import config
from factor.broker.base import OrderResult
from factor.broker.ctrader_client import CTraderClient, CTraderError
from factor.rules.presets import OrderPlan, price_decimals, resolve_preset


def lots_to_protocol_volume(lots: float, lot_units: float) -> int:
    """
    cTrader volume = units of base asset × 100.
    e.g. 0.01 lot × 100 oz/lot = 1 oz → volume 100.
    """
    units = float(lots) * float(lot_units)
    vol = int(round(units * 100))
    if vol <= 0:
        raise ValueError(f"volume too small: lots={lots} lot_units={lot_units}")
    return vol


def price_distance_to_relative(distance: float, point_size: float = 0.01) -> int:
    """
    relativeStopLoss/TP = price distance × 100_000.

    Round the distance to symbol digits *before* scaling. Scaling a messy
    float first (e.g. |3456.789 − 3466.79| → 1_000_100) is rejected as
    'Relative stop loss has invalid precision' on 2-digit XAU.
    """
    digits = price_decimals(point_size)
    dist = round(abs(float(distance)), digits)
    rel = int(round(dist * 100_000))
    step = max(1, int(round(abs(float(point_size)) * 100_000)))
    if step > 1:
        rel = int(round(rel / step)) * step
    if rel <= 0:
        raise ValueError(f"relative distance too small: {distance}")
    return rel


class CTraderBroker:
    name = "ctrader"

    def __init__(
        self,
        *,
        access_token: Optional[str] = None,
        account_id: Optional[str] = None,
        host: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._access_token = (access_token if access_token is not None else config.CTRADER_ACCESS_TOKEN) or ""
        self._account_id = str(account_id if account_id is not None else config.CTRADER_ACCOUNT_ID or "")
        self._host = (host if host is not None else config.CTRADER_HOST) or "demo.ctraderapi.com"
        self._client_id = (client_id if client_id is not None else config.CTRADER_CLIENT_ID) or ""
        self._client_secret = (
            client_secret if client_secret is not None else config.CTRADER_CLIENT_SECRET
        ) or ""
        self._dry_run = config.CTRADER_DRY_RUN if dry_run is None else bool(dry_run)
        self._open_cache_at = 0.0
        self._open_cache_key: Optional[str] = None
        self._open_cache_val = False
        self._open_count_cache = 0

    def configured(self) -> bool:
        return bool(
            self._client_id
            and self._client_secret
            and self._access_token
            and self._account_id
        )

    def count_open_positions(self, broker_symbol: Optional[str] = None) -> int:
        """Number of open positions on this connection (optional symbol filter)."""
        if not self.configured():
            return 0
        key = (broker_symbol or "").upper()
        now = time.time()
        with self._lock:
            if self._open_cache_key == key and now - self._open_cache_at < 2.0:
                return int(self._open_count_cache)
            try:
                with CTraderClient(
                    host=self._host,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                    access_token=self._access_token,
                    account_id=int(self._account_id),
                ) as client:
                    n = client.count_open_positions(broker_symbol or None)
                    print(f"📡 cTrader open positions: {n} account={self._account_id}")
            except CTraderError as e:
                print(f"⚠️  count_open_positions failed: {e}")
                # Live fail closed as "busy"; dry-run fail open
                n = 999 if not self._dry_run else 0
            except Exception as e:
                print(f"⚠️  count_open_positions unexpected: {e}")
                n = 999 if not self._dry_run else 0
            self._open_cache_at = now
            self._open_cache_key = key
            self._open_count_cache = n
            self._open_cache_val = n > 0
            return n

    def has_open_position(self, broker_symbol: Optional[str] = None) -> bool:
        return self.count_open_positions(broker_symbol) > 0

    def close_all_positions(self) -> list:
        """
        Close all open positions on this account.
        Returns list of {position_id, ok, message}.
        """
        if not self.configured():
            return [{"ok": False, "message": "cTrader not configured"}]
        if self._dry_run:
            n = self.count_open_positions(None)
            print(f"🛑 dry_run close_all — would close ~{n} position(s)")
            return [
                {
                    "ok": True,
                    "message": "dry_run — not sent",
                    "dry_run": True,
                    "approx_open": n,
                }
            ]
        with self._lock:
            try:
                with CTraderClient(
                    host=self._host,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                    access_token=self._access_token,
                    account_id=int(self._account_id),
                ) as client:
                    results = client.close_all_positions()
                self._open_cache_at = 0.0
                return results
            except CTraderError as e:
                print(f"❌ close_all_positions: {e}")
                return [{"ok": False, "message": str(e)}]
            except Exception as e:
                print(f"❌ close_all_positions unexpected: {e}")
                return [{"ok": False, "message": f"unexpected: {e}"}]

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        if not self.configured():
            return OrderResult(
                ok=False,
                status="error",
                message=(
                    "cTrader credentials missing. Set CTRADER_CLIENT_ID, "
                    "CTRADER_CLIENT_SECRET, access token, account id"
                ),
                detail=plan.as_dict(),
            )

        try:
            preset = resolve_preset(plan.symbol, plan.kind)
            lot_units = float(preset.get("lot_units") or 100.0)
            volume = lots_to_protocol_volume(plan.volume_lots, lot_units)
            sl_dist = abs(plan.entry_price - plan.sl_price)
            tp_dist = abs(plan.tp_price - plan.entry_price)
            rel_sl = price_distance_to_relative(sl_dist, plan.point_size)
            rel_tp = price_distance_to_relative(tp_dist, plan.point_size)
        except Exception as e:
            return OrderResult(
                ok=False,
                status="error",
                message=f"volume/sltp convert failed: {e}",
                detail=plan.as_dict(),
            )

        client_order_id = f"f{plan.alert_id}-{uuid.uuid4().hex[:8]}"
        detail: Dict[str, Any] = {
            "plan": plan.as_dict(),
            "host": self._host,
            "account_id": self._account_id,
            "volume_protocol": volume,
            "relative_sl": rel_sl,
            "relative_tp": rel_tp,
            "client_order_id": client_order_id,
            "dry_run": self._dry_run,
        }

        print(
            f"📤 cTrader {plan.side} {plan.broker_symbol} "
            f"lots={plan.volume_lots} vol={volume} "
            f"relSL={rel_sl} relTP={rel_tp} "
            f"alert_id={plan.alert_id} dry_run={self._dry_run} "
            f"account={self._account_id}"
        )

        if self._dry_run:
            return OrderResult(
                ok=True,
                status="open",
                broker_order_id=f"dry-{client_order_id}",
                message="dry_run — not sent to cTrader",
                detail=detail,
            )

        with self._lock:
            try:
                with CTraderClient(
                    host=self._host,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                    access_token=self._access_token,
                    account_id=int(self._account_id),
                ) as client:
                    exec_body = client.place_market(
                        broker_symbol=plan.broker_symbol,
                        side=plan.side,
                        volume_protocol=volume,
                        relative_sl=rel_sl,
                        relative_tp=rel_tp,
                        client_order_id=client_order_id,
                        comment=f"factor alert {plan.alert_id} {plan.kind}",
                        label="factor",
                    )
                self._open_cache_at = 0.0
            except CTraderError as e:
                print(f"❌ cTrader error: {e}")
                return OrderResult(
                    ok=False,
                    status="rejected",
                    message=str(e),
                    detail=detail,
                )
            except Exception as e:
                print(f"❌ cTrader unexpected: {e}")
                return OrderResult(
                    ok=False,
                    status="error",
                    message=f"unexpected: {e}",
                    detail=detail,
                )

        detail["execution"] = exec_body
        order_id = _extract_order_id(exec_body) or client_order_id
        position_id = _extract_position_id(exec_body)
        if position_id:
            detail["position_id"] = position_id
        print(f"✅ cTrader order ok id={order_id} alert_id={plan.alert_id}")
        return OrderResult(
            ok=True,
            status="open",
            broker_order_id=str(order_id),
            message="order accepted",
            detail=detail,
        )

    def _new_client(self) -> CTraderClient:
        return CTraderClient(
            host=self._host,
            client_id=self._client_id,
            client_secret=self._client_secret,
            access_token=self._access_token,
            account_id=int(self._account_id),
        )

    @contextmanager
    def session(self) -> Iterator[Any]:
        """
        One authenticated WebSocket for multiple ops (list + spot + amend).

        Yields a lightweight facade with the same BE methods as this broker.
        Prefer this in the breakeven poll to avoid 3 reconnects per order.
        """
        if not self.configured():
            raise CTraderError("cTrader not configured")
        if self._dry_run:
            yield _DryRunSession()
            return
        with self._lock:
            with self._new_client() as client:
                yield _LiveSession(client, on_amend=self._bump_open_cache)

    def _bump_open_cache(self) -> None:
        self._open_cache_at = 0.0

    def list_open_positions_info(self) -> list:
        """Normalized open positions (entry, SL, TP, side, symbol)."""
        if not self.configured():
            return []
        if self._dry_run:
            return []
        with self._lock:
            with self._new_client() as client:
                return client.list_open_positions_info()

    def position_close_outcome(
        self,
        position_id: int,
        *,
        from_timestamp_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Look up cTrader close deals for a position and return won/lost summary.
        """
        from factor.services.outcome import RESULT_UNKNOWN, summarize_close_deals

        if not self.configured() or self._dry_run:
            return {"result": RESULT_UNKNOWN, "close_deals": 0, "reason": "dry_or_unconfigured"}
        with self._lock:
            with self._new_client() as client:
                deals = client.list_deals_by_position_id(
                    int(position_id),
                    from_timestamp_ms=from_timestamp_ms,
                )
        out = summarize_close_deals(deals)
        out["position_id"] = int(position_id)
        return out

    def get_spot(self, broker_symbol: str) -> Dict[str, float]:
        if not self.configured():
            raise CTraderError("cTrader not configured")
        if self._dry_run:
            # No live feed — caller should skip BE in dry_run
            raise CTraderError("dry_run has no spots")
        with self._lock:
            with self._new_client() as client:
                return client.get_spot(broker_symbol)

    def close_position(self, *, position_id: int, volume: int) -> Dict[str, Any]:
        if not self.configured():
            raise CTraderError("cTrader not configured")
        if self._dry_run:
            print(f"🛑 dry_run close pos={position_id} vol={volume}")
            return {
                "dry_run": True,
                "position_id": position_id,
                "volume": volume,
            }
        with self._lock:
            with self._new_client() as client:
                body = client.close_position(
                    position_id=int(position_id), volume=int(volume)
                )
            self._bump_open_cache()
            return body

    def amend_position_sl_tp(
        self,
        *,
        position_id: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.configured():
            raise CTraderError("cTrader not configured")
        if self._dry_run:
            print(
                f"🛡️  dry_run amend SL/TP pos={position_id} "
                f"sl={stop_loss} tp={take_profit}"
            )
            return {
                "dry_run": True,
                "position_id": position_id,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        with self._lock:
            with self._new_client() as client:
                body = client.amend_position_sl_tp(
                    position_id=position_id,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
            self._bump_open_cache()
            return body


class _DryRunSession:
    """Facade matching BE broker methods when CTRADER_DRY_RUN is on."""

    _dry_run = True

    def list_open_positions_info(self) -> list:
        return []

    def get_spot(self, broker_symbol: str) -> Dict[str, float]:
        raise CTraderError("dry_run has no spots")

    def close_position(self, *, position_id: int, volume: int) -> Dict[str, Any]:
        return {"dry_run": True, "position_id": position_id, "volume": volume}

    def amend_position_sl_tp(
        self,
        *,
        position_id: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        return {
            "dry_run": True,
            "position_id": position_id,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }

    def position_close_outcome(
        self,
        position_id: int,
        *,
        from_timestamp_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        from factor.services.outcome import RESULT_UNKNOWN

        return {"result": RESULT_UNKNOWN, "close_deals": 0, "reason": "dry_or_unconfigured"}


class _LiveSession:
    """Wrap an open CTraderClient so BE can list/spot/amend without reconnecting."""

    _dry_run = False

    def __init__(self, client: CTraderClient, *, on_amend: Optional[Any] = None) -> None:
        self._client = client
        self._on_amend = on_amend

    def list_open_positions_info(self) -> list:
        return self._client.list_open_positions_info()

    def get_spot(self, broker_symbol: str) -> Dict[str, float]:
        return self._client.get_spot(broker_symbol)

    def amend_position_sl_tp(
        self,
        *,
        position_id: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        body = self._client.amend_position_sl_tp(
            position_id=position_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if self._on_amend:
            self._on_amend()
        return body

    def close_position(self, *, position_id: int, volume: int) -> Dict[str, Any]:
        body = self._client.close_position(
            position_id=int(position_id), volume=int(volume)
        )
        if self._on_amend:
            self._on_amend()
        return body

    def position_close_outcome(
        self,
        position_id: int,
        *,
        from_timestamp_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        from factor.services.outcome import summarize_close_deals

        deals = self._client.list_deals_by_position_id(
            int(position_id),
            from_timestamp_ms=from_timestamp_ms,
        )
        out = summarize_close_deals(deals)
        out["position_id"] = int(position_id)
        return out


def broker_from_connection(
    *,
    access_token: str,
    account_id: int,
    host: str,
    dry_run: Optional[bool] = None,
) -> CTraderBroker:
    """Build a broker bound to one user's cTrader connection."""
    return CTraderBroker(
        access_token=access_token,
        account_id=str(account_id),
        host=host,
        dry_run=dry_run,
    )


def _extract_order_id(execution: Dict[str, Any]) -> Optional[str]:
    if not isinstance(execution, dict):
        return None
    order = execution.get("order") or {}
    pos = execution.get("position") or {}
    for key in ("orderId", "positionId", "dealId"):
        if order.get(key) is not None:
            return str(order[key])
        if pos.get(key) is not None:
            return str(pos[key])
        if execution.get(key) is not None:
            return str(execution[key])
    return None


def _extract_position_id(execution: Dict[str, Any]) -> Optional[str]:
    if not isinstance(execution, dict):
        return None
    pos = execution.get("position") or {}
    deal = execution.get("deal") or {}
    for src in (pos, deal, execution):
        if not isinstance(src, dict):
            continue
        for key in ("positionId", "position_id"):
            if src.get(key) is not None:
                return str(src[key])
    return None
