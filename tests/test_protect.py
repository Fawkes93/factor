"""Pure-logic tests for Asia breakout trail + flatten."""

from datetime import datetime, timezone

from factor.services.protect import (
    flatten_due,
    next_trail_sl,
    protect_detail_from_metrics,
    should_arm_trail,
    trail_never_loosens,
    trail_stop_price,
)


def test_protect_detail_from_metrics():
    d = protect_detail_from_metrics(
        {
            "trail_arm_pts": 400,
            "trail_pts": 500,
            "flatten_hour": 18,
            "max_hold_hours": 10,
        },
        triggered_at="2026-04-01T07:20:00Z",
    )
    assert d["enabled"] is True
    assert d["trail_arm_pts"] == 400
    assert d["entry_at"] == "2026-04-01T07:20:00Z"


def test_trail_stop_and_arm():
    assert trail_stop_price(side="BUY", best=3314.0, trail_pts=250, point_size=0.01) == 3311.5
    assert trail_stop_price(side="SELL", best=3300.0, trail_pts=250, point_size=0.01) == 3302.5
    assert should_arm_trail(
        side="BUY", entry=3310.0, mark=3314.0, trail_arm_pts=400, point_size=0.01
    )
    assert not should_arm_trail(
        side="BUY", entry=3310.0, mark=3313.0, trail_arm_pts=400, point_size=0.01
    )


def test_trail_never_loosens():
    assert trail_never_loosens(side="BUY", new_sl=3311.5, current_sl=3300.0) == 3311.5
    assert trail_never_loosens(side="BUY", new_sl=3299.0, current_sl=3300.0) == 3300.0
    assert trail_never_loosens(side="SELL", new_sl=3302.5, current_sl=3320.0) == 3302.5
    assert trail_never_loosens(side="SELL", new_sl=3321.0, current_sl=3320.0) == 3320.0


def test_next_trail_sl_buy_arms_and_amends():
    amend, sl, best, armed, reason = next_trail_sl(
        side="BUY",
        entry=3310.0,
        mark=3314.0,
        point_size=0.01,
        trail_arm_pts=400,
        trail_pts=250,
        current_sl=3300.0,
        best_price=None,
        already_armed=False,
    )
    assert armed is True
    assert amend is True
    assert sl == 3311.5
    assert best == 3314.0
    assert reason == "trail_amend"


def test_flatten_hour_23_uses_entry_date_even_after_midnight():
    """23:00 Bucharest on the entry date, including a late check after midnight.

    2026-10-06 is still EEST (UTC+3). 23:00 local is 20:00 UTC.
    00:30 the next local morning is 21:30 UTC the same UTC date.
    """
    entry = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)  # 08:00 Bucharest
    at_23 = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    assert flatten_due(
        at_23,
        entry_at=entry,
        flatten_hour=23,
        tz_name="Europe/Bucharest",
        max_hold_hours=0,
    )
    before = datetime(2026, 10, 6, 19, 59, tzinfo=timezone.utc)
    assert (
        flatten_due(
            before,
            entry_at=entry,
            flatten_hour=23,
            tz_name="Europe/Bucharest",
            max_hold_hours=0,
        )
        is None
    )
    after_midnight = datetime(2026, 10, 6, 21, 30, tzinfo=timezone.utc)
    reason = flatten_due(
        after_midnight,
        entry_at=entry,
        flatten_hour=23,
        tz_name="Europe/Bucharest",
        max_hold_hours=0,
    )
    assert reason and reason.startswith("flatten_hour:23")


def test_flatten_hour_zero_is_off():
    entry = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)
    assert (
        flatten_due(
            datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc),
            entry_at=entry,
            flatten_hour=0,
            tz_name="Europe/Chisinau",
            max_hold_hours=0,
        )
        is None
    )


def test_dawn_protect_keeps_explicit_zero_flatten():
    d = protect_detail_from_metrics(
        {"flatten_hour": 0, "flatten_tz": "Europe/Chisinau"},
        kind="dawn_cross",
        triggered_at="2026-10-06T01:00:00Z",
    )
    assert d["kind"] == "dawn_cross"
    assert d["flatten_hour"] == 0
    assert d["max_hold_hours"] == 0.0
    assert d["flatten_tz"] == "Europe/Chisinau"


def test_flatten_hour_uses_bucharest_dst():
    # 18:00 EEST on 2026-04-01 = 15:00 UTC
    now = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    assert flatten_due(
        now,
        entry_at=datetime(2026, 4, 1, 7, 0, tzinfo=timezone.utc),
        flatten_hour=18,
        tz_name="Europe/Bucharest",
        max_hold_hours=10,
    )
    before = datetime(2026, 4, 1, 14, 59, tzinfo=timezone.utc)
    assert (
        flatten_due(
            before,
            entry_at=datetime(2026, 4, 1, 7, 0, tzinfo=timezone.utc),
            flatten_hour=18,
            tz_name="Europe/Bucharest",
            max_hold_hours=12,
        )
        is None
    )
    # Winter: 18:00 EET = 16:00 UTC
    winter_flat = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
    assert flatten_due(
        winter_flat,
        entry_at=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
        flatten_hour=18,
        tz_name="Europe/Bucharest",
        max_hold_hours=12,
    )
