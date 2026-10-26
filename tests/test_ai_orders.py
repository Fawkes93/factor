"""AI execution API. Offline: no cTrader, Supabase, or Reaktor calls."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from factor import config
from factor.broker.base import OrderResult
from factor.broker.ctrader import CTraderBroker
from factor.broker.ctrader_client import normalize_open_position
from factor.db import repos as repos_mod
from factor.rules.presets import OrderPlan
from factor.services import ai_orders as ai
from factor.services.ai_orders import AI_ORDER_LABEL, AiOrderError

_ORIG_INSERT = repos_mod.insert_order


def _conn(account_id=6168349, kill=False, trading=True):
    return {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "account_id": account_id,
        "host": "demo.ctraderapi.com",
        "kill_switch": kill,
        "trading_enabled": trading,
        "access_token_enc": "enc",
    }


def _pos(**over):
    base = {
        "position_id": 67890,
        "side": "BUY",
        "entry_price": 2345.5,
        "stop_loss": 2335.0,
        "take_profit": 2365.0,
        "broker_symbol": "XAUUSD",
        "volume": 100,
        "label": AI_ORDER_LABEL,
        "comment": "run-1",
        "open_timestamp": 1_700_000_000_000,
        "bid": 2350.0,
        "ask": 2350.4,
        "unrealized_pnl": 4.2,
    }
    base.update(over)
    return base


class FakeBroker:
    def __init__(self):
        self.placed = []
        self.closed = []
        self.amended = []
        self.positions = []

    def place_labeled_order(self, **kwargs):
        self.placed.append(kwargs)
        return OrderResult(
            ok=True,
            status="open",
            broker_order_id="12345",
            message="order accepted",
            detail={"position_id": "67890"},
        )

    def describe_open_positions(self):
        return list(self.positions)

    def close_position(self, *, position_id, volume):
        self.closed.append({"position_id": position_id, "volume": volume})
        return {"ok": True}

    def amend_position_sl_tp(self, *, position_id, stop_loss=None, take_profit=None):
        self.amended.append(
            {
                "position_id": position_id,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        return {"ok": True}


def _market_body(**over):
    body = {
        "symbol": "XAUUSD",
        "side": "buy",
        "volume": 0.01,
        "order_type": "market",
        "entry_price": 2345.5,
        "stop_loss": 2335.0,
        "take_profit": 2365.0,
        "label": AI_ORDER_LABEL,
        "comment": "run-9",
    }
    body.update(over)
    return body


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def inserted():
    return {}


@pytest.fixture(autouse=True)
def _offline(monkeypatch, broker, inserted):
    monkeypatch.setattr(config, "TRADING_ENABLED", True)
    monkeypatch.setattr(config, "FACTOR_AI_ACCOUNT_ID", "")
    monkeypatch.setattr(ai.repos, "list_all_connections", lambda: [_conn()])
    monkeypatch.setattr(
        ai.repos,
        "get_user",
        lambda uid: {
            "id": uid,
            "kill_switch": False,
            "trading_enabled": True,
        },
    )
    monkeypatch.setattr(ai.repos, "list_orders", lambda *a, **k: [])

    def insert_order(**kwargs):
        inserted.update(kwargs)
        return {"id": "local-1"}

    monkeypatch.setattr(ai.repos, "insert_order", insert_order)
    monkeypatch.setattr(ai.repos, "update_order", lambda *a, **k: {"id": a[0] if a else None})
    monkeypatch.setattr(ai, "broker_for_connection", lambda conn: broker)


def test_normalize_position_keeps_ctrader_label():
    row = normalize_open_position(
        {
            "positionId": 67890,
            "price": 2345.5,
            "stopLoss": 2335.0,
            "takeProfit": 2365.0,
            "volume": 100,
            "tradeData": {
                "symbolId": 1,
                "volume": 100,
                "tradeSide": 1,
                "label": AI_ORDER_LABEL,
                "comment": "run-1",
                "openTimestamp": 1_700_000_000_000,
            },
        },
        {1: "XAUUSD"},
    )
    assert row is not None
    assert row["label"] == AI_ORDER_LABEL
    assert row["comment"] == "run-1"
    assert row["broker_symbol"] == "XAUUSD"
    assert row["volume"] == 100
    assert row["open_timestamp"] == 1_700_000_000_000
    assert row["side"] == "BUY"


def test_place_market_uses_relative_stops_and_ai_label(broker, inserted):
    out = ai.place_ai_order(_market_body())
    assert out["success"] is True
    assert out["order_id"] == "12345"
    assert out["position_id"] == "67890"
    assert out["message"] == "Order placed"
    assert out["account_id"] == 6168349
    placed = broker.placed[0]
    assert placed["label"] == AI_ORDER_LABEL
    assert placed["order_type"] == "market"
    assert placed["comment"] == "run-9"
    assert placed["volume_protocol"] == 100
    assert placed["relative_sl"] == 1_050_000
    assert placed["relative_tp"] == 1_950_000
    assert placed["stop_loss"] is None
    assert placed["take_profit"] is None
    assert placed["limit_price"] is None
    assert inserted["kind"] == "ai"
    assert inserted["label"] == AI_ORDER_LABEL
    assert inserted["symbol"] == "XAU/USD"
    assert inserted["side"] == "BUY"
    assert inserted["reaktor_alert_id"] == 0
    assert inserted["detail"]["breakeven"]["enabled"] is False
    assert inserted["detail"]["source"] == "ai"


def test_place_limit_uses_absolute_prices(broker):
    body = _market_body(order_type="limit")
    out = ai.place_ai_order(body)
    assert out["success"] is True
    placed = broker.placed[0]
    assert placed["order_type"] == "limit"
    assert placed["limit_price"] == 2345.5
    assert placed["stop_price"] is None
    assert placed["stop_loss"] == 2335.0
    assert placed["take_profit"] == 2365.0
    assert placed["relative_sl"] == 0
    assert placed["relative_tp"] == 0


def test_place_stop_sell(broker):
    out = ai.place_ai_order(
        _market_body(
            side="sell",
            order_type="stop",
            stop_loss=2355.0,
            take_profit=2330.0,
        )
    )
    assert out["success"] is True
    placed = broker.placed[0]
    assert placed["side"] == "SELL"
    assert placed["order_type"] == "stop"
    assert placed["stop_price"] == 2345.5
    assert placed["limit_price"] is None
    assert placed["stop_loss"] == 2355.0
    assert placed["take_profit"] == 2330.0


def test_place_rejects_other_labels():
    with pytest.raises(AiOrderError) as exc:
        ai.place_ai_order(_market_body(label="factor"))
    assert "AI-GOLD-v1" in exc.value.message
    assert "MANAN" in exc.value.message


def test_place_accepts_manan_label(broker, inserted):
    out = ai.place_ai_order(_market_body(label="MANAN"))
    assert out["success"] is True
    assert broker.placed[0]["label"] == "MANAN"
    assert inserted["label"] == "MANAN"


def test_place_rejects_buy_stop_above_entry():
    with pytest.raises(AiOrderError) as exc:
        ai.place_ai_order(_market_body(stop_loss=2400.0))
    assert "stop_loss" in exc.value.message


def test_limit_requires_entry_price():
    body = _market_body(order_type="limit")
    body.pop("entry_price")
    with pytest.raises(AiOrderError) as exc:
        ai.place_ai_order(body)
    assert "entry_price" in exc.value.message


def test_kill_switch_blocks_new_orders(monkeypatch, broker):
    monkeypatch.setattr(ai.repos, "list_all_connections", lambda: [_conn(kill=True)])
    out = ai.place_ai_order(_market_body())
    assert out["success"] is False
    assert out["message"] == "kill_switch"
    assert out["kill_switch"] is True
    assert broker.placed == []


def test_global_trading_flag_blocks_new_orders(monkeypatch, broker):
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    out = ai.place_ai_order(_market_body())
    assert out["success"] is False
    assert out["message"] == "trading_disabled"
    assert broker.placed == []


def test_kill_status_reads_account_and_global_flag(monkeypatch):
    ok = ai.ai_kill_status()
    assert ok["kill_switch"] is False
    assert ok["trading_enabled"] is True
    assert ok["account_id"] == 6168349
    monkeypatch.setattr(config, "TRADING_ENABLED", False)
    blocked = ai.ai_kill_status()
    assert blocked["kill_switch"] is True
    assert blocked["global_trading_enabled"] is False
    assert blocked["message"] == "trading_disabled"


def test_single_account_is_the_default():
    out = ai.place_ai_order(_market_body())
    assert out["account_id"] == 6168349


def test_several_accounts_require_an_id(monkeypatch):
    monkeypatch.setattr(
        ai.repos, "list_all_connections", lambda: [_conn(1), _conn(2)]
    )
    with pytest.raises(AiOrderError) as exc:
        ai.place_ai_order(_market_body())
    assert exc.value.message == "account_id is required"
    out = ai.place_ai_order(_market_body(account_id=2))
    assert out["account_id"] == 2


def test_env_account_is_overridden_by_the_body(monkeypatch):
    monkeypatch.setattr(config, "FACTOR_AI_ACCOUNT_ID", "1")
    monkeypatch.setattr(
        ai.repos, "list_all_connections", lambda: [_conn(1), _conn(2)]
    )
    assert ai.place_ai_order(_market_body())["account_id"] == 1
    assert ai.place_ai_order(_market_body(account_id=2))["account_id"] == 2


def test_list_returns_only_the_ai_label(broker):
    broker.positions = [
        _pos(),
        _pos(position_id=99, label="factor", unrealized_pnl=1),
    ]
    out = ai.list_ai_positions()
    assert out["success"] is True
    assert len(out["positions"]) == 1
    row = out["positions"][0]
    assert row["position_id"] == "67890"
    assert row["symbol"] == "XAUUSD"
    assert row["side"] == "buy"
    assert row["volume"] == 0.01
    assert row["entry_price"] == 2345.5
    assert row["current_price"] == 2350.0
    assert row["stop_loss"] == 2335.0
    assert row["take_profit"] == 2365.0
    assert row["unrealized_pnl"] == 4.2
    opened = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert row["opened_at"] == opened.isoformat().replace("+00:00", "Z")
    assert row["label"] == AI_ORDER_LABEL


def test_list_rejects_a_reaktor_label():
    with pytest.raises(AiOrderError):
        ai.list_ai_positions(label="factor")


def test_list_can_return_manan_only(broker):
    broker.positions = [
        _pos(),
        _pos(position_id=99, label="MANAN"),
    ]
    out = ai.list_ai_positions(label="MANAN")
    assert out["success"] is True
    assert out["label"] == "MANAN"
    assert [row["position_id"] for row in out["positions"]] == ["99"]


def test_manage_closes_a_manan_position(broker):
    broker.positions = [_pos(label="MANAN")]
    out = ai.manage_ai_position(
        {"position_id": "67890", "action": "close", "label": "MANAN"}
    )
    assert out["success"] is True
    assert broker.closed == [{"position_id": 67890, "volume": 100}]


def test_manage_refuses_a_reaktor_position(broker):
    broker.positions = [_pos(label="factor")]
    out = ai.manage_ai_position({"position_id": "67890", "action": "close"})
    assert out["success"] is False
    assert "factor" in out["message"]
    assert broker.closed == []
    assert broker.amended == []


def test_manage_leave_does_not_touch_the_broker(broker):
    broker.positions = [_pos()]
    out = ai.manage_ai_position({"position_id": 67890, "action": "leave"})
    assert out["success"] is True
    assert out["message"] == "Position left unchanged"
    assert broker.closed == []
    assert broker.amended == []


def test_manage_close(broker):
    broker.positions = [_pos()]
    out = ai.manage_ai_position({"position_id": "67890", "action": "close"})
    assert out["success"] is True
    assert out["message"] == "Position closed"
    assert broker.closed == [{"position_id": 67890, "volume": 100}]


def test_manage_close_records_the_local_order(monkeypatch, broker):
    broker.positions = [_pos()]
    saved = {}

    def update_order(order_id, **fields):
        saved["id"] = order_id
        saved["fields"] = fields
        return {"id": order_id}

    monkeypatch.setattr(ai.repos, "update_order", update_order)
    monkeypatch.setattr(
        ai.repos,
        "list_orders",
        lambda *a, **k: [
            {
                "id": "ord-1",
                "broker_position_id": "67890",
                "kind": "ai",
                "symbol": "XAU/USD",
                "detail": {},
            }
        ],
    )
    out = ai.manage_ai_position({"position_id": 67890, "action": "close"})
    assert out["success"] is True
    assert saved["id"] == "ord-1"
    assert saved["fields"]["status"] == "closed"


def test_manage_partial_close(monkeypatch, broker):
    broker.positions = [_pos()]
    saved = {}
    monkeypatch.setattr(
        ai.repos,
        "update_order",
        lambda order_id, **fields: saved.update(fields) or {"id": order_id},
    )
    monkeypatch.setattr(
        ai.repos,
        "list_orders",
        lambda *a, **k: [{"id": "ord-1", "broker_position_id": "67890", "detail": {}}],
    )
    out = ai.manage_ai_position(
        {"position_id": 67890, "action": "partial_close", "close_percent": 50}
    )
    assert out["success"] is True
    assert out["message"] == "Position partially closed"
    assert broker.closed == [{"position_id": 67890, "volume": 50}]
    assert saved["volume_lots"] == 0.005


def test_manage_move_sl_to_breakeven(broker):
    broker.positions = [_pos()]
    out = ai.manage_ai_position(
        {"position_id": 67890, "action": "move_sl_to_breakeven"}
    )
    assert out["success"] is True
    assert out["message"] == "Stop loss moved to breakeven"
    assert broker.amended == [
        {"position_id": 67890, "stop_loss": 2345.5, "take_profit": 2365.0}
    ]


def test_manage_trail_sets_the_new_stop(broker):
    broker.positions = [_pos()]
    out = ai.manage_ai_position(
        {"position_id": 67890, "action": "trail", "new_sl": 2340.0}
    )
    assert out["success"] is True
    assert out["message"] == "Stop loss updated"
    assert broker.amended[0]["stop_loss"] == 2340.0
    assert broker.amended[0]["take_profit"] == 2365.0


def test_manage_still_runs_when_the_kill_switch_is_on(monkeypatch, broker):
    monkeypatch.setattr(ai.repos, "list_all_connections", lambda: [_conn(kill=True)])
    broker.positions = [_pos()]
    out = ai.manage_ai_position({"position_id": 67890, "action": "close"})
    assert out["success"] is True
    assert broker.closed


def test_missing_position(broker):
    broker.positions = []
    out = ai.manage_ai_position({"position_id": 5, "action": "close"})
    assert out["success"] is False
    assert out["message"] == "position not found"


def test_insert_falls_back_when_label_column_is_missing(monkeypatch):
    class _Result:
        def __init__(self, data):
            self.data = data

    class _SB:
        def __init__(self):
            self.payloads = []
            self._payload = None

        def table(self, name):
            assert name == "factor_orders"
            return self

        def insert(self, payload):
            self._payload = payload
            self.payloads.append(dict(payload))
            return self

        def execute(self):
            if "label" in (self._payload or {}) and len(self.payloads) == 1:
                raise Exception('column "label" does not exist')
            return _Result([dict(self._payload)])

    sb = _SB()
    monkeypatch.setattr(repos_mod, "get_supabase", lambda: sb)
    row = _ORIG_INSERT(
        user_id="u",
        reaktor_alert_id=0,
        kind="ai",
        symbol="XAU/USD",
        side="BUY",
        status="open",
        label=AI_ORDER_LABEL,
        detail={"source": "ai"},
    )
    assert row["detail"]["label"] == AI_ORDER_LABEL
    assert "label" not in row
    assert len(sb.payloads) == 2
    assert sb.payloads[0]["label"] == AI_ORDER_LABEL
    assert "label" not in sb.payloads[1]
    assert sb.payloads[1]["detail"]["label"] == AI_ORDER_LABEL


def test_insert_does_not_retry_an_unrelated_error(monkeypatch):
    class _SB:
        def __init__(self):
            self.calls = 0

        def table(self, name):
            return self

        def insert(self, payload):
            return self

        def execute(self):
            self.calls += 1
            raise Exception("connection reset")

    sb = _SB()
    monkeypatch.setattr(repos_mod, "get_supabase", lambda: sb)
    with pytest.raises(Exception, match="connection reset"):
        _ORIG_INSERT(
            user_id="u",
            reaktor_alert_id=0,
            kind="ai",
            symbol="XAU/USD",
            side="BUY",
            status="open",
            label=AI_ORDER_LABEL,
        )
    assert sb.calls == 1


def test_reaktor_market_orders_keep_the_factor_label(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def place_market(self, **kwargs):
            captured.update(kwargs)
            return {
                "order": {"orderId": "55"},
                "position": {"positionId": "66"},
            }

    monkeypatch.setattr("factor.broker.ctrader.CTraderClient", FakeClient)
    broker = CTraderBroker(
        access_token="tok",
        account_id="1",
        host="demo.ctraderapi.com",
        client_id="id",
        client_secret="secret",
        dry_run=False,
    )
    plan = OrderPlan(
        symbol="XAU/USD",
        broker_symbol="XAUUSD",
        side="BUY",
        volume_lots=0.01,
        entry_price=2345.5,
        sl_price=2335.0,
        tp_price=2365.0,
        sl_points=1050,
        tp_points=1950,
        point_size=0.01,
        kind="candle",
        alert_id=42,
    )
    result = broker.place_market_with_sl_tp(plan)
    assert result.ok is True
    assert captured["label"] == "factor"
    assert captured["comment"].startswith("factor alert 42")


def _client():
    from factor.api.app import create_app

    return TestClient(create_app())


def test_http_requires_admin_token(monkeypatch):
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "")
    client = _client()
    missing = client.post("/api/ai/orders", json=_market_body())
    assert missing.status_code == 503
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "secret")
    denied = client.post(
        "/api/ai/orders",
        json=_market_body(),
        headers={"Authorization": "Bearer nope"},
    )
    assert denied.status_code == 401


def test_http_place_and_aliases(monkeypatch, broker):
    monkeypatch.setattr(config, "FACTOR_ADMIN_TOKEN", "secret")
    client = _client()
    headers = {"Authorization": "Bearer secret"}
    placed = client.post("/api/ai/orders", json=_market_body(), headers=headers)
    assert placed.status_code == 200
    body = placed.json()
    assert body["success"] is True
    assert body["order_id"] == "12345"
    assert body["position_id"] == "67890"
    alias = client.post(
        "/api/orders",
        json=_market_body(),
        headers={"X-Api-Key": "secret"},
    )
    assert alias.status_code == 200
    assert alias.json()["success"] is True
    bad = client.post(
        "/api/ai/orders",
        content=b"not-json",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert bad.status_code == 400
    assert bad.json()["success"] is False
    broker.positions = [_pos()]
    listed = client.get("/api/positions", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["positions"][0]["position_id"] == "67890"
    managed = client.post(
        "/api/positions/manage",
        json={"position_id": "67890", "action": "leave"},
        headers=headers,
    )
    assert managed.status_code == 200
    assert managed.json()["message"] == "Position left unchanged"
    killed = client.get("/api/ai/kill", headers=headers)
    assert killed.status_code == 200
    assert killed.json()["kill_switch"] is False
    assert len(broker.placed) == 2
