"""Screens are live IBKR pages. No persisted arena watchlist."""

from __future__ import annotations

import pytest

from abcxauto.universe import ARENA_CATALOG, resolve_screen


def test_deleted_screens_are_not_in_the_catalog():
    for dead in (
        "index_etfs",
        "commodities",
        "technology",
        "healthcare",
        "energy",
        "financials",
    ):
        assert dead not in ARENA_CATALOG
        out = resolve_screen(arena=dead)
        assert out["ok"] is False
        assert "unknown screen" in str(out.get("error") or "")
        assert "valid=" in str(out.get("error") or "")


def test_catalog_is_not_frozen_to_a_self_tune_enum():
    """Deliberately dropped: catalog-equals-self_tune-enum. No watchlist enum."""
    assert "mega_cap" in ARENA_CATALOG
    assert "most_active" in ARENA_CATALOG
    assert ARENA_CATALOG["mega_cap"]["ibkr"]["scanCode"]


@pytest.mark.asyncio
async def test_scan_hits_carry_screen_label(monkeypatch):
    from abcxauto.opportunity_scan import criteria_scan

    async def fake_pull(_connector=None, **_k):
        return {
            "ok": True,
            "arena_id": "mega_cap",
            "scan_code": "HOT_BY_VOLUME",
            "source": "ibkr",
            "empty": False,
            "symbols": ["AAPL", "MSFT"],
            "rows": [
                {"symbol": "AAPL", "rank": 0, "distance": "1.2"},
                {"symbol": "MSFT", "rank": 1, "distance": "0.8"},
            ],
            "applied": {},
            "ibkr_rows": 2,
            "kept": 2,
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)
    out = await criteria_scan(arena="mega_cap", connector=object())
    assert out["ok"] is True
    assert out.get("screen") == "mega_cap"
    assert out.get("arena") == "mega_cap"
    assert (out.get("criteria") or {}).get("arena") == "mega_cap"
    assert out["hits"]
    assert out["provenance"]["screen"] == "mega_cap"
    for row in out["hits"]:
        assert row.get("screen") == "mega_cap"
        assert row.get("source") == "ibkr"
        assert "skip_class" in row
        assert "bid" not in row


@pytest.mark.asyncio
async def test_ranked_scan_rows_are_not_send_geometry(monkeypatch):
    from abcxauto.look_snapshot import REASON_CODE, begin_look, check_ticket_numbers
    from abcxauto.opportunity_scan import criteria_scan

    async def fake_pull(_connector=None, **_k):
        return {
            "ok": True,
            "arena_id": "top_gainers",
            "scan_code": "TOP_PERC_GAIN",
            "source": "ibkr",
            "symbols": ["AAPL"],
            "rows": [{"symbol": "AAPL", "rank": 0, "distance": "1.2", "last": 500.12}],
            "applied": {},
            "ibkr_rows": 1,
            "kept": 1,
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)
    out = await criteria_scan(arena="top_gainers", connector=object())
    snap: dict = {}
    begin_look(snap)
    snap["scan_hits"] = {
        "source": "ibkr",
        "arena": "top_gainers",
        "rows": out["hits"],
    }
    ok, code, _msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "AAPL", "price_hint": 500.12, "stop_price": 495.0},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
