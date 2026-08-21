"""Unit tests for claim-before-place logic with a fake supabase table layer."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _FakeQuery:
    def __init__(self, store: "_FakeSB", table: str):
        self.store = store
        self.table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: List[Tuple[str, Any]] = []
        self._on_conflict: Optional[str] = None

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

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        return self

    def order(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def execute(self):
        rows = self.store.tables.setdefault(self.table, [])
        if self._op == "insert":
            # PK conflict for processed_alerts
            if self.table == "factor_processed_alerts":
                uid = self._payload["user_id"]
                aid = self._payload["alert_id"]
                acc = self._payload.get("account_id")
                for r in rows:
                    if (
                        r["user_id"] == uid
                        and r["alert_id"] == aid
                        and r.get("account_id") == acc
                    ):
                        raise Exception("duplicate key")
            rows.append(dict(self._payload))
            return _FakeResult([dict(self._payload)])
        if self._op == "select":
            out = rows
            for col, val in self._filters:
                out = [r for r in out if r.get(col) == val]
            return _FakeResult(list(out))
        return _FakeResult([])


class _FakeSB:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


def test_claim_alert_once(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    assert repos.claim_alert("user-1", 42, "claimed", account_id=1) is True
    assert repos.claim_alert("user-1", 42, "claimed", account_id=1) is False
    assert repos.was_processed("user-1", 42, account_id=1) is True
    assert repos.was_processed("user-2", 42, account_id=1) is False
    # different user can claim same alert_id
    assert repos.claim_alert("user-2", 42, "claimed", account_id=1) is True
    # same user, different account can also claim
    assert repos.claim_alert("user-1", 42, "claimed", account_id=2) is True
