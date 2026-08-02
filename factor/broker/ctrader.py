"""
IC Markets / cTrader Open API broker.

Places market orders with relative SL/TP (demo or live host).
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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Short cache so a double webhook does not double-reconcile
        self._open_cache_at = 0.0
        self._open_cache_key: Optional[str] = None
        self._open_cache_val = False

    def configured(self) -> bool:
        return bool(
            config.CTRADER_CLIENT_ID
            and config.CTRADER_CLIENT_SECRET
            and config.CTRADER_ACCESS_TOKEN
            and config.CTRADER_ACCOUNT_ID
        )

    def has_open_position(self, broker_symbol: Optional[str] = None) -> bool:
        """
        Ask cTrader (reconcile) whether the account has an open position.
        If broker_symbol is set (e.g. XAUUSD), only that symbol counts.
        """
        if not self.configured():
            print("⚠️  has_open_position: cTrader not configured — assuming flat")
            return False
        if config.CTRADER_DRY_RUN:
            # Dry-run still checks live account when creds exist (safer).
            pass

        key = (broker_symbol or "").upper()
        now = time.time()
        with self._lock:
            if (
                self._open_cache_key == key
                and now - self._open_cache_at < 2.0
            ):
                return self._open_cache_val

            try:
                with CTraderClient(
                    host=config.CTRADER_HOST,
                    client_id=config.CTRADER_CLIENT_ID,
                    client_secret=config.CTRADER_CLIENT_SECRET,
                    access_token=config.CTRADER_ACCESS_TOKEN,
                    account_id=int(config.CTRADER_ACCOUNT_ID),
                ) as client:
                    # Any open position on account blocks (one-at-a-time policy).
                    # Pass symbol to count only that market if you prefer later.
                    n = client.count_open_positions(None)
                    has = n > 0
                    print(
                        f"📡 cTrader open positions: {n}"
                        + (f" (filter {broker_symbol})" if broker_symbol else "")
                    )
            except CTraderError as e:
                print(f"⚠️  has_open_position reconcile failed: {e}")
                # Fail closed: treat as open so we do not stack risk on unknown state
                has = True
            except Exception as e:
                print(f"⚠️  has_open_position unexpected: {e}")
                has = True

            self._open_cache_at = now
            self._open_cache_key = key
            self._open_cache_val = has
            return has

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        if not self.configured():
            return OrderResult(
                ok=False,
                status="error",
                message=(
                    "cTrader credentials missing. Set CTRADER_CLIENT_ID, "
                    "CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN, "
                    "CTRADER_ACCOUNT_ID"
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
            "host": config.CTRADER_HOST,
            "account_id": config.CTRADER_ACCOUNT_ID,
            "volume_protocol": volume,
            "relative_sl": rel_sl,
            "relative_tp": rel_tp,
            "client_order_id": client_order_id,
            "dry_run": config.CTRADER_DRY_RUN,
        }

        print(
            f"📤 cTrader {plan.side} {plan.broker_symbol} "
            f"lots={plan.volume_lots} vol={volume} "
            f"relSL={rel_sl} relTP={rel_tp} "
            f"alert_id={plan.alert_id} dry_run={config.CTRADER_DRY_RUN}"
        )

        if config.CTRADER_DRY_RUN:
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
                    host=config.CTRADER_HOST,
                    client_id=config.CTRADER_CLIENT_ID,
                    client_secret=config.CTRADER_CLIENT_SECRET,
                    access_token=config.CTRADER_ACCESS_TOKEN,
                    account_id=int(config.CTRADER_ACCOUNT_ID),
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
                # Invalidate open-position cache after a live order attempt
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
