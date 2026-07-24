"""Broker interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from factor.rules.presets import OrderPlan


@dataclass
class OrderResult:
    ok: bool
    status: str  # paper | open | rejected | error
    broker_order_id: Optional[str] = None
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


class Broker(Protocol):
    name: str

    def place_market_with_sl_tp(self, plan: OrderPlan) -> OrderResult:
        ...
