"""Research dossier: snapshot price copy + earnings new-risk blocks."""

from __future__ import annotations

from abcxauto.research_dossier import assemble_dossiers, dossier_blocks_new_risk


def _empty_sides() -> dict:
    return {
        "scan_rows": [],
        "calendar": {},
        "news": {},
        "web": {},
        "odds": {},
        "spend": None,
    }


def test_empty_snapshot_returns_no_dossiers():
    assert assemble_dossiers({}, **_empty_sides()) == {}
    assert assemble_dossiers({"ok": False, "names": {"AAPL": {"last": 1}}}, **_empty_sides()) == {}
    assert assemble_dossiers({"ok": True, "names": {}}, **_empty_sides()) == {}


def test_scan_row_last_does_not_change_dossier_last():
    snapshot = {
        "ok": True,
        "names": {
            "AAPL": {
                "last": 190.5,
                "asof": "2026-09-22T14:00:00Z",
                "bar_date": "2026-09-22",
                "vs_spy": 0.4,
            }
        },
    }
    scan_rows = [
        {
            "symbol": "AAPL",
            "last": 999,
            "open_gap_pct": 2.5,
            "vs_open": 1.1,
            "asof": "2026-09-22T13:55:00Z",
        }
    ]
    dossiers = assemble_dossiers(
        snapshot,
        scan_rows=scan_rows,
        calendar={"AAPL": {"earnings": "2026-11-15", "earnings_in": 40, "ex_div": None, "calendar_asof": "c"}},
        news={"news_asof": "n", "headlines": [], "news": "ok"},
        web={"web": "unavailable", "web_asof": ""},
        odds={"odds": "unavailable", "odds_asof": ""},
        spend={"spend_asof": "s", "session_usd": 0.1},
    )
    assert "AAPL" in dossiers
    d = dossiers["AAPL"]
    assert d["last"] == 190.5
    assert d["last"] != 999
    assert d["gap"] == 2.5
    assert d["gap"] != 0
    assert d["price_asof"] == "2026-09-22T14:00:00Z"
    assert d["bar_date"] == "2026-09-22"
    assert d["vs_spy"] == 0.4
    assert d["vs_open"] == 1.1


def test_missing_gap_is_unavailable_not_zero():
    snapshot = {"ok": True, "names": {"MSFT": {"last": 400.0, "asof": "t"}}}
    dossiers = assemble_dossiers(snapshot, **_empty_sides())
    assert dossiers["MSFT"]["gap"] == "unavailable"
    assert dossiers["MSFT"]["gap"] != 0


def test_earnings_unknown_blocks_new_risk():
    blocked, reason = dossier_blocks_new_risk(
        {
            "last": 10,
            "earnings": "unknown",
            "earnings_in": 30,
            "gap": "unavailable",
        }
    )
    assert blocked is True
    assert reason == "earnings_unknown"

    snap = {"ok": True, "names": {"XYZ": {"last": 10, "asof": "t"}}}
    dossiers = assemble_dossiers(snap, **_empty_sides())
    assert dossiers["XYZ"]["earnings"] == "unknown"
    blocked2, reason2 = dossier_blocks_new_risk(dossiers["XYZ"])
    assert blocked2 is True
    assert reason2 == "earnings_unknown"


def test_earnings_in_one_blocks_window():
    blocked, reason = dossier_blocks_new_risk(
        {
            "last": 50,
            "earnings": "2026-09-23",
            "earnings_in": 1,
            "gap": 1.0,
        }
    )
    assert blocked is True
    assert reason == "earnings_window"


def test_complete_dossier_earnings_30_sessions_allows():
    dossier = {
        "price_asof": "2026-09-22T14:00:00Z",
        "bar_date": "2026-09-22",
        "vs_spy": 0.1,
        "last": 100.0,
        "scan_asof": "2026-09-22T13:55:00Z",
        "gap": 1.2,
        "vs_open": 0.5,
        "calendar_asof": "2026-09-22T14:01:00Z",
        "earnings": "2026-11-05",
        "earnings_in": 30,
        "ex_div": None,
        "news_asof": "2026-09-22T14:00:00Z",
        "headlines": [],
        "web_asof": "2026-09-22T14:00:00Z",
        "web": "ok",
        "odds_asof": "2026-09-22T14:00:00Z",
        "odds": {"probs": {}},
        "spend_asof": "2026-09-22",
        "session_usd": 0.4,
    }
    blocked, reason = dossier_blocks_new_risk(dossier)
    assert blocked is False
    assert reason == ""


def test_none_or_empty_dossier_is_no_dossier():
    assert dossier_blocks_new_risk(None) == (True, "no_dossier")
    assert dossier_blocks_new_risk({}) == (True, "no_dossier")
