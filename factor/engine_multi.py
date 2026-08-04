"""
Multi-user fan-out: one Reaktor alert → zero or more user orders.

This is the only execution path (single-tenant SQLite engine removed).
Requires Supabase + per-user cTrader connections and enabled prefs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from factor import config
from factor.broker.ctrader import broker_from_connection
from factor.crypto.tokens import decrypt_token
from factor.db import repos
from factor.inbound.models import AlertData
from factor.rules.filter import alert_age_seconds, alert_triggered_at, market_open_blackout
from factor.rules.presets import build_order_plan
from factor.services.breakeven import build_breakeven_order_detail


def _pref_overrides(pref: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "volume_lots": float(pref.get("volume_lots") or 0.01),
        "sl_points": int(pref.get("sl_points") or 1000),
        "tp_points": int(pref.get("tp_points") or 1000),
    }


def process_alert_for_user(
    alert: AlertData,
    *,
    user: Dict[str, Any],
    pref: Dict[str, Any],
    connection: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Handle one alert for one user.

    Claim-before-place on (user_id, alert_id). Caps from pref.
    max_concurrent / max_per_day are per alert pref (kind + symbol).
    """
    user_id = str(user["id"])
    uid_short = user_id[:8]

    # 1) Claim de-dupe — same webhook never runs twice for this user
    if not repos.claim_alert(user_id, alert.alert_id, reason="claimed"):
        print(
            f"⏭  [{uid_short}] skip alert_id={alert.alert_id} duplicate_alert_id"
        )
        return {
            "action": "skip",
            "reason": "duplicate_alert_id",
            "alert_id": alert.alert_id,
            "user_id": user_id,
        }

    def _skip(reason: str, status: str = "skipped") -> Dict[str, Any]:
        repos.update_processed_reason(user_id, alert.alert_id, reason)
        try:
            repos.insert_order(
                user_id=user_id,
                reaktor_alert_id=alert.alert_id,
                kind=alert.kind,
                symbol=alert.symbol,
                side=alert.side if alert.side in ("UP", "DOWN") else "NEUTRAL",
                status=status,
                skip_reason=reason,
            )
        except Exception as e:
            print(f"⚠️  [{uid_short}] insert skip order failed: {e}")
        print(
            f"⏭  [{uid_short}] skip alert_id={alert.alert_id} "
            f"{alert.kind} {alert.symbol}: {reason}"
        )
        return {
            "action": "skip",
            "reason": reason,
            "alert_id": alert.alert_id,
            "user_id": user_id,
        }

    # 2) Kill / trading flags (candidates should already filter; belt)
    if user.get("kill_switch"):
        return _skip("kill_switch")
    if not user.get("trading_enabled", True):
        return _skip("trading_disabled")

    # 3) Direction / freshness
    if alert.side == "NEUTRAL" or not alert.is_directional:
        return _skip("side_neutral")

    max_age = float(getattr(config, "MAX_ALERT_AGE_SEC", 5) or 5)
    if max_age > 0:
        age = alert_age_seconds(alert)
        if age is None:
            return _skip("alert_age_unknown")
        if age < -2.0:
            return _skip(f"alert_in_future:{age:.1f}s")
        if age > max_age:
            return _skip(f"alert_too_old:{age:.1f}s>{max_age:g}s")

    # Market-open blackout (e.g. first 15m after ~01:05 local)
    bo = market_open_blackout(alert_triggered_at(alert))
    if bo:
        return _skip(bo)

    # 4) Daily cap (successful opens) — per pref kind (same as concurrent scope)
    max_day = int(pref.get("max_per_day") or 0)
    if max_day > 0:
        try:
            day_n = repos.count_orders_today(
                user_id, kind=alert.kind, statuses=["open", "filled"]
            )
        except Exception as e:
            print(f"⚠️  [{uid_short}] count_orders_today failed: {e}")
            day_n = 0
        if day_n >= max_day:
            return _skip(f"max_per_day:{day_n}>={max_day}")

    # 5) Concurrent open orders — per pref (kind + symbol), Factor DB
    max_conc = int(pref.get("max_concurrent") or 0)
    if max_conc <= 0:
        return _skip("max_concurrent_zero")
    try:
        open_n = repos.count_open_orders_db(
            user_id, kind=alert.kind, symbol=alert.symbol
        )
    except Exception as e:
        print(f"⚠️  [{uid_short}] count_open_orders_db failed: {e}")
        return _skip(f"open_count_failed:{e}")
    if open_n >= max_conc:
        return _skip(f"max_concurrent:{open_n}>={max_conc}")

    # 6) Decrypt tokens + broker
    if not config.FACTOR_TOKEN_ENC_KEY:
        return _skip("token_enc_key_missing")
    try:
        access = decrypt_token(
            connection["access_token_enc"], config.FACTOR_TOKEN_ENC_KEY
        )
    except Exception as e:
        return _skip(f"token_decrypt_failed:{e}")

    broker = broker_from_connection(
        access_token=access,
        account_id=int(connection["account_id"]),
        host=str(connection.get("host") or "demo.ctraderapi.com"),
        dry_run=config.CTRADER_DRY_RUN,
    )

    # 7) Plan + place
    try:
        plan = build_order_plan(alert, overrides=_pref_overrides(pref))
    except Exception as e:
        return _skip(f"plan_error:{e}", status="error")

    result = broker.place_market_with_sl_tp(plan)
    status = result.status if result.ok else "rejected"
    if result.ok and status not in ("open", "filled", "rejected", "error"):
        status = "open"

    order_side = plan.side  # BUY/SELL
    pos_id = None
    if isinstance(result.detail, dict):
        pos_id = result.detail.get("position_id")
    try:
        order_row = repos.insert_order(
            user_id=user_id,
            reaktor_alert_id=alert.alert_id,
            kind=alert.kind,
            symbol=alert.symbol,
            side=order_side,
            status=status if result.ok else "rejected",
            skip_reason=None if result.ok else result.message,
            entry_price=plan.entry_price,
            sl_price=plan.sl_price,
            tp_price=plan.tp_price,
            volume_lots=plan.volume_lots,
            broker_order_id=result.broker_order_id,
            broker_position_id=str(pos_id) if pos_id is not None else None,
            detail={
                "broker_message": result.message,
                "broker_detail": result.detail,
                "ok": result.ok,
                "breakeven": build_breakeven_order_detail(plan=plan, pref=pref),
            },
        )
        order_id = order_row.get("id")
    except Exception as e:
        print(f"⚠️  [{uid_short}] insert_order failed: {e}")
        order_id = None

    reason = "traded" if result.ok else f"broker_fail:{result.message}"
    repos.update_processed_reason(user_id, alert.alert_id, reason)

    action = "traded" if result.ok else "broker_error"
    print(
        f"{'✅' if result.ok else '❌'} [{uid_short}] {action} "
        f"alert_id={alert.alert_id} {plan.side} {plan.symbol} "
        f"status={status}"
    )
    return {
        "action": action,
        "alert_id": alert.alert_id,
        "user_id": user_id,
        "order_id": order_id,
        "plan": plan.as_dict(),
        "broker": {
            "ok": result.ok,
            "status": result.status,
            "order_id": result.broker_order_id,
            "message": result.message,
        },
    }


def process_alert_multi(alert: AlertData) -> Dict[str, Any]:
    """
    Fan-out one alert to all eligible users (sequential, 512MB-safe).
    """
    if not config.TRADING_ENABLED:
        print(f"⏭  fan-out skip alert_id={alert.alert_id}: trading_disabled")
        return {
            "action": "fanout",
            "alert_id": alert.alert_id,
            "users": 0,
            "results": [],
            "reason": "trading_disabled",
        }

    if alert.side == "NEUTRAL" or not alert.is_directional:
        print(f"⏭  fan-out skip alert_id={alert.alert_id}: side_neutral")
        return {
            "action": "fanout",
            "alert_id": alert.alert_id,
            "users": 0,
            "results": [],
            "reason": "side_neutral",
        }

    try:
        candidates = repos.list_candidates_for_alert(
            kind=alert.kind, symbol=alert.symbol
        )
    except Exception as e:
        print(f"⚠️  list_candidates failed: {e}")
        return {
            "action": "fanout_error",
            "alert_id": alert.alert_id,
            "error": str(e),
            "users": 0,
            "results": [],
        }

    print(
        f"📣 Fan-out alert_id={alert.alert_id} {alert.kind} {alert.symbol} "
        f"{alert.side} → {len(candidates)} user(s)"
    )

    results: List[Dict[str, Any]] = []
    traded = 0
    skipped = 0
    for c in candidates:
        try:
            r = process_alert_for_user(
                alert,
                user=c["user"],
                pref=c["pref"],
                connection=c["connection"],
            )
        except Exception as e:
            r = {
                "action": "error",
                "alert_id": alert.alert_id,
                "user_id": c.get("user", {}).get("id"),
                "reason": str(e),
            }
            print(f"⚠️  fan-out user error: {e}")
        results.append(
            {
                "user_id": r.get("user_id"),
                "action": r.get("action"),
                "reason": r.get("reason"),
                "order_id": r.get("order_id"),
            }
        )
        if r.get("action") == "traded":
            traded += 1
        elif r.get("action") == "skip":
            skipped += 1

    print(
        f"📣 Fan-out done alert_id={alert.alert_id} "
        f"traded={traded} skipped={skipped} total={len(results)}"
    )
    return {
        "action": "fanout",
        "alert_id": alert.alert_id,
        "users": len(candidates),
        "traded": traded,
        "skipped": skipped,
        "results": results,
    }
