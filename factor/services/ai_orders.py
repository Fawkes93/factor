"""
AI execution API.

The orchestrator sends structured orders. Factor is the only side that talks
to cTrader. Scheduled AI orders are labeled AI-GOLD-v1. Channel signals use
MANAN. Reaktor orders stay on the webhook path and keep the cTrader label
"factor".

Market orders use relative SL/TP because cTrader rejects absolute protection
prices on MARKET. Limit and stop orders use absolute prices.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from factor import config
from factor.broker.ctrader import (
    broker_from_connection,
    lots_to_protocol_volume,
    price_distance_to_relative,
)
from factor.crypto.tokens import decrypt_token
from factor.db import repos
from factor.rules.presets import round_price
from factor.services.breakeven import mark_price_for_side, reconcile_order_vs_positions

AI_ORDER_LABEL = "AI-GOLD-v1"
SIGNAL_ORDER_LABEL = "MANAN"
ALLOWED_ORDER_LABELS = frozenset({AI_ORDER_LABEL, SIGNAL_ORDER_LABEL})
_ORDER_TYPES = ("market", "limit", "stop")
_ACTIONS = (
    "leave",
    "trail",
    "move_sl_to_breakeven",
    "partial_close",
    "close",
)


class AiOrderError(Exception):
    """Caller mistake or missing Factor setup. status_code is the HTTP status."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def body(self) -> Dict[str, Any]:
        return {
            "success": False,
            "order_id": None,
            "position_id": None,
            "message": self.message,
        }


def _trade_result(
    *,
    success: bool,
    message: str,
    order_id: Any = None,
    position_id: Any = None,
    **extra: Any,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "success": bool(success),
        "order_id": None if order_id is None else str(order_id),
        "position_id": None if position_id is None else str(position_id),
        "message": message,
    }
    body.update(extra)
    return body


def _optional_account_id(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise AiOrderError("account_id must be an integer")


def _configured_account_id() -> Optional[int]:
    raw = str(getattr(config, "FACTOR_AI_ACCOUNT_ID", "") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise AiOrderError("FACTOR_AI_ACCOUNT_ID is not an integer", 503)


def resolve_connection(account_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Pick the cTrader connection for an AI request.

    Body account_id wins, then FACTOR_AI_ACCOUNT_ID, then the only connected
    account. More than one account and no id is an error.
    """
    if account_id is None:
        account_id = _configured_account_id()
    try:
        rows = repos.list_all_connections()
    except Exception as e:
        raise AiOrderError(f"could not load trading accounts: {e}", 503)
    if account_id is not None:
        matches = []
        for row in rows:
            try:
                if int(row.get("account_id")) == int(account_id):
                    matches.append(row)
            except (TypeError, ValueError):
                continue
        if not matches:
            raise AiOrderError("account not found")
        if len(matches) > 1:
            raise AiOrderError("account_id matches more than one user")
        return matches[0]
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise AiOrderError("no cTrader account is connected", 503)
    raise AiOrderError("account_id is required")


def _user_for(conn: Dict[str, Any]) -> Dict[str, Any]:
    try:
        user = repos.get_user(str(conn["user_id"]))
    except Exception as e:
        raise AiOrderError(f"could not load user: {e}", 503)
    if not user:
        raise AiOrderError("account not found")
    return user


def kill_block_reason(
    conn: Dict[str, Any], user: Optional[Dict[str, Any]]
) -> Optional[str]:
    """
    Why a new AI order must be refused, or None when trading is allowed.

    Same gates as the Reaktor path: process TRADING_ENABLED, then the
    account kill switch, then trading_enabled.
    """
    if not config.TRADING_ENABLED:
        return "trading_disabled"
    if not user:
        return "trading_disabled"
    conn_killed = conn.get("kill_switch")
    if conn_killed or (conn_killed is None and user.get("kill_switch")):
        return "kill_switch"
    conn_enabled = conn.get("trading_enabled")
    if conn_enabled is False or (
        conn_enabled is None and not user.get("trading_enabled", True)
    ):
        return "trading_disabled"
    return None


def broker_for_connection(conn: Dict[str, Any]) -> Any:
    if not config.FACTOR_TOKEN_ENC_KEY:
        raise AiOrderError("FACTOR_TOKEN_ENC_KEY missing", 503)
    try:
        access = decrypt_token(conn["access_token_enc"], config.FACTOR_TOKEN_ENC_KEY)
    except Exception as e:
        raise AiOrderError(f"could not read cTrader token: {e}", 503)
    return broker_from_connection(
        access_token=access,
        account_id=int(conn["account_id"]),
        host=str(conn.get("host") or "demo.ctraderapi.com"),
        dry_run=config.CTRADER_DRY_RUN,
    )


def _preset_for_symbol(raw: str) -> Optional[tuple]:
    text = (raw or "").strip().upper().replace(" ", "")
    if not text:
        return None
    for name, preset in (config.PRESETS or {}).items():
        broker = str(preset.get("broker_symbol") or "").upper()
        canon = str(name).upper()
        if text in (canon, canon.replace("/", ""), broker):
            return str(name), broker or canon.replace("/", ""), dict(preset)
    return None


def _preset_for_broker_symbol(broker_symbol: Optional[str]) -> Optional[Dict[str, Any]]:
    found = _preset_for_symbol(str(broker_symbol or ""))
    if found is None:
        return None
    return found[2]


def _point_size_for(broker_symbol: Optional[str]) -> float:
    preset = _preset_for_broker_symbol(broker_symbol)
    if preset is not None:
        try:
            return float(preset.get("point_size") or 0.01)
        except (TypeError, ValueError):
            return 0.01
    name = (broker_symbol or "").upper()
    if "XAU" in name or "GOLD" in name:
        return 0.01
    if "JPY" in name:
        return 0.001
    if "BTC" in name:
        return 1.0
    return 0.00001


def _lot_units_for(broker_symbol: Optional[str]) -> Optional[float]:
    preset = _preset_for_broker_symbol(broker_symbol)
    if preset is None:
        return None
    try:
        units = float(preset.get("lot_units"))
    except (TypeError, ValueError):
        return None
    if units <= 0:
        return None
    return units


def _require_number(raw: Any, name: str) -> float:
    if isinstance(raw, bool) or raw is None:
        raise AiOrderError(f"{name} is required")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise AiOrderError(f"{name} must be a number")
    if val != val or val in (float("inf"), float("-inf")):
        raise AiOrderError(f"{name} must be a number")
    return val


def _optional_number(raw: Any, name: str) -> Optional[float]:
    if raw is None or raw == "":
        return None
    return _require_number(raw, name)


def _parse_side(raw: Any) -> str:
    side = str(raw or "").strip().lower()
    if side in ("buy", "long", "up"):
        return "BUY"
    if side in ("sell", "short", "down"):
        return "SELL"
    raise AiOrderError("side must be buy or sell")


def _public_side(side: Any) -> str:
    return "sell" if str(side or "").upper() in ("SELL", "2", "SHORT", "DOWN") else "buy"


def _check_protection(
    *,
    side: str,
    entry: Optional[float],
    stop_loss: Optional[float],
    take_profit: Optional[float],
    point_size: float,
) -> None:
    if entry is None:
        return
    eps = max(float(point_size) * 0.5, 1e-9)
    if stop_loss is not None:
        if side == "BUY" and stop_loss >= entry - eps:
            raise AiOrderError("stop_loss must be below entry for a buy")
        if side == "SELL" and stop_loss <= entry + eps:
            raise AiOrderError("stop_loss must be above entry for a sell")
    if take_profit is not None:
        if side == "BUY" and take_profit <= entry + eps:
            raise AiOrderError("take_profit must be above entry for a buy")
        if side == "SELL" and take_profit >= entry - eps:
            raise AiOrderError("take_profit must be below entry for a sell")


def _parse_label(raw: Any) -> str:
    if raw is None or str(raw).strip() == "":
        return AI_ORDER_LABEL
    label = str(raw).strip()
    if label not in ALLOWED_ORDER_LABELS:
        allowed = " or ".join(sorted(ALLOWED_ORDER_LABELS))
        raise AiOrderError(f"label must be {allowed}")
    return label


def parse_order_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one AI order body and fill broker fields. Does not trade."""
    if not isinstance(payload, dict):
        raise AiOrderError("JSON object required")
    found = _preset_for_symbol(str(payload.get("symbol") or ""))
    if found is None:
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            raise AiOrderError("symbol is required")
        raise AiOrderError(f"unknown symbol {symbol}")
    canon, broker_symbol, preset = found
    side = _parse_side(payload.get("side"))
    volume = _require_number(payload.get("volume"), "volume")
    if volume <= 0:
        raise AiOrderError("volume must be greater than 0")
    order_type = str(payload.get("order_type") or "").strip().lower()
    if order_type not in _ORDER_TYPES:
        raise AiOrderError("order_type must be market, limit, or stop")
    point_size = float(preset.get("point_size") or 0.01)
    entry = _optional_number(payload.get("entry_price"), "entry_price")
    stop_loss = _optional_number(payload.get("stop_loss"), "stop_loss")
    take_profit = _optional_number(payload.get("take_profit"), "take_profit")
    if entry is not None:
        if entry <= 0:
            raise AiOrderError("entry_price must be greater than 0")
        entry = round_price(entry, point_size)
    if stop_loss is not None:
        if stop_loss <= 0:
            raise AiOrderError("stop_loss must be greater than 0")
        stop_loss = round_price(stop_loss, point_size)
    if take_profit is not None:
        if take_profit <= 0:
            raise AiOrderError("take_profit must be greater than 0")
        take_profit = round_price(take_profit, point_size)
    if order_type in ("limit", "stop") and entry is None:
        raise AiOrderError(f"{order_type} orders need entry_price")
    if order_type == "market" and entry is None and (
        stop_loss is not None or take_profit is not None
    ):
        raise AiOrderError(
            "market orders with stop_loss or take_profit need entry_price"
        )
    _check_protection(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        point_size=point_size,
    )
    relative_sl = 0
    relative_tp = 0
    limit_price = None
    stop_price = None
    absolute_sl = None
    absolute_tp = None
    if order_type == "market":
        if entry is not None and stop_loss is not None:
            relative_sl = price_distance_to_relative(abs(entry - stop_loss), point_size)
        if entry is not None and take_profit is not None:
            relative_tp = price_distance_to_relative(
                abs(take_profit - entry), point_size
            )
    else:
        if order_type == "limit":
            limit_price = entry
        else:
            stop_price = entry
        absolute_sl = stop_loss
        absolute_tp = take_profit
    lot_units = float(preset.get("lot_units") or 100)
    comment = str(payload.get("comment") or "").strip()[:512]
    return {
        "symbol": canon,
        "broker_symbol": broker_symbol,
        "side": side,
        "volume_lots": volume,
        "volume_protocol": lots_to_protocol_volume(volume, lot_units),
        "order_type": order_type,
        "entry_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "label": _parse_label(payload.get("label")),
        "comment": comment,
        "account_id": _optional_account_id(payload.get("account_id")),
        "relative_sl": relative_sl,
        "relative_tp": relative_tp,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "absolute_sl": absolute_sl,
        "absolute_tp": absolute_tp,
        "point_size": point_size,
    }


def place_ai_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Place one AI order. Refuses when the kill switch or trading flag is off."""
    req = parse_order_request(payload)
    conn = resolve_connection(req["account_id"])
    user = _user_for(conn)
    account_id = int(conn["account_id"])
    blocked = kill_block_reason(conn, user)
    if blocked:
        print(
            f"⏭  AI order skipped account={account_id} "
            f"{req['side']} {req['broker_symbol']}: {blocked}"
        )
        return _trade_result(
            success=False,
            message=blocked,
            account_id=account_id,
            kill_switch=True,
        )
    broker = broker_for_connection(conn)
    result = broker.place_labeled_order(
        broker_symbol=req["broker_symbol"],
        side=req["side"],
        volume_protocol=req["volume_protocol"],
        order_type=req["order_type"],
        label=req["label"],
        comment=req["comment"],
        limit_price=req["limit_price"],
        stop_price=req["stop_price"],
        relative_sl=req["relative_sl"],
        relative_tp=req["relative_tp"],
        stop_loss=req["absolute_sl"],
        take_profit=req["absolute_tp"],
    )
    detail = result.detail if isinstance(result.detail, dict) else {}
    position_id = detail.get("position_id")
    order_detail = {
        "source": "ai",
        "label": req["label"],
        "comment": req["comment"] or None,
        "order_type": req["order_type"],
        "broker_message": result.message,
        "broker_detail": detail,
        "ok": bool(result.ok),
        "breakeven": {"enabled": False, "applied": False},
    }
    try:
        repos.insert_order(
            user_id=str(conn["user_id"]),
            account_id=account_id,
            reaktor_alert_id=0,
            kind="ai",
            symbol=req["symbol"],
            side=req["side"],
            status="open" if result.ok else "rejected",
            skip_reason=None if result.ok else result.message,
            entry_price=req["entry_price"],
            sl_price=req["stop_loss"],
            tp_price=req["take_profit"],
            volume_lots=req["volume_lots"],
            broker_order_id=result.broker_order_id,
            broker_position_id=str(position_id) if position_id is not None else None,
            detail=order_detail,
            label=req["label"],
        )
    except Exception as e:
        print(f"⚠️  AI insert_order failed: {e}")
        if result.ok:
            message = "Order placed"
            if detail.get("dry_run"):
                message = result.message or message
            return _trade_result(
                success=True,
                message=f"{message} (audit row not saved: {e})",
                order_id=result.broker_order_id,
                position_id=position_id,
                account_id=account_id,
            )
    if result.ok:
        message = "Order placed"
        if detail.get("dry_run"):
            message = result.message or message
    else:
        message = result.message or "Order rejected"
    return _trade_result(
        success=bool(result.ok),
        message=message,
        order_id=result.broker_order_id,
        position_id=position_id,
        account_id=account_id,
    )


def _opened_at(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _lots_from_protocol(volume: Any, broker_symbol: Optional[str]) -> Optional[float]:
    units = _lot_units_for(broker_symbol)
    if units is None:
        return None
    try:
        protocol = int(float(volume))
    except (TypeError, ValueError):
        return None
    if protocol <= 0:
        return None
    return round((protocol / 100.0) / units, 8)


def _current_price(pos: Dict[str, Any]) -> Optional[float]:
    bid = pos.get("bid")
    ask = pos.get("ask")
    try:
        if bid is not None and ask is not None:
            return mark_price_for_side(
                side=str(pos.get("side") or ""),
                bid=float(bid),
                ask=float(ask),
            )
    except (TypeError, ValueError):
        pass
    raw = pos.get("current_price")
    try:
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _public_position(pos: Dict[str, Any]) -> Dict[str, Any]:
    broker_symbol = pos.get("broker_symbol") or pos.get("symbol")
    pnl = pos.get("unrealized_pnl")
    try:
        pnl_f = None if pnl is None else float(pnl)
    except (TypeError, ValueError):
        pnl_f = None
    return {
        "position_id": str(pos.get("position_id")),
        "symbol": broker_symbol,
        "side": _public_side(pos.get("side")),
        "volume": _lots_from_protocol(pos.get("volume"), broker_symbol),
        "entry_price": pos.get("entry_price"),
        "current_price": _current_price(pos),
        "stop_loss": pos.get("stop_loss"),
        "take_profit": pos.get("take_profit"),
        "unrealized_pnl": pnl_f,
        "opened_at": _opened_at(pos.get("open_timestamp") or pos.get("opened_at")),
        "label": str(pos.get("label") or ""),
    }


def list_ai_positions(
    *,
    label: str = AI_ORDER_LABEL,
    account_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Open positions whose cTrader label matches. Default is AI-GOLD-v1."""
    want = _parse_label(label)
    conn = resolve_connection(account_id)
    broker = broker_for_connection(conn)
    try:
        rows = broker.describe_open_positions()
    except Exception as e:
        return {
            "success": False,
            "label": want,
            "account_id": int(conn["account_id"]),
            "positions": [],
            "message": str(e),
        }
    positions = [
        _public_position(row)
        for row in rows or []
        if str((row or {}).get("label") or "") == want
    ]
    return {
        "success": True,
        "label": want,
        "account_id": int(conn["account_id"]),
        "positions": positions,
        "message": f"{len(positions)} open",
    }


def _find_position(rows: List[Dict[str, Any]], position_id: int) -> Optional[Dict[str, Any]]:
    for row in rows or []:
        try:
            if int(row.get("position_id")) == int(position_id):
                return row
        except (TypeError, ValueError):
            continue
    return None


def _protocol_volume(pos: Dict[str, Any]) -> int:
    try:
        vol = int(float(pos.get("volume")))
    except (TypeError, ValueError):
        raise AiOrderError("position volume is missing")
    if vol <= 0:
        raise AiOrderError("position volume is missing")
    return vol


def _local_open_order(conn: Dict[str, Any], position_id: int) -> Optional[Dict[str, Any]]:
    try:
        rows = repos.list_orders(
            str(conn["user_id"]),
            limit=200,
            status="open",
            account_id=int(conn["account_id"]),
        )
    except Exception as e:
        print(f"⚠️  AI list_orders failed: {e}")
        return None
    for row in rows:
        if str(row.get("broker_position_id") or "") == str(position_id):
            return row
    return None


def _sync_local(
    conn: Dict[str, Any],
    broker: Any,
    position_id: int,
    *,
    closed: bool,
    fields: Optional[Dict[str, Any]] = None,
) -> None:
    row = _local_open_order(conn, position_id)
    if row is None or not row.get("id"):
        return
    try:
        if closed:
            reconcile_order_vs_positions(row, [], broker=broker)
            return
        if fields:
            repos.update_order(str(row["id"]), **fields)
    except Exception as e:
        print(f"⚠️  AI order sync failed pos={position_id}: {e}")


def manage_ai_position(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Modify or close one position that already carries the requested label.

    Kill switch blocks new orders only. Closing and stop changes still run
    so an open AI trade can be flattened.
    """
    if not isinstance(payload, dict):
        raise AiOrderError("JSON object required")
    label = _parse_label(payload.get("label"))
    try:
        position_id = int(payload.get("position_id"))
    except (TypeError, ValueError):
        raise AiOrderError("position_id is required")
    if position_id <= 0:
        raise AiOrderError("position_id is required")
    action = str(payload.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        raise AiOrderError(
            "action must be leave, trail, move_sl_to_breakeven, partial_close, or close"
        )
    new_sl = _optional_number(payload.get("new_sl"), "new_sl")
    new_tp = _optional_number(payload.get("new_tp"), "new_tp")
    conn = resolve_connection(_optional_account_id(payload.get("account_id")))
    account_id = int(conn["account_id"])
    broker = broker_for_connection(conn)
    try:
        rows = broker.describe_open_positions()
    except Exception as e:
        return _trade_result(
            success=False,
            message=str(e),
            position_id=position_id,
            account_id=account_id,
        )
    pos = _find_position(rows or [], position_id)
    if pos is None:
        return _trade_result(
            success=False,
            message="position not found",
            position_id=position_id,
            account_id=account_id,
        )
    got = str(pos.get("label") or "")
    if got != label:
        shown = got or "empty"
        return _trade_result(
            success=False,
            message=f"position is labeled '{shown}', not {label}",
            position_id=position_id,
            account_id=account_id,
        )
    if action == "leave":
        return _trade_result(
            success=True,
            message="Position left unchanged",
            position_id=position_id,
            account_id=account_id,
        )
    if action == "close":
        try:
            volume = _protocol_volume(pos)
            broker.close_position(position_id=position_id, volume=volume)
        except Exception as e:
            return _trade_result(
                success=False,
                message=str(e),
                position_id=position_id,
                account_id=account_id,
            )
        _sync_local(conn, broker, position_id, closed=True)
        return _trade_result(
            success=True,
            message="Position closed",
            position_id=position_id,
            account_id=account_id,
        )
    if action == "partial_close":
        pct = _require_number(payload.get("close_percent"), "close_percent")
        if pct <= 0 or pct > 100:
            raise AiOrderError("close_percent must be between 0 and 100")
        try:
            full = _protocol_volume(pos)
        except AiOrderError as e:
            return _trade_result(
                success=False,
                message=e.message,
                position_id=position_id,
                account_id=account_id,
            )
        close_vol = int(round(full * pct / 100.0))
        if close_vol <= 0:
            return _trade_result(
                success=False,
                message="close volume too small",
                position_id=position_id,
                account_id=account_id,
            )
        if close_vol > full:
            close_vol = full
        try:
            broker.close_position(position_id=position_id, volume=close_vol)
        except Exception as e:
            return _trade_result(
                success=False,
                message=str(e),
                position_id=position_id,
                account_id=account_id,
            )
        if close_vol >= full:
            _sync_local(conn, broker, position_id, closed=True)
            return _trade_result(
                success=True,
                message="Position closed",
                position_id=position_id,
                account_id=account_id,
            )
        remain = _lots_from_protocol(full - close_vol, pos.get("broker_symbol"))
        fields: Dict[str, Any] = {}
        if remain is not None:
            fields["volume_lots"] = remain
        _sync_local(conn, broker, position_id, closed=False, fields=fields)
        return _trade_result(
            success=True,
            message="Position partially closed",
            position_id=position_id,
            account_id=account_id,
        )

    point_size = _point_size_for(pos.get("broker_symbol"))
    entry = pos.get("entry_price")
    try:
        entry_f = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        entry_f = None
    sl_target = new_sl
    tp_target = new_tp if new_tp is not None else pos.get("take_profit")
    if action == "move_sl_to_breakeven":
        if entry_f is None or entry_f <= 0:
            return _trade_result(
                success=False,
                message="position has no entry price",
                position_id=position_id,
                account_id=account_id,
            )
        sl_target = round_price(entry_f, point_size)
    elif action == "trail":
        if sl_target is None and new_tp is None:
            raise AiOrderError("trail requires new_sl or new_tp")
    if sl_target is not None:
        sl_target = round_price(float(sl_target), point_size)
    if tp_target is not None:
        try:
            tp_target = round_price(float(tp_target), point_size)
        except (TypeError, ValueError):
            tp_target = None
    if sl_target is None and tp_target is None:
        return _trade_result(
            success=False,
            message="nothing to amend",
            position_id=position_id,
            account_id=account_id,
        )
    try:
        broker.amend_position_sl_tp(
            position_id=position_id,
            stop_loss=sl_target,
            take_profit=float(tp_target) if tp_target is not None else None,
        )
    except Exception as e:
        return _trade_result(
            success=False,
            message=str(e),
            position_id=position_id,
            account_id=account_id,
        )
    fields = {}
    if sl_target is not None:
        fields["sl_price"] = float(sl_target)
    if tp_target is not None:
        fields["tp_price"] = float(tp_target)
    _sync_local(conn, broker, position_id, closed=False, fields=fields)
    if action == "move_sl_to_breakeven":
        message = "Stop loss moved to breakeven"
    elif sl_target is not None and new_tp is not None:
        message = "Stop loss and take profit updated"
    elif sl_target is not None:
        message = "Stop loss updated"
    else:
        message = "Take profit updated"
    return _trade_result(
        success=True,
        message=message,
        position_id=position_id,
        account_id=account_id,
    )


def ai_kill_status(account_id: Optional[int] = None) -> Dict[str, Any]:
    """Read the kill switch the next AI order would hit."""
    conn = resolve_connection(account_id)
    user = _user_for(conn)
    reason = kill_block_reason(conn, user)
    killed = reason is not None
    return {
        "success": True,
        "kill_switch": killed,
        "trading_enabled": not killed,
        "global_trading_enabled": bool(config.TRADING_ENABLED),
        "account_kill_switch": bool(conn.get("kill_switch")),
        "account_id": int(conn["account_id"]),
        "message": reason or "trading allowed",
    }
