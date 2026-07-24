"""
cTrader Open API adapter — Phase 3 stub.

IC Markets via cTrader is the intended live path (not FIX for MVP).
Wire credentials + WebSocket order flow when paper path is proven.
"""

from __future__ import annotations

from factor.broker.base import OrderResult
from factor.rules.presets import OrderPlan


class CTraderBroker:
    name = "ctrader"

    def __init__(self) -> None:
        pass

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        return OrderResult(
            ok=False,
            status="error",
            message=(
                "cTrader adapter not implemented yet (Phase 3). "
                "Use BROKER=paper until Open API is wired."
            ),
            detail=plan.as_dict(),
        )
