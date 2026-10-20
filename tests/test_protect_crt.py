"""CRT partial, original stop, max-hold, and Friday flatten. No network."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from factor import config
from factor.broker.ctrader_client import CTraderClient
from factor.services.protect_crt import (
    crt_detail_from_metrics,
    crt_flatten_due,
    crt_mark,
    crt_partial_volume,
    crt_spot_block_reason,
    crt_stale_fill_reason,
    process_open_order_for_crt,
    tp1_reached,
)

TZ = ZoneInfo("Europe/Chisinau")


@pytest.fixture(autouse=True)
def _crt_config(monkeypatch):
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN", "FRI 23:45 Europe/Chisinau")
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", True)
    monkeypatch.setattr(config, "CRT_MAX_ENTRY_SLIP_PTS", 0.0)


def _local(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


def _detail(**over):
    blob = crt_detail_from_metrics(
        {
            "tp1_price": 4010.0,
            "tp1_frac": 0.5,
            "be_after_tp1": False,
            "max_hold_hours": 96,
        },
        triggered_at="2026-10-19T07:00:00Z",
    )
    blob.update(over)
    return blob


class FakeBroker:
    def __init__(self, bid=4010.0, ask=4011.0):
        self.bid = bid
        self.ask = ask
        self.closes = []
        self.amends = []

    def get_spot(self, sym):
        return {"bid": self.bid, "ask": self.ask}

    def close_position(self, *, position_id, volume):
        self.closes.append((int(position_id), int(volume)))
        return {"ok": True}

    def amend_position_sl_tp(self, *, position_id, stop_loss, take_profit):
        self.amends.append(
            {
                "position_id": int(position_id),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        return {"ok": True}


def _order(*, side="BUY", be=False, frac=0.5, tp1=4010.0, tp=4060.0, sl=3990.0, entry=4000.0):
    return {
        "id": "ord-crt",
        "kind": "crt",
        "symbol": "XAU/USD",
        "side": side,
        "broker_position_id": "77",
        "entry_price": entry,
        "sl_price": sl,
        "tp_price": tp,
        "created_at": "2026-10-19T07:00:00Z",
        "status": "open",
        "detail": {
            "breakeven": {
                "enabled": True,
                "applied": False,
                "fraction": 0.5,
                "point_size": 0.01,
            },
            "protect": _detail(
                tp1_price=tp1,
                tp1_frac=frac,
                be_after_tp1=be,
                entry_at="2026-10-19T07:00:00Z",
            ),
        },
    }


def _pos(**over):
    base = {
        "position_id": 77,
        "side": "BUY",
        "entry_price": 4000.0,
        "volume": 1000,
        "volume_step": 100,
        "min_volume": 100,
        "stop_loss": 3990.0,
        "take_profit": 4060.0,
        "broker_symbol": "XAUUSD",
    }
    base.update(over)
    return base


def test_detail_from_metrics_uses_config_not_webhook_clock():
    d = crt_detail_from_metrics(
        {
            "tp1_price": 4010,
            "tp1_frac": 0.5,
            "be_after_tp1": False,
            "max_hold_until": "2026-10-23T16:00:00Z",
            "max_hold_hours": 96,
            "weekly_flatten": "MON 00:01 UTC",
        },
        triggered_at="2026-10-19T07:00:00Z",
    )
    assert d["enabled"] is True
    assert d["kind"] == "crt"
    assert d["tp1_price"] == 4010.0
    assert d["tp1_frac"] == 0.5
    assert d["be_after_tp1"] is False
    assert d["max_hold_until"] == "2026-10-23T16:00:00Z"
    assert d["max_hold_hours"] == 96.0
    assert d["flatten_weekday"] == 4
    assert d["flatten_time"] == "23:45"
    assert d["flatten_tz"] == "Europe/Chisinau"
    assert d["partial_done"] is False
    assert d["flattened"] is False
    assert d["entry_at"] == "2026-10-19T07:00:00Z"


def test_string_false_be_after_tp1_stays_off():
    d = crt_detail_from_metrics({"be_after_tp1": "false", "tp1_price": 1.1})
    assert d["be_after_tp1"] is False


def test_tp1_reached_uses_exit_side_mark():
    # BUY watches the bid. A high ask must not count.
    buy_mark = crt_mark("BUY", bid=4008.0, ask=4012.0)
    assert buy_mark == 4008.0
    assert tp1_reached("BUY", buy_mark, 4010.0) is False
    assert tp1_reached("BUY", crt_mark("BUY", bid=4010.0, ask=4011.0), 4010.0) is True
    # SELL watches the ask. A low bid must not count.
    sell_mark = crt_mark("SELL", bid=3988.0, ask=3992.0)
    assert sell_mark == 3992.0
    assert tp1_reached("SELL", sell_mark, 3990.0) is False
    assert tp1_reached("SELL", crt_mark("SELL", bid=3989.0, ask=3990.0), 3990.0) is True


def test_partial_volume_rounds_down_and_skips_min_step():
    assert crt_partial_volume(1000, 0.5, 100, 100) == (500, "partial")
    # 1100 * 0.5 = 550 → 500 on a 100 step. Remainder 600 stays tradable.
    assert crt_partial_volume(1100, 0.5, 100, 100) == (500, "partial")
    # Not a whole step.
    assert crt_partial_volume(150, 0.5, 100, 1) == (None, "skip")
    # Half is a step but under the broker minimum.
    assert crt_partial_volume(300, 0.5, 10, 200) == (None, "skip")
    # Remainder would be under the minimum, so the partial takes the rest.
    assert crt_partial_volume(180, 0.8, 100, 100) == (180, "full")


def test_buy_partial_once_leaves_sl_and_tp(monkeypatch):
    updates = []
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: updates.append(fields) or {"id": oid},
    )
    order = _order()
    pos = _pos()
    broker = FakeBroker()
    mark = crt_mark("BUY", bid=4010.0, ask=4011.0)
    now = _local(2026, 10, 20, 12, 0)
    first = process_open_order_for_crt(order, broker, pos, mark, 0.01, now)
    assert first["action"] == "partial"
    assert first["volume"] == 500
    assert broker.closes == [(77, 500)]
    assert broker.amends == []
    assert order["detail"]["protect"]["partial_done"] is True
    assert order["detail"]["protect"]["partial_price"] == 4010.0
    assert order["sl_price"] == 3990.0
    assert all("sl_price" not in u for u in updates)
    assert updates[0]["detail"]["protect"]["partial_done"] is True

    second = process_open_order_for_crt(order, broker, pos, mark, 0.01, now)
    assert second["action"] == "skip"
    assert second["reason"] == "partial_done"
    assert broker.closes == [(77, 500)]
    assert broker.amends == []


def test_sell_partial_once_rounds_down(monkeypatch):
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: {"id": oid, **fields},
    )
    order = _order(side="SELL", tp1=3990.0, tp=3940.0, sl=4010.0, entry=4000.0)
    pos = _pos(
        side="SELL",
        volume=1100,
        stop_loss=4010.0,
        take_profit=3940.0,
        entry_price=4000.0,
    )
    broker = FakeBroker(bid=3989.0, ask=3990.0)
    mark = crt_mark("SELL", bid=broker.bid, ask=broker.ask)
    result = process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 0)
    )
    assert result["action"] == "partial"
    assert result["volume"] == 500
    assert broker.amends == []
    again = process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 1)
    )
    assert again["reason"] == "partial_done"
    assert broker.closes == [(77, 500)]


def test_be_after_tp1_moves_sl_to_fill_and_keeps_tp(monkeypatch):
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: {"id": oid, **fields},
    )
    order = _order(be=True)
    # Fill slipped away from the alert price. SL goes to the fill, not the alert.
    pos = _pos(entry_price=4001.25, take_profit=4060.0, stop_loss=3990.0)
    broker = FakeBroker()
    mark = crt_mark("BUY", bid=4010.0, ask=4011.0)
    result = process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 0)
    )
    assert result["action"] == "partial"
    assert broker.closes == [(77, 500)]
    assert broker.amends == [
        {"position_id": 77, "stop_loss": 4001.25, "take_profit": 4060.0}
    ]
    assert order["detail"]["protect"]["be_sl"] == 4001.25
    # Runner target is still the original tp_price.
    assert broker.amends[0]["take_profit"] == order["tp_price"]


def test_sell_be_after_tp1_moves_sl_to_fill(monkeypatch):
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: {"id": oid, **fields},
    )
    order = _order(side="SELL", be=True, tp1=3990.0, tp=3940.0, sl=4010.0)
    pos = _pos(
        side="SELL",
        entry_price=3998.4,
        stop_loss=4010.0,
        take_profit=3940.0,
    )
    broker = FakeBroker(bid=3989.0, ask=3990.0)
    mark = crt_mark("SELL", bid=broker.bid, ask=broker.ask)
    process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 0)
    )
    assert broker.amends == [
        {"position_id": 77, "stop_loss": 3998.4, "take_profit": 3940.0}
    ]


def test_partial_skipped_when_half_is_below_min_volume(monkeypatch):
    updates = []
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: updates.append(fields) or {"id": oid},
    )
    order = _order()
    pos = _pos(volume=100, volume_step=100, min_volume=100)
    broker = FakeBroker()
    mark = crt_mark("BUY", bid=4010.0, ask=4011.0)
    result = process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 0)
    )
    assert result["reason"] == "partial_skipped:min_volume"
    assert broker.closes == []
    assert broker.amends == []
    assert order["detail"]["protect"]["partial_skipped"] == "min_volume"
    assert order["detail"]["protect"]["partial_done"] is False
    again = process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 1)
    )
    assert again["reason"] == "partial_skipped:min_volume"
    assert broker.closes == []
    assert len(updates) == 1


def test_partial_that_consumes_the_position_marks_closed(monkeypatch):
    updates = []
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: updates.append(fields) or {"id": oid},
    )
    order = _order(frac=0.8)
    pos = _pos(volume=180, volume_step=100, min_volume=100)
    broker = FakeBroker()
    mark = crt_mark("BUY", bid=4010.0, ask=4011.0)
    result = process_open_order_for_crt(
        order, broker, pos, mark, 0.01, _local(2026, 10, 20, 12, 0)
    )
    assert result["action"] == "closed"
    assert result["reason"] == "crt_partial_full"
    assert broker.closes == [(77, 180)]
    assert broker.amends == []
    assert updates[0]["status"] == "closed"
    assert order["detail"]["protect"]["partial_done"] is True


def test_before_tp1_does_not_move_sl_even_if_generic_be_would(monkeypatch):
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: {"id": oid},
    )
    # Halfway to the runner (4020) would be generic BE. TP1 is 4010.
    order = _order(be=False)
    order["detail"]["breakeven"]["enabled"] = True
    broker = FakeBroker(bid=4005.0, ask=4005.2)
    mark = crt_mark("BUY", bid=4005.0, ask=4005.2)
    result = process_open_order_for_crt(
        order, broker, _pos(), mark, 0.01, _local(2026, 10, 20, 12, 0)
    )
    assert result["reason"] == "tp1_not_yet"
    assert broker.closes == []
    assert broker.amends == []


def test_stale_fill_reasons_do_not_shift_levels():
    # Mark slipped, but levels stay the absolute webhook prices.
    assert (
        crt_stale_fill_reason(
            side="BUY",
            mark=4002.0,
            sl_price=3990.0,
            tp1_price=4010.0,
            entry_price=4000.0,
            point_size=0.01,
            max_slip_pts=None,
        )
        is None
    )
    assert (
        crt_stale_fill_reason(
            side="BUY",
            mark=3989.0,
            sl_price=3990.0,
            tp1_price=4010.0,
            entry_price=4000.0,
            point_size=0.01,
        )
        == "crt_stale_fill:beyond_sl"
    )
    assert (
        crt_stale_fill_reason(
            side="BUY",
            mark=4010.0,
            sl_price=3990.0,
            tp1_price=4010.0,
            entry_price=4000.0,
            point_size=0.01,
        )
        == "crt_stale_fill:beyond_tp1"
    )
    assert (
        crt_stale_fill_reason(
            side="SELL",
            mark=4010.0,
            sl_price=4010.0,
            tp1_price=3990.0,
            entry_price=4000.0,
            point_size=0.01,
        )
        == "crt_stale_fill:beyond_sl"
    )
    assert (
        crt_stale_fill_reason(
            side="SELL",
            mark=3990.0,
            sl_price=4010.0,
            tp1_price=3990.0,
            entry_price=4000.0,
            point_size=0.01,
        )
        == "crt_stale_fill:beyond_tp1"
    )
    # Cap off: a 500-point slip is still allowed when price is between SL and TP1.
    assert (
        crt_stale_fill_reason(
            side="BUY",
            mark=4005.0,
            sl_price=3990.0,
            tp1_price=4010.0,
            entry_price=4000.0,
            point_size=0.01,
            max_slip_pts=0,
        )
        is None
    )
    assert (
        crt_stale_fill_reason(
            side="BUY",
            mark=4005.0,
            sl_price=3990.0,
            tp1_price=4010.0,
            entry_price=4000.0,
            point_size=0.01,
            max_slip_pts=100,
        )
        == "crt_stale_fill:slip"
    )


class _SpotBroker:
    def __init__(self, bid, ask):
        self._dry_run = False
        self.bid = bid
        self.ask = ask

    def get_spot(self, sym):
        return {"bid": self.bid, "ask": self.ask}


def _eur_plan(**over):
    fields = {
        "broker_symbol": "EURUSD",
        "side": "BUY",
        "entry_price": 1.12308,
        "sl_price": 1.12258,
        "point_size": 0.00001,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def test_eur_protocol_spot_between_sl_and_tp1_does_not_block():
    """112310 / 112312 are ProtoOASpotEvent ints for 1.12310 / 1.12312."""
    bid = CTraderClient._normalize_spot_quote(112310)
    ask = CTraderClient._normalize_spot_quote(112312)
    broker = _SpotBroker(bid, ask)
    buy = {
        "entry_price": 1.12308,
        "sl_price": 1.12258,
        "tp1_price": 1.12358,
    }
    assert crt_spot_block_reason(broker, _eur_plan(), buy) is None
    # SELL mirror: stop above entry, tp1 below, mark is the ask.
    sell = {
        "entry_price": 1.12308,
        "sl_price": 1.12358,
        "tp1_price": 1.12258,
    }
    assert (
        crt_spot_block_reason(
            broker, _eur_plan(side="SELL", sl_price=1.12358), sell
        )
        is None
    )


def test_mark_ten_percent_from_entry_is_bad_spot():
    metrics = {
        "entry_price": 1.12308,
        "sl_price": 1.12258,
        "tp1_price": 1.12358,
    }
    far = 1.12308 * 1.10
    assert (
        crt_spot_block_reason(_SpotBroker(far, far), _eur_plan(), metrics)
        == "crt_stale_fill:bad_spot"
    )
    # The live failure: an unscaled EUR int is past TP1 only because of scale.
    assert (
        crt_spot_block_reason(_SpotBroker(112310, 112312), _eur_plan(), metrics)
        == "crt_stale_fill:bad_spot"
    )


def _hold_detail(hours=None, until=None):
    return {
        "flatten_weekday": 4,
        "flatten_time": "23:45",
        "flatten_tz": "Europe/Chisinau",
        "max_hold_hours": hours,
        "max_hold_until": until,
    }


def test_max_hold_xau_96h_and_eur_48h_include_the_weekend(monkeypatch):
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", False)
    # Friday 16:00 UTC + 96h = Tuesday 16:00 UTC (Saturday and Sunday included).
    entry = datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)
    xau_end = entry + timedelta(hours=96)
    assert xau_end == datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)
    assert xau_end.strftime("%A") == "Tuesday"
    detail = _hold_detail(hours=96)
    assert crt_flatten_due(xau_end - timedelta(seconds=1), detail, entry) is None
    reason = crt_flatten_due(xau_end, detail, entry)
    assert reason == "max_hold:2026-10-27T16:00:00Z"

    # Friday 16:00 UTC + 48h = Sunday 16:00 UTC, not the next business day.
    eur_end = entry + timedelta(hours=48)
    assert eur_end == datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)
    assert eur_end.strftime("%A") == "Sunday"
    eur = _hold_detail(hours=48)
    assert crt_flatten_due(eur_end - timedelta(seconds=1), eur, entry) is None
    assert crt_flatten_due(eur_end, eur, entry) == "max_hold:2026-10-25T16:00:00Z"

    # Missing max_hold_until falls back to the hours. A timestamp wins over hours.
    assert crt_flatten_due(eur_end, _hold_detail(hours=48, until=None), entry).startswith(
        "max_hold:"
    )
    stamped = _hold_detail(hours=96, until="2026-10-25T16:00:00Z")
    assert crt_flatten_due(eur_end, stamped, entry) == "max_hold:2026-10-25T16:00:00Z"
    assert crt_flatten_due(eur_end - timedelta(seconds=1), stamped, entry) is None


def test_weekly_flatten_friday_2345_not_thursday(monkeypatch):
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", True)
    # Far max-hold so only the weekly clock can fire.
    detail = _hold_detail(hours=None, until="2026-12-01T00:00:00Z")
    entry = _local(2026, 10, 19, 10, 0)  # Monday
    assert crt_flatten_due(_local(2026, 10, 22, 23, 50), detail, entry) is None
    assert crt_flatten_due(_local(2026, 10, 23, 23, 44), detail, entry) is None
    reason = crt_flatten_due(_local(2026, 10, 23, 23, 45), detail, entry)
    assert reason == "weekly:FRI 23:45 Europe/Chisinau"
    assert (
        crt_flatten_due(_local(2026, 10, 23, 23, 45, 10), detail, entry)
        == "weekly:FRI 23:45 Europe/Chisinau"
    )


def test_weekly_flatten_dst_weeks_use_local_2345():
    """23:45 wall time, both sides of the March and October changes.

    2026-03-29 and 2026-10-25 are the EU/Chișinău transitions. Friday
    before each is still the old offset; Friday after is the new one.
    A fixed UTC offset would miss 23:45 on one of those Fridays.
    """
    detail = _hold_detail(hours=None, until="2026-12-01T00:00:00Z")
    summer = _local(2026, 10, 23, 23, 45)  # still EEST, before Oct 25
    winter = _local(2026, 10, 30, 23, 45)  # EET, after the change
    march = _local(2026, 3, 27, 23, 45)  # EET, before March 29
    april = _local(2026, 4, 3, 23, 45)  # EEST, after the change
    assert summer.utcoffset() == timedelta(hours=3)
    assert winter.utcoffset() == timedelta(hours=2)
    assert march.utcoffset() == timedelta(hours=2)
    assert april.utcoffset() == timedelta(hours=3)

    cases = (
        (_local(2026, 10, 19, 10, 0), _local(2026, 10, 23, 23, 44), _local(2026, 10, 23, 23, 45)),
        (_local(2026, 10, 26, 10, 0), _local(2026, 10, 30, 23, 44), _local(2026, 10, 30, 23, 45)),
        (_local(2026, 3, 23, 10, 0), _local(2026, 3, 27, 23, 44), _local(2026, 3, 27, 23, 45)),
        (_local(2026, 3, 30, 10, 0), _local(2026, 4, 3, 23, 44), _local(2026, 4, 3, 23, 45)),
    )
    for entry, before, at in cases:
        assert crt_flatten_due(before, detail, entry) is None
        assert crt_flatten_due(at, detail, entry) == "weekly:FRI 23:45 Europe/Chisinau"


def test_opened_at_or_after_friday_waits_until_next_friday():
    """A fill at 23:45 or later is not flattened that night.

    It is flattened the next Friday, including when that Friday is the
    other side of the October DST change. A position whose Friday already
    passed still flattens on a later poll (Monday catch-up).
    """
    detail = _hold_detail(hours=None, until="2026-12-01T00:00:00Z")
    entry = _local(2026, 10, 23, 23, 46)
    assert crt_flatten_due(_local(2026, 10, 23, 23, 50), detail, entry) is None
    assert crt_flatten_due(_local(2026, 10, 24, 12, 0), detail, entry) is None
    assert crt_flatten_due(_local(2026, 10, 30, 23, 44), detail, entry) is None
    assert (
        crt_flatten_due(_local(2026, 10, 30, 23, 45), detail, entry)
        == "weekly:FRI 23:45 Europe/Chisinau"
    )
    # Exactly 23:45 is not "before" the clock, so it also waits.
    exact = _local(2026, 10, 23, 23, 45)
    assert crt_flatten_due(exact, detail, exact) is None

    # Thursday entry, missed Friday, Monday poll still closes.
    early = _local(2026, 10, 22, 10, 0)
    assert (
        crt_flatten_due(_local(2026, 10, 26, 8, 0), detail, early)
        == "weekly:FRI 23:45 Europe/Chisinau"
    )


def test_weekly_switch_off_still_honours_max_hold(monkeypatch):
    monkeypatch.setattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", False)
    entry = _local(2026, 10, 19, 10, 0)
    # Clock would be Friday, but the switch is off and max hold is still later.
    held = _hold_detail(hours=None, until="2026-12-01T00:00:00Z")
    assert crt_flatten_due(_local(2026, 10, 23, 23, 45), held, entry) is None
    # 48h from Monday 10:00 local is Wednesday 10:00, before Friday.
    detail = _hold_detail(hours=48, until=None)
    assert crt_flatten_due(_local(2026, 10, 21, 9, 59), detail, entry) is None
    assert crt_flatten_due(_local(2026, 10, 21, 10, 0), detail, entry).startswith(
        "max_hold:"
    )


def test_flatten_closes_full_volume_before_partial(monkeypatch):
    updates = []
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: updates.append(fields) or {"id": oid},
    )
    order = _order()
    order["detail"]["protect"]["entry_at"] = "2026-10-19T07:00:00Z"
    order["detail"]["protect"]["max_hold_until"] = "2026-12-01T00:00:00Z"
    broker = FakeBroker()
    mark = crt_mark("BUY", bid=4010.0, ask=4011.0)
    result = process_open_order_for_crt(
        order,
        broker,
        _pos(volume=1000),
        mark,
        0.01,
        _local(2026, 10, 23, 23, 45),
    )
    assert result["action"] == "flattened"
    assert result["reason"] == "weekly:FRI 23:45 Europe/Chisinau"
    assert result["volume"] == 1000
    assert broker.closes == [(77, 1000)]
    assert broker.amends == []
    assert updates[0]["status"] == "closed"
    assert order["detail"]["protect"]["partial_done"] is False


def test_poll_routes_crt_by_exit_side_quote(monkeypatch):
    """The breakeven poll sends kind=crt here, using bid for BUY and ask for SELL."""
    from factor.services.breakeven import process_open_order_for_be

    updates = []
    monkeypatch.setattr(
        "factor.db.repos.update_order",
        lambda oid, **fields: updates.append({"id": oid, **fields}) or {"id": oid},
    )
    def _still_open(order):
        prot = order["detail"]["protect"]
        prot["entry_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        prot["max_hold_until"] = "2099-01-01T00:00:00Z"
        prot["max_hold_hours"] = None
        return order

    broker = FakeBroker(bid=4010.0, ask=4011.0)
    order = _still_open(_order())
    result = process_open_order_for_be(order, broker=broker, positions=[_pos()])
    assert result["action"] == "partial"
    assert broker.closes == [(77, 500)]
    assert broker.amends == []

    # Wrong side of the quote must not take the partial.
    quiet = FakeBroker(bid=4008.0, ask=4012.0)
    fresh = _still_open(_order())
    fresh["id"] = "ord-crt-2"
    missed = process_open_order_for_be(fresh, broker=quiet, positions=[_pos()])
    assert missed["reason"] == "tp1_not_yet"
    assert quiet.closes == []

    sell_broker = FakeBroker(bid=3988.0, ask=3992.0)
    sell = _still_open(_order(side="SELL", tp1=3990.0, tp=3940.0, sl=4010.0))
    sell["id"] = "ord-crt-sell"
    sell_pos = _pos(side="SELL", stop_loss=4010.0, take_profit=3940.0)
    missed_sell = process_open_order_for_be(
        sell, broker=sell_broker, positions=[sell_pos]
    )
    assert missed_sell["reason"] == "tp1_not_yet"
    assert sell_broker.closes == []
