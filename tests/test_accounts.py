"""Multi-account OAuth merge, identity, and connection helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from factor.auth.ctrader_oauth import (
    account_id_from_row,
    account_is_live,
    broker_name_from_row,
    fetch_accounts_all_hosts,
    host_for_account,
    pick_default_account,
    trader_login_from_row,
)


def test_fetch_accounts_all_hosts_merges_demo_and_live(monkeypatch):
    from factor.auth import ctrader_oauth as oauth

    def fake_fetch(*, access_token, host):
        if host.startswith("demo"):
            return [
                {"ctidTraderAccountId": 10, "isLive": False, "traderLogin": 10093658},
                {"ctidTraderAccountId": 11, "isLive": False, "traderLogin": 5866231},
            ]
        return [
            {"ctidTraderAccountId": 12, "isLive": True, "traderLogin": 6168349},
        ]

    monkeypatch.setattr(oauth, "fetch_accounts_for_token", fake_fetch)
    accounts = fetch_accounts_all_hosts(access_token="tok")
    ids = sorted(account_id_from_row(a) for a in accounts)
    assert ids == [10, 11, 12]
    live = [a for a in accounts if account_is_live(a)]
    demo = [a for a in accounts if not account_is_live(a)]
    assert len(live) == 1
    assert len(demo) == 2
    assert host_for_account(live[0]).startswith("live")
    assert host_for_account(demo[0]).startswith("demo")


def test_pick_default_prefers_demo():
    accounts = [
        {"ctidTraderAccountId": 12, "isLive": True, "traderLogin": 1},
        {"ctidTraderAccountId": 10, "isLive": False, "traderLogin": 2},
    ]
    pick = pick_default_account(accounts, prefer_demo=True)
    assert account_id_from_row(pick) == 10


def test_account_field_helpers():
    row = {
        "ctidTraderAccountId": "6168349",
        "traderLogin": 6168349,
        "brokerTitle": "Raw Trading Ltd",
        "isLive": True,
    }
    assert account_id_from_row(row) == 6168349
    assert trader_login_from_row(row) == "6168349"
    assert broker_name_from_row(row) == "Raw Trading Ltd"
    assert account_is_live(row) is True
    assert host_for_account(row) == "live.ctraderapi.com"


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
        self._on_conflict: Optional[str] = None
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

    def in_(self, col, vals):
        self._filters.append((col, ("in", list(vals))))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.store.tables.setdefault(self.table, [])

        def match(r):
            for c, v in self._filters:
                if isinstance(v, tuple) and v and v[0] == "in":
                    if r.get(c) not in v[1]:
                        return False
                elif r.get(c) != v:
                    return False
            return True

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

        if self._op == "upsert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            keys = [k.strip() for k in (self._on_conflict or "").split(",") if k.strip()]
            out = []
            for item in items:
                row = dict(item)
                found = None
                if keys:
                    for r in rows:
                        if all(r.get(k) == row.get(k) for k in keys):
                            found = r
                            break
                if found:
                    found.update(row)
                    out.append(dict(found))
                else:
                    row.setdefault("id", str(uuid4()))
                    rows.append(row)
                    out.append(dict(row))
            return _FakeResult(out)

        if self._op == "delete":
            kept = [r for r in rows if not match(r)]
            n = len(rows) - len(kept)
            self.store.tables[self.table] = kept
            return _FakeResult([{}] * n)

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


def _wire(monkeypatch, fake):
    from factor.db import repos
    from factor.db import client as db_client

    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)
    return repos


def test_upsert_two_connections_same_user(monkeypatch):
    repos = _wire(monkeypatch, _FakeSB())
    uid = str(uuid4())
    repos.upsert_ctrader_connection(
        user_id=uid,
        account_id=10,
        host="demo.ctraderapi.com",
        access_token_enc="a",
        refresh_token_enc="r",
        trader_login="10093658",
        broker_name="Raw Trading Ltd",
        is_live=False,
    )
    repos.upsert_ctrader_connection(
        user_id=uid,
        account_id=12,
        host="live.ctraderapi.com",
        access_token_enc="a",
        refresh_token_enc="r",
        trader_login="6168349",
        broker_name="Raw Trading Ltd",
        is_live=True,
    )
    rows = repos.list_connections(uid)
    assert len(rows) == 2
    assert {int(r["account_id"]) for r in rows} == {10, 12}
    assert repos.get_connection(uid, 12)["is_live"] is True


def test_find_user_by_existing_account(monkeypatch):
    fake = _FakeSB()
    repos = _wire(monkeypatch, fake)
    uid = str(uuid4())
    fake.tables["factor_users"] = [{"id": uid, "ctrader_ctid": "10093658"}]
    fake.tables["factor_ctrader_connections"] = [
        {"user_id": uid, "account_id": 10, "host": "demo.ctraderapi.com"}
    ]
    found = repos.find_user_for_accounts([10, 12], ["6168349"])
    assert found["id"] == uid


def test_prune_ungranted_keeps_prefs(monkeypatch):
    repos = _wire(monkeypatch, _FakeSB())
    uid = str(uuid4())
    for aid in (10, 11):
        repos.upsert_ctrader_connection(
            user_id=uid,
            account_id=aid,
            host="demo.ctraderapi.com",
            access_token_enc="a",
            refresh_token_enc="r",
        )
    n = repos.prune_ungranted_connections(uid, [10])
    assert n == 1
    assert [int(r["account_id"]) for r in repos.list_connections(uid)] == [10]


def test_account_label_and_public_connection():
    from factor.db import repos

    conn = {
        "account_id": 12,
        "host": "live.ctraderapi.com",
        "trader_login": "6168349",
        "broker_name": "Raw Trading Ltd",
        "is_live": True,
        "kill_switch": False,
        "trading_enabled": True,
        "access_token_enc": "secret",
        "refresh_token_enc": "secret",
    }
    pub = repos.public_connection(conn)
    assert "access_token_enc" not in pub
    assert pub["label"] == "Raw Trading Ltd · Live · 6168349"
    assert pub["is_live"] is True


def test_adopt_legacy_users_moves_prefs(monkeypatch):
    """Old 1-account user keeps prefs; session user already has the same account."""
    fake = _FakeSB()
    repos = _wire(monkeypatch, fake)
    dest = str(uuid4())
    src = str(uuid4())
    fake.tables["factor_users"] = [
        {"id": dest, "ctrader_ctid": "6168349", "created_at": "2026-01-01T00:00:00Z"},
        {"id": src, "ctrader_ctid": "10093658", "created_at": "2026-01-02T00:00:00Z"},
    ]
    fake.tables["factor_ctrader_connections"] = [
        {
            "id": str(uuid4()),
            "user_id": dest,
            "account_id": 10,
            "host": "demo.ctraderapi.com",
            "trader_login": "10093658",
        },
        {
            "id": str(uuid4()),
            "user_id": dest,
            "account_id": 12,
            "host": "live.ctraderapi.com",
            "trader_login": "6168349",
        },
        {
            "id": str(uuid4()),
            "user_id": src,
            "account_id": 10,
            "host": "demo.ctraderapi.com",
            "trader_login": "10093658",
        },
    ]
    fake.tables["factor_user_alert_prefs"] = [
        {
            "id": str(uuid4()),
            "user_id": src,
            "account_id": 10,
            "kind": "candle",
            "symbol": "XAU/USD",
            "enabled": True,
        }
    ]

    n = repos.adopt_legacy_users(dest)
    assert n == 1
    assert repos.get_user(src) is None
    prefs = repos.list_prefs(dest, 10)
    assert len(prefs) == 1
    assert prefs[0]["kind"] == "candle"
    assert prefs[0]["user_id"] == dest
    # dest still has both accounts; leftover src connection is gone
    assert {int(c["account_id"]) for c in repos.list_connections(dest)} == {10, 12}


def test_merge_rewrites_account_id_zero_prefs(monkeypatch):
    fake = _FakeSB()
    repos = _wire(monkeypatch, fake)
    dest = str(uuid4())
    src = str(uuid4())
    fake.tables["factor_users"] = [
        {"id": dest, "created_at": "a"},
        {"id": src, "created_at": "b"},
    ]
    fake.tables["factor_ctrader_connections"] = [
        {"id": str(uuid4()), "user_id": dest, "account_id": 10},
        {"id": str(uuid4()), "user_id": src, "account_id": 10},
    ]
    fake.tables["factor_user_alert_prefs"] = [
        {
            "id": str(uuid4()),
            "user_id": src,
            "account_id": 0,
            "kind": "spike",
            "symbol": "XAU/USD",
            "enabled": True,
        }
    ]
    repos.merge_user_into(src, dest)
    prefs = repos.list_prefs(dest, 10)
    assert len(prefs) == 1
    assert prefs[0]["kind"] == "spike"
    assert int(prefs[0]["account_id"]) == 10
