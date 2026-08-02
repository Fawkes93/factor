"""
IC Markets / cTrader Open API broker.

Places market orders with relative SL/TP (demo or live host).
Supports env credentials (single-tenant) or per-user constructor args (multi-user).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, Optional

from factor import config
from factor.broker.base import OrderResult
from factor.broker.ctrader_client import CTraderClient, CTraderError
from factor.rules.presets import OrderPlan, resolve_preset


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


def price_distance_to_relative(distance: float) -> int:
    """relativeStopLoss/TP = price distance × 100_000."""
    rel = int(round(abs(float(distance)) * 100_000))
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
            rel_sl = price_distance_to_relative(sl_dist)
            rel_tp = price_distance_to_relative(tp_dist)
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
        print(f"✅ cTrader order ok id={order_id} alert_id={plan.alert_id}")
        return OrderResult(
            ok=True,
            status="open",
            broker_order_id=str(order_id),
            message="order accepted",
            detail=detail,
        )


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
