"""One NYSE session clock — holidays, half-days, DST, pre/RTH/post edges."""

from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from abcxauto.marketdata.market_hours import (
    as_eastern,
    get_session_info,
    is_early_close,
    is_trading_day,
    minutes_to_rth_open,
    regular_close_time,
    rth_minute_bounds,
    rth_now,
    session_of,
)
from abcxauto.opportunity_scan import rth_now as scan_rth_now
from abcxauto.park_clock import (
    et_minutes_to_rth_open,
    infer_session_before_open,
    resolve_stay_up_session,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


# Thursday 2026-08-27 is a full NYSE session.
FULL = (2026, 8, 27)
# Thanksgiving 2026-11-26 (Thursday) is closed.
HOLIDAY = (2026, 11, 26)
# Black Friday 2026-11-27 is a 13:00 ET early close.
HALF = (2026, 11, 27)
# DST: 2026-03-08 Sunday spring-forward; Monday 2026-03-09 is a session.
DST_SPRING = (2026, 3, 9)
# DST: 2026-11-01 Sunday fall-back; Monday 2026-11-02 is a session.
DST_FALL = (2026, 11, 2)


@pytest.mark.parametrize(
    ("hh", "mm", "want"),
    [
        (3, 59, "closed"),
        (4, 0, "premarket"),
        (9, 29, "premarket"),
        (9, 30, "regular"),
        (15, 59, "regular"),
        (16, 0, "postmarket"),
        (19, 59, "postmarket"),
        (20, 0, "closed"),
    ],
)
def test_full_day_pre_rth_post_edges(hh, mm, want):
    now = _et(*FULL, hh, mm)
    assert session_of(now) == want
    assert rth_now(now=now) is (want == "regular")
    assert scan_rth_now(now=now) is (want == "regular")


def test_holiday_is_closed_through_rth_hours():
    now = _et(*HOLIDAY, 10, 16)
    assert is_trading_day(now) is False
    assert session_of(now) == "closed"
    assert rth_now(now=now) is False
    assert scan_rth_now(now=now) is False
    assert resolve_stay_up_session("", now=now) == ""
    assert infer_session_before_open(now=_et(*HOLIDAY, 8, 0)) == ("", None)


def test_half_day_early_close():
    open_rth = _et(*HALF, 10, 0)
    after = _et(*HALF, 14, 0)
    assert is_trading_day(open_rth) is True
    assert is_early_close(open_rth) is True
    assert regular_close_time(open_rth).hour == 13
    assert rth_minute_bounds(open_rth) == (9 * 60 + 30, 13 * 60)
    assert session_of(_et(*HALF, 12, 59)) == "regular"
    assert session_of(_et(*HALF, 13, 0)) == "postmarket"
    assert rth_now(now=open_rth) is True
    assert rth_now(now=after) is False
    assert scan_rth_now(now=after) is False
    info = get_session_info(after)
    assert info["early_close"] is True
    assert info["session"] == "postmarket"


@pytest.mark.parametrize("day", [DST_SPRING, DST_FALL])
def test_dst_session_edges_are_local_et(day):
    assert is_trading_day(_et(*day, 12, 0)) is True
    assert session_of(_et(*day, 9, 29)) == "premarket"
    assert session_of(_et(*day, 9, 30)) == "regular"
    assert session_of(_et(*day, 16, 0)) == "postmarket"
    assert as_eastern(_et(*day, 9, 30)).hour == 9


def test_minutes_to_open_only_before_todays_bell():
    prem = _et(*FULL, 8, 30)
    rth = _et(*FULL, 10, 0)
    assert minutes_to_rth_open(now=prem) == 60.0
    assert et_minutes_to_rth_open(now=prem) == 60.0
    assert minutes_to_rth_open(now=rth) is None
    assert et_minutes_to_rth_open(now=_et(*HOLIDAY, 8, 30)) is None


def test_resolve_stay_up_uses_shared_clock():
    assert resolve_stay_up_session("", now=_et(*FULL, 10, 16)) == "regular"
    assert resolve_stay_up_session("", now=_et(*FULL, 9, 7)) == "premarket"
    assert resolve_stay_up_session("", now=_et(*FULL, 17, 0)) == ""
    assert resolve_stay_up_session("closed", now=_et(*FULL, 10, 16)) == "closed"


def test_weekend_is_closed():
    sat = datetime(2026, 8, 29, 10, 0, tzinfo=ET)
    assert is_trading_day(sat) is False
    assert session_of(sat) == "closed"
    assert rth_now(now=sat) is False
