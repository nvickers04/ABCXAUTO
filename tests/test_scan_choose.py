"""Bare scan() is a catalog. One asked screen is one public page."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("stub_agent_loop_import")

from abcxauto.brain import BrainTurn, _run_tool
from abcxauto.world_state import WorldState


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _gap_hits():
    return [
        {"symbol": "GAP1", "open_gap_pct": 42.0, "last": 28.0, "scan_code": "HIGH_OPEN_GAP"},
        {"symbol": "GAP2", "open_gap_pct": 8.0, "last": 55.0, "scan_code": "HIGH_OPEN_GAP"},
    ]


def _opt_hits():
    return [
        {
            "symbol": "INIO",
            "distance": 135.5,
            "last": 22.0,
            "scan_code": "HOT_BY_OPT_VOLUME",
        },
        {
            "symbol": "REAL",
            "distance": 20.0,
            "open_gap_pct": 5.0,
            "last": 81.0,
            "scan_code": "HOT_BY_OPT_VOLUME",
        },
    ]


@pytest.mark.asyncio
async def test_bare_scan_returns_catalog_without_fetching(monkeypatch):
    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        raise AssertionError("bare scan() must not call criteria_scan")

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    data = json.loads(
        await _run_tool(
            "scan",
            {},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert n["calls"] == 0
    assert data.get("fetched") is False
    assert "arena" in str(data.get("need") or "")
    screens = data.get("screens") or []
    assert isinstance(screens, list) and screens
    for row in screens:
        assert row.get("arena")
        assert "scan_code" in row
        assert "metric" in row


@pytest.mark.asyncio
async def test_second_screen_public_hits_are_only_that_page(monkeypatch):
    n = {"calls": 0}

    async def _fake_scan(**kw):
        n["calls"] += 1
        arena = str(kw.get("arena") or "").strip().lower()
        code = str(kw.get("scan_code") or "").strip().upper()
        if arena == "high_open_gap" or code == "HIGH_OPEN_GAP":
            hits = _gap_hits()
            return {
                "ok": True,
                "source": "ibkr",
                "arena": "high_open_gap",
                "scan_code": "HIGH_OPEN_GAP",
                "symbols": [r["symbol"] for r in hits],
                "hits": hits,
                "quoted": len(hits),
            }
        hits = _opt_hits()
        return {
            "ok": True,
            "source": "ibkr",
            "arena": "hot_by_opt_volume",
            "scan_code": "HOT_BY_OPT_VOLUME",
            "symbols": [r["symbol"] for r in hits],
            "hits": hits,
            "quoted": len(hits),
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    first = json.loads(
        await _run_tool(
            "scan",
            {"arena": "high_open_gap"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    second = json.loads(
        await _run_tool(
            "scan",
            {"arena": "hot_by_opt_volume"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert n["calls"] == 2
    assert first.get("reused") is not True
    assert set(first.get("symbols") or []) == {"GAP1", "GAP2"}
    assert set(second.get("symbols") or []) == {"INIO", "REAL"}
    assert "GAP1" not in (second.get("symbols") or [])
    assert "GAP2" not in (second.get("symbols") or [])
    second_hits = second.get("hits") or []
    assert {str(r.get("symbol")) for r in second_hits if isinstance(r, dict)} == {
        "INIO",
        "REAL",
    }
    trophy = second.get("deepest_open_gap_pct")
    assert trophy != pytest.approx(135.5)
    assert trophy != pytest.approx(42.0)
    assert trophy == pytest.approx(5.0)
    assert second.get("deepest_symbol") == "REAL"


@pytest.mark.asyncio
async def test_repeat_same_arena_this_look_is_reuse_stub(monkeypatch):
    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        hits = _gap_hits()
        return {
            "ok": True,
            "source": "ibkr",
            "arena": "high_open_gap",
            "scan_code": "HIGH_OPEN_GAP",
            "symbols": [r["symbol"] for r in hits],
            "hits": hits,
            "quoted": len(hits),
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    first = json.loads(
        await _run_tool(
            "scan",
            {"arena": "high_open_gap"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    again = json.loads(
        await _run_tool(
            "scan",
            {"arena": "high_open_gap"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert n["calls"] == 1
    assert first.get("reused") is not True
    assert first.get("hits")
    assert again["reused"] is True
    assert again.get("repeat_of_this_think") is True
    assert not again.get("hits")
