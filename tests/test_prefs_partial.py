"""Single-alert update keeps every other row and every untouched field."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pytest


class _FakeResult:
    def __init__(self, data=None):
        self.data = data


class _FakeQuery:
    def __init__(self, store: "_FakeSB", table: str):
        self.store = store
        self.table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: List[Tuple[str, Any]] = []
        self._limit: Optional[int] = None

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.store.tables.setdefault(self.table, [])

        def match(row):
            for col, val in self._filters:
                if row.get(col) != val:
                    return False
            return True

        if self._op == "delete":
            self.store.tables[self.table] = [r for r in rows if not match(r)]
            return _FakeResult([])
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", str(uuid4()))
            rows.append(row)
            return _FakeResult([dict(row)])
        if self._op == "update":
            out = []
            for row in rows:
                if match(row):
                    row.update(self._payload)
                    out.append(dict(row))
            return _FakeResult(out)
        out = [dict(r) for r in rows if match(r)]
        if self._limit is not None:
            out = out[: self._limit]
        return _FakeResult(out)


class _FakeSB:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


def _wire(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)
    return repos, fake


def _row(**extra):
    base = {
        "id": extra.pop("id", str(uuid4())),
        "user_id": "u",
        "account_id": 10,
        "kind": "candle",
        "symbol": "XAU/USD",
        "enabled": True,
        "max_concurrent": 1,
        "max_per_day": 5,
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 2000,
        "breakeven_half_tp": True,
        "breakeven_fraction": 0.5,
        "sl_tp_override": False,
    }
    base.update(extra)
    return base


def test_update_pref_changes_only_that_field(monkeypatch):
    repos, fake = _wire(monkeypatch)
    fake.tables["factor_user_alert_prefs"] = [
        _row(id="p1", volume_lots=0.01),
        _row(id="p2", symbol="EUR/USD", volume_lots=0.03),
    ]
    saved = repos.update_pref("u", 10, "candle", "XAU/USD", {"volume_lots": 0.02})
    assert saved["volume_lots"] == 0.02
    assert saved["sl_points"] == 1000
    assert saved["tp_points"] == 2000
    assert saved["enabled"] is True
    other = repos.get_pref("u", 10, "candle", "EUR/USD")
    assert other["volume_lots"] == 0.03


def test_update_pref_ignores_identity_and_unknown_keys(monkeypatch):
    repos, fake = _wire(monkeypatch)
    fake.tables["factor_user_alert_prefs"] = [_row(id="p1")]
    saved = repos.update_pref(
        "u",
        10,
        "candle",
        "XAU/USD",
        {"volume_lots": 0.05, "kind": "spike", "symbol": "BTC/USD", "nope": 1},
    )
    assert saved["kind"] == "candle"
    assert saved["symbol"] == "XAU/USD"
    assert saved["volume_lots"] == 0.05


def test_update_pref_missing_row(monkeypatch):
    repos, _fake = _wire(monkeypatch)
    with pytest.raises(LookupError):
        repos.update_pref("u", 10, "candle", "XAU/USD", {"volume_lots": 0.02})


def test_update_pref_crt_cannot_enable_breakeven(monkeypatch):
    repos, fake = _wire(monkeypatch)
    fake.tables["factor_user_alert_prefs"] = [
        _row(id="crt1", kind="crt", symbol="XAU/USD", breakeven_half_tp=False)
    ]
    saved = repos.update_pref(
        "u", 10, "crt", "XAU/USD", {"breakeven_half_tp": True, "volume_lots": 0.04}
    )
    assert saved["breakeven_half_tp"] is False
    assert saved["sl_tp_override"] is False
    assert saved["volume_lots"] == 0.04


def test_delete_pref_removes_one_row(monkeypatch):
    repos, fake = _wire(monkeypatch)
    fake.tables["factor_user_alert_prefs"] = [
        _row(id="p1"),
        _row(id="p2", symbol="EUR/USD"),
    ]
    assert repos.delete_pref("u", 10, "candle", "XAU/USD") is True
    assert repos.get_pref("u", 10, "candle", "XAU/USD") is None
    assert repos.get_pref("u", 10, "candle", "EUR/USD")["id"] == "p2"
    assert repos.delete_pref("u", 10, "candle", "XAU/USD") is False
