"""
Decide whether an alert should become a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from factor import config
from factor.inbound.models import AlertData
from factor.store.state import StateStore


@dataclass
class Decision:
    ok: bool
    reason: str
    # when ok=False: skip | reject | disabled | duplicate | limit


def decide(alert: AlertData, store: StateStore) -> Decision:
    """
    Pure-ish policy check. store is used for de-dupe + rate limits.
    """
    if not config.TRADING_ENABLED:
        return Decision(False, "trading_disabled")

    if store.was_processed(alert.alert_id):
        return Decision(False, "duplicate_alert_id")

    if alert.side == "NEUTRAL" or not alert.is_directional:
        return Decision(False, "side_neutral")

    if config.TRADE_KINDS and alert.kind not in config.TRADE_KINDS:
        return Decision(False, f"kind_not_allowed:{alert.kind}")

    if config.TRADE_SYMBOLS and alert.symbol not in config.TRADE_SYMBOLS:
        return Decision(False, f"symbol_not_allowed:{alert.symbol}")

    if alert.symbol not in config.PRESETS:
        return Decision(False, f"no_preset:{alert.symbol}")

    open_n = store.count_open_positions(alert.symbol)
    if open_n >= config.MAX_OPEN_PER_SYMBOL:
        return Decision(False, f"max_open_per_symbol:{open_n}")

    day_n = store.count_trades_today()
    if day_n >= config.MAX_TRADES_PER_DAY:
        return Decision(False, f"max_trades_per_day:{day_n}")

    last_ts = store.last_trade_time(alert.symbol)
    if last_ts is not None:
        age = store.seconds_since(last_ts)
        if age is not None and age < config.COOLDOWN_SEC:
            return Decision(
                False,
                f"cooldown:{int(config.COOLDOWN_SEC - age)}s_left",
            )

    return Decision(True, "ok")
