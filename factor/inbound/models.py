"""
Parse Reaktor CloudEvents → typed alert data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class AlertData:
    alert_id: int
    kind: str
    symbol: str
    side: str  # UP | DOWN | NEUTRAL
    price: float
    timeframe_min: int
    triggered_at: Optional[str] = None
    bar_open_time: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    thresholds: Optional[Dict[str, Any]] = None
    event_id: Optional[str] = None  # CloudEvents envelope id

    @property
    def is_directional(self) -> bool:
        return self.side in ("UP", "DOWN")


def _normalize_side(raw: Any) -> str:
    if raw is None:
        return "NEUTRAL"
    s = str(raw).upper()
    if "UP" in s or "BULL" in s or "LONG" in s:
        return "UP"
    if "DOWN" in s or "BEAR" in s or "SHORT" in s:
        return "DOWN"
    return "NEUTRAL"


def parse_cloudevent(payload: Dict[str, Any]) -> AlertData:
    """
    Accept full CloudEvent envelope or a bare data object.
    Raises ValueError if required fields are missing / invalid.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        raise ValueError("missing data object")

    alert_id = data.get("alert_id")
    if alert_id is None:
        # Fallback: reaktor-alert-123
        eid = payload.get("id") or ""
        if isinstance(eid, str) and eid.startswith("reaktor-alert-"):
            try:
                alert_id = int(eid.rsplit("-", 1)[-1])
            except ValueError:
                alert_id = None
    if alert_id is None:
        raise ValueError("missing alert_id")
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid alert_id: {alert_id}") from e

    kind = str(data.get("kind") or "").strip()
    if not kind:
        raise ValueError("missing kind")

    symbol = str(data.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("missing symbol")

    side = _normalize_side(data.get("side"))

    try:
        price = float(data.get("price"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid price: {data.get('price')}") from e

    try:
        timeframe_min = int(data.get("timeframe_min") or 0)
    except (TypeError, ValueError):
        timeframe_min = 0

    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    thresholds = (
        data.get("thresholds") if isinstance(data.get("thresholds"), dict) else {}
    )

    # Prefer data.triggered_at; fall back to CloudEvents envelope time
    triggered_at = data.get("triggered_at") or payload.get("time")
    if triggered_at is not None:
        triggered_at = str(triggered_at)

    return AlertData(
        alert_id=alert_id,
        kind=kind,
        symbol=symbol,
        side=side,
        price=price,
        timeframe_min=timeframe_min,
        triggered_at=triggered_at,
        bar_open_time=data.get("bar_open_time"),
        metrics=metrics,
        thresholds=thresholds,
        event_id=str(payload.get("id") or "") or None,
    )
