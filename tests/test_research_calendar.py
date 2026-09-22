"""Pure calendar parse + implied-move window."""

from __future__ import annotations

from abcxauto.research_calendar import implied_move_needed, parse_calendar_report


def test_no_date_is_unknown():
    out = parse_calendar_report("upcoming earnings next week", today="2026-09-22")
    assert out["earnings"] == "unknown"
    assert out["earnings_in"] is None
    assert out["ex_div"] is None
    assert out["calendar_asof"] == ""


def test_earnings_date_near_word():
    out = parse_calendar_report("earnings 2026-12-01", today="2026-09-22")
    assert out["earnings"] == "2026-12-01"
    assert isinstance(out["earnings_in"], int)
    assert out["earnings_in"] > 2


def test_implied_move_needed():
    assert implied_move_needed(1) is True
    assert implied_move_needed(10) is False
    assert implied_move_needed(None) is False
