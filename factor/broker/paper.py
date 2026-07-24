"""Paper broker — log virtual fills, no real money."""

from __future__ import annotations

import uuid

from factor.broker.base import OrderResult
from factor.rules.presets import OrderPlan


class PaperBroker:
    name = "paper"

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        oid = f"paper-{uuid.uuid4().hex[:12]}"
        print(
            f"📝 PAPER {plan.side} {plan.symbol} lots={plan.volume_lots} "
            f"@ {plan.entry_price:.5f} SL={plan.sl_price:.5f} "
            f"TP={plan.tp_price:.5f} (sl={plan.sl_points:.0f}/"
            f"tp={plan.tp_points:.0f} pts) alert_id={plan.alert_id}"
        )
        return OrderResult(
            ok=True,
            status="paper",
            broker_order_id=oid,
            message="paper fill",
            detail=plan.as_dict(),
        )
