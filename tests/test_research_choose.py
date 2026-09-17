"""Bare news/odds/research_brief are a choice. Do not dump the tape."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("stub_agent_loop_import")

from abcxauto.brain import BrainTurn, _run_tool
from abcxauto.desk_mode import note_research_tool
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


async def _tool(name, args=None, *, world=None, snap=None, turn=None):
    return json.loads(
        await _run_tool(
            name,
            args or {},
            connector=None,
            world=world if world is not None else _world(),
            snap=snap if snap is not None else {},
            turn=turn if turn is not None else BrainTurn(),
        )
    )


@pytest.mark.asyncio
async def test_bare_news_asks_for_symbols_without_mda(monkeypatch):
    n = {"calls": 0}

    async def _news(*_a, **_k):
        n["calls"] += 1
        raise AssertionError("bare news() must not call _mda_news")

    monkeypatch.setattr("abcxauto.brain._mda_news", _news)
    data = await _tool("news", {})
    assert n["calls"] == 0
    assert data["ok"] is False
    assert data["need"] == "symbols[]"
    assert data["items"] == []
    assert data.get("fetched") is False
    assert "already" not in data


@pytest.mark.asyncio
async def test_news_symbols_fetches_only_asked(monkeypatch):
    seen: list[str] = []

    async def _news(syms, **_k):
        seen.extend(str(s).upper() for s in (syms or []))
        return [{"symbol": s, "headline": f"{s} head"} for s in (syms or [])]

    monkeypatch.setattr("abcxauto.brain._mda_news", _news)
    data = await _tool("news", {"symbols": ["AMD"]})
    assert seen == ["AMD"]
    assert [it.get("symbol") for it in data.get("items") or []] == ["AMD"]
    assert data.get("source") == "mda"


@pytest.mark.asyncio
async def test_bare_news_after_scan_lists_already_without_fetch(monkeypatch):
    n = {"calls": 0}

    async def _news(*_a, **_k):
        n["calls"] += 1
        raise AssertionError("scan names must not trigger a news fetch")

    monkeypatch.setattr("abcxauto.brain._mda_news", _news)
    world = _world()
    world.scan_fetched = ["ALB", "NKE"]
    snap = {
        "scan_hits": {
            "rows": [
                {"symbol": "ALB", "open_gap_pct": -3.8},
                {"symbol": "NKE", "open_gap_pct": -3.3},
            ]
        }
    }
    data = await _tool("news", {}, world=world, snap=snap)
    assert n["calls"] == 0
    assert data["ok"] is False
    assert data["need"] == "symbols[]"
    assert data["items"] == []
    assert data.get("fetched") is False
    already = data.get("already") or []
    assert "ALB" in already
    assert "NKE" in already
    assert not any("headline" in str(it).lower() for it in already)


@pytest.mark.asyncio
async def test_bare_odds_with_positions_does_not_search(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("positions must not invent a Polymarket search")

        async def get(self, *a, **k):
            raise AssertionError("positions must not invent a Polymarket search")

        async def aclose(self):
            return None

    monkeypatch.setattr("abcxauto.prediction_odds.httpx.AsyncClient", Boom)
    world = _world(positions=[{"symbol": "NVDA", "quantity": 10}])
    data = await _tool("odds", {}, world=world)
    assert data["ok"] is False
    assert data["need"] == "query|symbols[]"
    assert data.get("events") == []
    assert "NVDA" not in str(data.get("searched") or [])
    assert "Nvidia" not in str(data)


@pytest.mark.asyncio
async def test_odds_query_or_symbols_calls_fetch_odds(monkeypatch):
    seen: list[dict] = []

    async def _fake_odds(*, symbols=None, query="", **_k):
        seen.append({"symbols": list(symbols or []), "query": query})
        return {
            "source": "polymarket",
            "events": [],
            "searched": list(symbols or []) or [query],
        }

    monkeypatch.setattr("abcxauto.prediction_odds.fetch_odds", _fake_odds)
    q = await _tool("odds", {"query": "Fed September"})
    named = await _tool("odds", {"symbols": ["NVDA"]})
    assert seen == [
        {"symbols": [], "query": "Fed September"},
        {"symbols": ["NVDA"], "query": ""},
    ]
    assert q.get("searched") == ["Fed September"]
    assert named.get("searched") == ["NVDA"]


@pytest.mark.asyncio
async def test_research_brief_empty_snap_is_a_choice():
    data = await _tool("research_brief", {}, snap={})
    this_look = data.get("this_look") or {}
    assert this_look.get("tools") == []
    assert this_look.get("facts") == []
    assert "scan|news" in str(data.get("need") or "")
    assert "candles" in str(data.get("need") or "")
    prior = data.get("prior_session") or {}
    assert "expectancy" not in data
    assert "expectancy" not in prior


@pytest.mark.asyncio
async def test_research_brief_this_look_after_scan_and_news():
    snap: dict = {}
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "AMD", "open_gap_pct": 4.2}], "source": "ibkr"},
    )
    note_research_tool(
        snap,
        "news",
        {"items": [{"symbol": "NVDA", "headline": "NVDA prints after hours"}]},
    )
    data = await _tool("research_brief", {}, snap=snap)
    tools = (data.get("this_look") or {}).get("tools") or []
    assert "scan" in tools
    assert "news" in tools
    assert "need" not in data
