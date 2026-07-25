from __future__ import annotations

from factor import config
from factor.broker.base import Broker
from factor.broker.ctrader import CTraderBroker


def get_broker() -> Broker:
    name = (config.BROKER or "ctrader").lower()
    if name in ("ctrader", "ct", "icmarkets", "ic"):
        return CTraderBroker()
    print(f"⚠️  Unknown BROKER={name!r}, using ctrader")
    return CTraderBroker()
