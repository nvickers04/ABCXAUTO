"""Regression: a timed-out snap must not fabricate NL=0 / wiped-account."""

from __future__ import annotations

import asyncio
import logging

import pytest

from abcxauto.agent_loop import gate_ticket, snap
from abcxauto.brain import _book_payload
from abcxauto.scorecard import compute_scorecard
from abcxauto.world_state import WorldState, account_float, build_world_state, day_facts


LIVE_NL = 32284.80
STARTUP_NL = 36638.36


class FakeConnector:
    connected = True
    net_liquidation = 0.0
    cash_value = 0.0

    async def connect(self):
        return True

    async def get_positions(self):
        return []

    async def get_open_orders(self):
        return []

    async def get_account_summary(self):
        return {"netliquidation": 1000, "unrealizedpnl": 0}


def _ok_tool(_c, name: str, _a=None):
    return {
        "account_summary": {"NetLiquidation": LIVE_NL, "netliquidation": LIVE_NL},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "regular"},
        "quote": {"symbol": (_a or {}).get("symbol") or "SPY", "last": 500},
    }.get(name, {})


class _StartupJournal:
    def startup_cash(self):
        return STARTUP_NL

    def account_performance(self):
        return {}

    def model_usage_totals(self):
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _new_risk_ticket():
    return {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SPY", "quantity": 1, "direction": "LONG"},
    }


def _exit_ticket():
    return {
        "action": "vertical_spread",
        "strategy": "vertical_spread",
        "params": {
            "symbol": "JPM",
            "expiration": "20260918",
            "long_strike": 370.0,
            "short_strike": 375.0,
            "right": "C",
            "quantity": 1,
            "limit_price": 0.71,
            "closing_position": True,
        },
    }


@pytest.mark.asyncio
async def test_snap_keeps_finished_account_when_vix_hangs(monkeypatch, caplog):
    async def tools(_c, name: str, a=None):
        if name == "quote" and str((a or {}).get("symbol") or "").upper() == "VIX":
            await asyncio.sleep(2)
            return {"symbol": "VIX", "last": 15}
        if name == "account_summary":
            return {"NetLiquidation": LIVE_NL, "netliquidation": LIVE_NL}
        return _ok_tool(_c, name, a)

    monkeypatch.setattr("abcxauto.agent_loop._tool", tools)
    monkeypatch.setattr("abcxauto.agent_loop.SNAP_S", 0.15)
    with caplog.at_level(logging.WARNING, logger="abcxauto.agent_loop"):
        out = await snap(FakeConnector())
    nl = account_float(out["account"], "netliquidation", "NetLiquidation")
    assert nl == pytest.approx(LIVE_NL)
    assert nl != 0
    ws = build_world_state(cycle=1, snap=out, opportunities=[], news_items=[])
    assert ws.net_liquidation == pytest.approx(LIVE_NL)
    facts = day_facts(ws, {})
    assert facts["nl"] == pytest.approx(LIVE_NL)
    warn = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "quote VIX" in warn
    assert "account_summary" in warn
    assert "lost" in warn.lower()


@pytest.mark.asyncio
async def test_snap_account_failure_nl_unknown_blocks_new_risk_allows_exit(monkeypatch):
    async def tools(_c, name: str, a=None):
        if name == "account_summary":
            raise RuntimeError("account_summary failed")
        return _ok_tool(_c, name, a)

    monkeypatch.setattr("abcxauto.agent_loop._tool", tools)
    out = await snap(FakeConnector())
    nl = account_float(out.get("account") or {}, "netliquidation", "NetLiquidation")
    assert nl is None
    assert out["book_unreliable"] is True
    ws = build_world_state(cycle=1, snap=out, opportunities=[], news_items=[])
    assert ws.net_liquidation is None
    assert ws.net_liquidation != 0.0
    assert bool((ws.gates or {}).get("book_unreliable")) is True
    blocked, forced = gate_ticket(_new_risk_ticket(), ws)
    assert blocked == "blocked"
    assert "unreliable" in str((forced or {}).get("note") or "").lower()
    strat, exit_forced = gate_ticket(_exit_ticket(), ws)
    assert strat == "vertical_spread"
    assert exit_forced is None
    sc = compute_scorecard(equity=ws.net_liquidation, journal=_StartupJournal())
    assert sc.get("nl_unknown") is True
    assert sc.get("book_return_pct") is None
    assert sc.get("book_return_pct") != -100
    assert sc.get("ruin") != 1.0


def test_scorecard_unknown_nl_is_not_catastrophic():
    sc = compute_scorecard(equity=None, journal=_StartupJournal())
    assert sc.get("net_liquidation") is None
    assert sc.get("book_return_pct") is None
    assert sc.get("book_pnl") is None
    assert sc.get("book_return_pct") != -100
    assert sc.get("ruin") != 1.0
    assert sc.get("nl_unknown") is True


@pytest.mark.asyncio
async def test_book_payload_incomplete_snap_not_confident_zero(monkeypatch):
    async def tools(_c, name: str, a=None):
        if name == "account_summary":
            raise RuntimeError("account_summary failed")
        return _ok_tool(_c, name, a)

    monkeypatch.setattr("abcxauto.agent_loop._tool", tools)
    out = await snap(FakeConnector())
    ws = build_world_state(cycle=1, snap=out, opportunities=[], news_items=[])
    blob = _book_payload(ws, snap=out)
    world = blob.get("world") or {}
    day = blob.get("day") or {}
    assert world.get("net_liquidation") is None
    assert day.get("nl") is None
    assert world.get("net_liquidation") not in (0, 0.0)
    assert day.get("nl") not in (0, 0.0)
    assert blob.get("snap_incomplete") is True or world.get("nl_unavailable") is True


@pytest.mark.asyncio
async def test_snap_warning_names_lost_and_survived(monkeypatch, caplog):
    async def tools(_c, name: str, a=None):
        if name == "quote" and str((a or {}).get("symbol") or "").upper() == "VIX":
            await asyncio.sleep(2)
            return {"symbol": "VIX", "last": 15}
        return _ok_tool(_c, name, a)

    monkeypatch.setattr("abcxauto.agent_loop._tool", tools)
    monkeypatch.setattr("abcxauto.agent_loop.SNAP_S", 0.15)
    with caplog.at_level(logging.WARNING, logger="abcxauto.agent_loop"):
        await snap(FakeConnector())
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    blob = " ".join(msgs)
    assert msgs, "expected a WARNING naming lost snap fetches"
    assert "quote VIX" in blob
    assert "account_summary" in blob
    assert "survived" in blob.lower() or "lost" in blob.lower()

@pytest.mark.asyncio
async def test_snap_overlays_connector_cache_when_account_empty(monkeypatch):
    async def tools(_c, name: str, a=None):
        if name == "account_summary":
            return {}
        return _ok_tool(_c, name, a)

    class Cached(FakeConnector):
        net_liquidation = LIVE_NL
        cash_value = 32255.73

    monkeypatch.setattr("abcxauto.agent_loop._tool", tools)
    out = await snap(Cached())
    assert out.get("nl_source") == "connector_cache"
    assert out.get("net_liquidation") == pytest.approx(LIVE_NL)
    assert account_float(out["account"], "netliquidation", "NetLiquidation") == pytest.approx(LIVE_NL)
    assert out["book_unreliable"] is True
    ws = build_world_state(cycle=1, snap=out, opportunities=[], news_items=[])
    assert ws.net_liquidation == pytest.approx(LIVE_NL)
    blocked, _forced = gate_ticket(_new_risk_ticket(), ws)
    assert blocked == "blocked"
    strat, exit_forced = gate_ticket(_exit_ticket(), ws)
    assert strat == "vertical_spread"
    assert exit_forced is None