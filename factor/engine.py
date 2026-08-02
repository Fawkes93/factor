"""
Alert → decision → order plan → broker open-check → broker order.
"""

from __future__ import annotations

from typing import Any, Dict

from factor.broker.base import Broker
from factor.inbound.models import AlertData
from factor.rules.filter import decide
from factor.rules.presets import build_order_plan
from factor.store.state import StateStore


# Skips that may clear later — do NOT burn the alert_id.
_RETRYABLE_REASONS = (
    "trading_disabled",
    "max_trades_per_day",
    "cooldown",
)


def _is_retryable(reason: str) -> bool:
    if reason in _RETRYABLE_REASONS:
        return True
    return any(reason.startswith(p + ":") for p in _RETRYABLE_REASONS)


def process_alert(
    alert: AlertData,
    *,
    store: StateStore,
    broker: Broker,
) -> Dict[str, Any]:
    """
    Handle one alert.

    1. Local decide (incl. alert_id de-dupe — same webhook never double-opens)
    2. Build plan
    3. Ask cTrader if any open position exists → skip (burn alert_id)
    4. Place order; mark alert_id processed so retries cannot re-fire
    """
    decision = decide(alert, store)
    if not decision.ok:
        if decision.reason != "duplicate_alert_id" and not _is_retryable(
            decision.reason
        ):
            store.mark_processed(
                alert.alert_id,
                kind=alert.kind,
                symbol=alert.symbol,
                side=alert.side,
                reason=decision.reason,
            )
        print(
            f"⏭  skip alert_id={alert.alert_id} {alert.kind} "
            f"{alert.symbol} {alert.side}: {decision.reason}"
        )
        return {
            "action": "skip",
            "reason": decision.reason,
            "alert_id": alert.alert_id,
        }

    try:
        plan = build_order_plan(alert)
    except Exception as e:
        store.mark_processed(
            alert.alert_id,
            kind=alert.kind,
            symbol=alert.symbol,
            side=alert.side,
            reason=f"plan_error:{e}",
        )
        print(f"⚠️  plan failed alert_id={alert.alert_id}: {e}")
        return {
            "action": "error",
            "reason": f"plan_error:{e}",
            "alert_id": alert.alert_id,
        }

    # Broker is source of truth for open exposure (not SQLite).
    try:
        if broker.has_open_position(plan.broker_symbol):
            reason = "broker_has_open_position"
            store.mark_processed(
                alert.alert_id,
                kind=alert.kind,
                symbol=alert.symbol,
                side=alert.side,
                reason=reason,
            )
            print(
                f"⏭  skip alert_id={alert.alert_id} {alert.kind} "
                f"{alert.symbol} {alert.side}: {reason}"
            )
            return {
                "action": "skip",
                "reason": reason,
                "alert_id": alert.alert_id,
            }
    except Exception as e:
        # Fail closed if broker check itself explodes
        reason = f"broker_open_check_failed:{e}"
        store.mark_processed(
            alert.alert_id,
            kind=alert.kind,
            symbol=alert.symbol,
            side=alert.side,
            reason=reason,
        )
        print(f"⚠️  {reason}")
        return {
            "action": "skip",
            "reason": reason,
            "alert_id": alert.alert_id,
        }

    result = broker.place_market_with_sl_tp(plan)

    # Mark after broker attempt so we never double-fire the same alert_id.
    status = result.status if result.ok else "rejected"
    if result.ok and status not in ("open", "filled", "rejected", "error"):
        status = "open"

    trade_id = store.insert_trade(
        alert_id=alert.alert_id,
        broker=broker.name,
        status=status if result.ok else "rejected",
        symbol=plan.symbol,
        side=plan.side,
        volume_lots=plan.volume_lots,
        entry_price=plan.entry_price,
        sl_price=plan.sl_price,
        tp_price=plan.tp_price,
        kind=plan.kind,
        broker_order_id=result.broker_order_id,
        detail={
            "plan": plan.as_dict(),
            "broker_message": result.message,
            "broker_detail": result.detail,
            "ok": result.ok,
        },
    )
    store.mark_processed(
        alert.alert_id,
        kind=alert.kind,
        symbol=alert.symbol,
        side=alert.side,
        reason="traded" if result.ok else f"broker_fail:{result.message}",
    )

    action = "traded" if result.ok else "broker_error"
    print(
        f"{'✅' if result.ok else '❌'} {action} alert_id={alert.alert_id} "
        f"trade_id={trade_id} {plan.side} {plan.symbol} "
        f"broker={broker.name} status={status}"
    )
    return {
        "action": action,
        "alert_id": alert.alert_id,
        "trade_id": trade_id,
        "plan": plan.as_dict(),
        "broker": {
            "ok": result.ok,
            "status": result.status,
            "order_id": result.broker_order_id,
            "message": result.message,
        },
    }
