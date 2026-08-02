"""
Per-user kill switch: block new trades + close open cTrader positions.
"""

from __future__ import annotations

from typing import Any, Dict, List

from factor import config
from factor.broker.ctrader import broker_from_connection
from factor.crypto.tokens import decrypt_token
from factor.db import repos


def activate_kill_switch(user_id: str) -> Dict[str, Any]:
    """
    1. Set kill_switch=true (and trading_enabled=false)
    2. Close all open positions on user's cTrader account
    3. Mark local open orders as closed

    Always leaves kill_switch true even if some closes fail.
    """
    user = repos.get_user(user_id)
    if not user:
        raise ValueError("user not found")

    user = repos.set_kill_switch(user_id, True)
    print(f"🛑 Kill switch ON for user {user_id[:8]}…")

    close_results: List[Dict[str, Any]] = []
    conn = repos.get_connection(user_id)
    if not conn:
        print(f"⚠️  Kill: no cTrader connection for {user_id[:8]}")
        close_results.append({"ok": False, "message": "no_ctrader_connection"})
    elif not config.FACTOR_TOKEN_ENC_KEY:
        close_results.append({"ok": False, "message": "FACTOR_TOKEN_ENC_KEY missing"})
    else:
        try:
            access = decrypt_token(
                conn["access_token_enc"], config.FACTOR_TOKEN_ENC_KEY
            )
            broker = broker_from_connection(
                access_token=access,
                account_id=int(conn["account_id"]),
                host=str(conn.get("host") or "demo.ctraderapi.com"),
                dry_run=config.CTRADER_DRY_RUN,
            )
            close_results = broker.close_all_positions()
        except Exception as e:
            print(f"⚠️  Kill close_all failed: {e}")
            close_results = [{"ok": False, "message": str(e)}]

    db_closed = 0
    try:
        db_closed = repos.mark_open_orders_closed(user_id, reason="kill_switch")
    except Exception as e:
        print(f"⚠️  mark_open_orders_closed: {e}")

    closed_ok = sum(1 for r in close_results if r.get("ok"))
    closed_fail = sum(1 for r in close_results if not r.get("ok"))

    return {
        "ok": True,
        "kill_switch": True,
        "trading_enabled": False,
        "user_id": user_id,
        "close": {
            "results": close_results,
            "ok_count": closed_ok,
            "fail_count": closed_fail,
        },
        "orders_marked_closed": db_closed,
        "user": {
            "kill_switch": user.get("kill_switch"),
            "trading_enabled": user.get("trading_enabled"),
        },
    }


def resume_trading(user_id: str) -> Dict[str, Any]:
    """
    Clear kill switch and re-enable trading.
    Does not re-open positions.
    """
    user = repos.get_user(user_id)
    if not user:
        raise ValueError("user not found")
    user = repos.set_kill_switch(user_id, False)
    # set_kill_switch(False) sets trading_enabled=True
    print(f"▶️  Kill switch OFF / trading resumed for user {user_id[:8]}…")
    return {
        "ok": True,
        "kill_switch": False,
        "trading_enabled": True,
        "user_id": user_id,
        "user": {
            "kill_switch": user.get("kill_switch"),
            "trading_enabled": user.get("trading_enabled"),
        },
    }
