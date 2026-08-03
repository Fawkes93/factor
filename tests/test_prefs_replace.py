"""replace_prefs_for_user keeps disabled rows when only_enabled=False."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4


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

        def match(r):
            return all(r.get(c) == v for c, v in self._filters)

        if self._op == "delete":
            kept = [r for r in rows if not match(r)]
            self.store.tables[self.table] = kept
            return _FakeResult([])

        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for item in items:
                row = dict(item)
                row.setdefault("id", str(uuid4()))
                rows.append(row)
                out.append(dict(row))
            return _FakeResult(out)

        if self._op == "update":
            out = []
            for r in rows:
                if match(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return _FakeResult(out)

        if self._op == "select":
            out = [dict(r) for r in rows if match(r)]
            if self._limit is not None:
                out = out[: self._limit]
            return _FakeResult(out)

        return _FakeResult([])


class _FakeSB:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


def test_replace_keeps_disabled_rows(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-1"
    saved = repos.replace_prefs_for_user(
        uid,
        [
            {
                "kind": "candle",
                "symbol": "XAU/USD",
                "enabled": True,
                "max_concurrent": 1,
                "max_per_day": 5,
                "volume_lots": 0.01,
                "sl_points": 1000,
                "tp_points": 1000,
            },
            {
                "kind": "spike",
                "symbol": "XAU/USD",
                "enabled": False,
                "max_concurrent": 2,
                "max_per_day": 3,
                "volume_lots": 0.02,
                "sl_points": 500,
                "tp_points": 500,
            },
        ],
        only_enabled=False,
    )
    assert len(saved) == 2
    by_kind = {r["kind"]: r for r in saved}
    assert by_kind["candle"]["enabled"] is True
    assert by_kind["spike"]["enabled"] is False
    assert by_kind["spike"]["sl_points"] == 500
    assert by_kind["spike"]["volume_lots"] == 0.02

    listed = repos.list_prefs(uid)
    assert len(listed) == 2
    assert {r["kind"] for r in listed} == {"candle", "spike"}


def test_only_enabled_drops_disabled(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-2"
    saved = repos.replace_prefs_for_user(
        uid,
        [
            {"kind": "candle", "symbol": "XAU/USD", "enabled": True},
            {"kind": "spike", "symbol": "XAU/USD", "enabled": False, "sl_points": 777},
        ],
        only_enabled=True,
    )
    assert len(saved) == 1
    assert saved[0]["kind"] == "candle"


def test_delete_via_omission(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-3"
    repos.replace_prefs_for_user(
        uid,
        [
            {"kind": "candle", "symbol": "XAU/USD", "enabled": True},
            {"kind": "spike", "symbol": "XAU/USD", "enabled": False},
        ],
        only_enabled=False,
    )
    # Trash spike — omit from payload
    saved = repos.replace_prefs_for_user(
        uid,
        [{"kind": "candle", "symbol": "XAU/USD", "enabled": True}],
        only_enabled=False,
    )
    assert len(saved) == 1
    assert saved[0]["kind"] == "candle"
    assert len(repos.list_prefs(uid)) == 1
