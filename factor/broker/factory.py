from __future__ import annotations

from factor import config
from factor.broker.base import Broker
from factor.broker.ctrader import CTraderBroker
from factor.broker.paper import PaperBroker


def get_broker() -> Broker:
    name = (config.BROKER or "paper").lower()
    if name == "paper":
        return PaperBroker()
    if name in ("ctrader", "ct"):
        return CTraderBroker()
    print(f"⚠️  Unknown BROKER={name!r}, falling back to paper")
    return PaperBroker()
