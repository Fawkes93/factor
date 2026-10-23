"""Open P/L and deposit currency are fetched at most once per 60s."""

from factor.services import live_pnl


def test_unrealized_cached_for_60s():
    live_pnl.reset_caches()
    calls = {"n": 0}
    conn = {"account_id": 7, "user_id": "u"}

    def fetch(_conn):
        calls["n"] += 1
        return {"9": {"net": 1.25, "gross": 1.5}}

    clock = {"t": 1000.0}
    now = lambda: clock["t"]
    first = live_pnl.unrealized_for_connection(conn, fetch=fetch, now=now)
    clock["t"] += 30
    second = live_pnl.unrealized_for_connection(conn, fetch=fetch, now=now)
    assert calls["n"] == 1
    assert first["9"]["net"] == 1.25
    assert second["9"]["net"] == 1.25
    clock["t"] += 31
    live_pnl.unrealized_for_connection(conn, fetch=fetch, now=now)
    assert calls["n"] == 2


def test_unrealized_failure_is_cached():
    live_pnl.reset_caches()
    calls = {"n": 0}

    def fetch(_conn):
        calls["n"] += 1
        raise RuntimeError("broker down")

    clock = {"t": 0.0}
    conn = {"account_id": 3}
    assert live_pnl.unrealized_for_connection(conn, fetch=fetch, now=lambda: clock["t"]) == {}
    clock["t"] += 10
    assert live_pnl.unrealized_for_connection(conn, fetch=fetch, now=lambda: clock["t"]) == {}
    assert calls["n"] == 1


def test_currency_uses_stored_value_without_fetch():
    live_pnl.reset_caches()
    conn = {"account_id": 1, "deposit_currency": "eur"}

    def fetch(_conn):
        raise AssertionError("should not fetch")

    assert live_pnl.deposit_currency_for(conn, fetch=fetch, now=lambda: 0) == "EUR"


def test_currency_fetch_once_then_persists(monkeypatch):
    live_pnl.reset_caches()
    saved = {}

    def remember(uid, aid, code):
        saved["uid"] = uid
        saved["aid"] = aid
        saved["code"] = code

    monkeypatch.setattr(live_pnl.repos, "set_deposit_currency", remember)
    calls = {"n": 0}

    def fetch(_conn):
        calls["n"] += 1
        return "usd"

    conn = {"account_id": 4, "user_id": "uu"}
    clock = {"t": 50.0}
    assert live_pnl.deposit_currency_for(conn, fetch=fetch, now=lambda: clock["t"]) == "USD"
    assert calls["n"] == 1
    assert saved == {"uid": "uu", "aid": 4, "code": "USD"}
    assert conn["deposit_currency"] == "USD"
    clock["t"] += 10
    assert live_pnl.deposit_currency_for(conn, fetch=fetch, now=lambda: clock["t"]) == "USD"
    assert calls["n"] == 1
