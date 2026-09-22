"""Scan / web / odds / spend color helpers. No brief, no last leak."""

from __future__ import annotations

from abcxauto.research_color import color_unavailable, scan_gap, spend_slot


def test_scan_gap_keeps_gap_drops_last():
    rows = [{"symbol": "AMD", "last": 999, "open_gap_pct": 2.5}]
    out = scan_gap(rows, "AMD")
    assert out["gap"] == 2.5
    assert "last" not in out
    assert set(out) == {"scan_asof", "gap", "vs_open"}


def test_scan_gap_missing_row_unavailable():
    out = scan_gap([{"symbol": "MU", "open_gap_pct": -3.3}], "AMD")
    assert out["gap"] == "unavailable"
    assert out["vs_open"] == "unavailable"
    assert out["scan_asof"] == ""
    assert "last" not in out


def test_scan_gap_zero_is_real():
    out = scan_gap([{"symbol": "FLAT", "open_gap_pct": 0, "vs_open": 0}], "FLAT")
    assert out["gap"] == 0
    assert out["vs_open"] == 0


def test_color_unavailable_shape():
    out = color_unavailable()
    assert out == {
        "web": "unavailable",
        "web_asof": "",
        "odds": "unavailable",
        "odds_asof": "",
        "citations": [],
    }


def test_spend_slot_none_unavailable():
    out = spend_slot(None)
    assert out == {"spend_asof": "", "session_usd": "unavailable"}


def test_spend_slot_with_asof_and_usd():
    out = spend_slot({"asof": "2026-09-22T14:00:00+00:00", "usd": 0.03})
    assert out == {
        "spend_asof": "2026-09-22T14:00:00+00:00",
        "session_usd": 0.03,
    }
