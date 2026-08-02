"""
Resolve volume + SL/TP prices from fixed-point presets.

MT-style points (same as Reaktor):
  XAU point_size 0.01 → 300 pts = $3.00 move
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from factor import config
from factor.inbound.models import AlertData


@dataclass
class OrderPlan:
    symbol: str
    broker_symbol: str
    side: str  # BUY | SELL
    volume_lots: float
    entry_price: float
    sl_price: float
    tp_price: float
    sl_points: float
    tp_points: float
    point_size: float
    kind: str
    alert_id: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "broker_symbol": self.broker_symbol,
            "side": self.side,
            "volume_lots": self.volume_lots,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "sl_points": self.sl_points,
            "tp_points": self.tp_points,
            "point_size": self.point_size,
            "kind": self.kind,
            "alert_id": self.alert_id,
        }


def _side_to_action(side: str) -> str:
    if side == "UP":
        return "BUY"
    if side == "DOWN":
        return "SELL"
    raise ValueError(f"cannot plan order for side={side}")


def resolve_preset(symbol: str, kind: str) -> Dict[str, Any]:
    base = dict(config.PRESETS.get(symbol) or {})
    if not base:
        raise KeyError(f"no PRESET for symbol {symbol!r}")
    kind_map = config.PRESET_OVERRIDES.get(kind) or {}
    override = kind_map.get(symbol) or {}
    base.update(override)
    return base


def calc_sl_tp(
    *,
    action: str,
    entry: float,
    sl_points: float,
    tp_points: float,
    point_size: float,
) -> tuple[float, float]:
    """Return (sl_price, tp_price)."""
    if point_size <= 0:
        raise ValueError("point_size must be > 0")
    sl_dist = float(sl_points) * point_size
    tp_dist = float(tp_points) * point_size
    if action == "BUY":
        return entry - sl_dist, entry + tp_dist
    if action == "SELL":
        return entry + sl_dist, entry - tp_dist
    raise ValueError(f"unknown action {action}")


def build_order_plan(
    alert: AlertData,
    *,
    overrides: Optional[Dict[str, Any]] = None,
) -> OrderPlan:
    """
    Build SL/TP plan. Optional overrides (from user prefs): volume_lots,
    sl_points, tp_points, broker_symbol, point_size, lot_units.
    """
    preset = resolve_preset(alert.symbol, alert.kind)
    if overrides:
        preset.update({k: v for k, v in overrides.items() if v is not None})
    action = _side_to_action(alert.side)
    point_size = float(preset["point_size"])
    sl_points = float(preset["sl_points"])
    tp_points = float(preset["tp_points"])
    volume = float(preset.get("volume_lots") or 0.01)
    broker_symbol = str(preset.get("broker_symbol") or alert.symbol.replace("/", ""))
    entry = float(alert.price)
    sl, tp = calc_sl_tp(
        action=action,
        entry=entry,
        sl_points=sl_points,
        tp_points=tp_points,
        point_size=point_size,
    )
    return OrderPlan(
        symbol=alert.symbol,
        broker_symbol=broker_symbol,
        side=action,
        volume_lots=volume,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        sl_points=sl_points,
        tp_points=tp_points,
        point_size=point_size,
        kind=alert.kind,
        alert_id=alert.alert_id,
    )
