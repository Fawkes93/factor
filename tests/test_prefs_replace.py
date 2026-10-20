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
        account_id=1001,
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
        account_id=1001,
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
        account_id=1001,
    )
    # Trash spike — omit from payload
    saved = repos.replace_prefs_for_user(
        uid,
        [{"kind": "candle", "symbol": "XAU/USD", "enabled": True}],
        only_enabled=False,
        account_id=1001,
    )
    assert len(saved) == 1
    assert saved[0]["kind"] == "candle"
    assert len(repos.list_prefs(uid, 1001)) == 1


def test_replace_does_not_wipe_other_account(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-4"
    repos.replace_prefs_for_user(
        uid,
        [{"kind": "candle", "symbol": "XAU/USD", "enabled": True}],
        account_id=11,
    )
    repos.replace_prefs_for_user(
        uid,
        [{"kind": "spike", "symbol": "XAU/USD", "enabled": True}],
        account_id=22,
    )
    assert len(repos.list_prefs(uid, 11)) == 1
    assert repos.list_prefs(uid, 11)[0]["kind"] == "candle"
    assert len(repos.list_prefs(uid, 22)) == 1
    assert repos.list_prefs(uid, 22)[0]["kind"] == "spike"

    repos.replace_prefs_for_user(
        uid,
        [{"kind": "candle", "symbol": "EUR/USD", "enabled": True}],
        account_id=11,
    )
    assert repos.list_prefs(uid, 11)[0]["symbol"] == "EUR/USD"
    assert repos.list_prefs(uid, 22)[0]["kind"] == "spike"


def test_session_kinds_force_breakeven_off(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-session"
    saved = repos.replace_prefs_for_user(
        uid,
        [
            {
                "kind": "asia_breakout_h",
                "symbol": "XAU/USD",
                "enabled": True,
                "breakeven_half_tp": True,
                "sl_tp_override": True,
                "sl_points": 700,
                "tp_points": 1400,
            },
            {
                "kind": "dawn_cross",
                "symbol": "XAU/USD",
                "enabled": True,
                "breakeven_half_tp": True,
                "sl_tp_override": True,
            },
        ],
        account_id=1001,
    )
    overnight = next(r for r in saved if r["kind"] == "asia_breakout_h")
    dawn = next(r for r in saved if r["kind"] == "dawn_cross")
    assert overnight["breakeven_half_tp"] is False
    assert overnight["sl_tp_override"] is True
    assert overnight["sl_points"] == 700
    assert dawn["breakeven_half_tp"] is False
    assert dawn["sl_tp_override"] is False


def test_crt_forces_breakeven_off(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-crt"
    saved = repos.replace_prefs_for_user(
        uid,
        [
            {
                "kind": "crt",
                "symbol": "XAU/USD",
                "enabled": True,
                "breakeven_half_tp": True,
                "sl_tp_override": True,
                "sl_points": 1000,
                "tp_points": 1000,
            }
        ],
        account_id=1001,
    )
    crt = saved[0]
    assert crt["kind"] == "crt"
    assert crt["breakeven_half_tp"] is False
    assert crt["sl_tp_override"] is False


def test_asia_breakout_forces_breakeven_off(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-asia"
    saved = repos.replace_prefs_for_user(
        uid,
        [
            {
                "kind": "asia_breakout",
                "symbol": "XAU/USD",
                "enabled": True,
                "breakeven_half_tp": True,
                "breakeven_fraction": 0.9,
            }
        ],
        account_id=1001,
    )
    assert len(saved) == 1
    assert saved[0]["breakeven_half_tp"] is False
    assert saved[0]["sl_tp_override"] is False
    listed = repos.list_prefs(uid, 1001)
    assert listed[0]["breakeven_half_tp"] is False
    assert listed[0]["sl_tp_override"] is False


def test_asia_sl_tp_override_persists_and_keeps_breakeven_off(monkeypatch):
    from factor.db import repos
    from factor.db import client as db_client

    fake = _FakeSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-asia-override"
    saved = repos.replace_prefs_for_user(
        uid,
        [
            {
                "kind": "asia_breakout",
                "symbol": "XAU/USD",
                "enabled": True,
                "sl_points": 800,
                "tp_points": 1600,
                "sl_tp_override": True,
                "breakeven_half_tp": True,
            },
            {
                "kind": "candle",
                "symbol": "EUR/USD",
                "enabled": True,
                "sl_tp_override": True,
            },
        ],
        account_id=1001,
    )
    asia = next(r for r in saved if r["kind"] == "asia_breakout")
    candle = next(r for r in saved if r["kind"] == "candle")
    assert asia["sl_tp_override"] is True
    assert asia["breakeven_half_tp"] is False
    assert asia["sl_points"] == 800
    assert asia["tp_points"] == 1600
    assert candle["sl_tp_override"] is False

    listed = repos.list_prefs(uid, 1001)
    asia_l = next(r for r in listed if r["kind"] == "asia_breakout")
    candle_l = next(r for r in listed if r["kind"] == "candle")
    assert asia_l["sl_tp_override"] is True
    assert asia_l["breakeven_half_tp"] is False
    assert candle_l["sl_tp_override"] is False


def test_asia_override_errors_until_column_exists(monkeypatch):
    """Saving override=true must not pretend it stuck when the column is missing."""
    import pytest

    from factor.db import repos
    from factor.db import client as db_client

    class _NoOverrideSB(_FakeSB):
        def table(self, name):
            q = super().table(name)
            orig = q.execute

            def execute():
                payload = q._payload
                if isinstance(payload, dict) and "sl_tp_override" in payload:
                    raise RuntimeError("column sl_tp_override does not exist")
                return orig()

            q.execute = execute
            return q

    fake = _NoOverrideSB()
    monkeypatch.setattr(db_client, "_client", fake)
    monkeypatch.setattr(repos, "get_supabase", lambda: fake)

    uid = "user-prefs-asia-nomig"
    with pytest.raises(repos.PrefSchemaError, match="006_sl_tp_override"):
        repos.replace_prefs_for_user(
            uid,
            [
                {
                    "kind": "asia_breakout",
                    "symbol": "XAU/USD",
                    "enabled": True,
                    "sl_tp_override": True,
                    "sl_points": 400,
                    "tp_points": 800,
                }
            ],
            account_id=1001,
        )

    saved = repos.replace_prefs_for_user(
        uid,
        [
            {
                "kind": "asia_breakout",
                "symbol": "EUR/USD",
                "enabled": True,
                "sl_tp_override": False,
            }
        ],
        account_id=1001,
    )
    assert saved[0]["sl_tp_override"] is False
    assert saved[0]["breakeven_half_tp"] is False
