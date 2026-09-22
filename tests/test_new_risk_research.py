"""New-risk send needs this-look structure plus news|web|option_facts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.desk_mode import REASON_RESEARCH_THIN, new_risk_research_error
from abcxauto.look_snapshot import begin_look, record_look_tool
from abcxauto.world_state import WorldState


def _thin_note(msg: str) -> None:
    assert msg
    assert "research_thin" in msg


def test_quote_or_scan_last_only_is_research_thin():
    snap = {
        "ibkr_live_quotes": {"AAPL": 178.5},
        "scan_hits": {"rows": [{"symbol": "AAPL", "last": 178.5}]},
    }
    _thin_note(new_risk_research_error("AAPL", snap, strat="market_bracket"))
    _thin_note(new_risk_research_error("AAPL", snap, strat="bracket"))


def test_empty_snap_is_research_thin():
    _thin_note(new_risk_research_error("AAPL", {}, strat="market_bracket"))
    _thin_note(new_risk_research_error("AAPL", None, strat="bracket"))


def _bar_row(**extra) -> dict:
    row = {"open": 178.0, "high": 179.0, "low": 177.5, "last": 178.5, "n": 40}
    row.update(extra)
    return row


def test_session_range_plus_news_clears():
    snap = {
        "session_range": {"AAPL": _bar_row()},
        "news_items": [{"symbol": "AAPL", "headline": "AAPL prints after hours"}],
    }
    assert new_risk_research_error("AAPL", snap, strat="market_bracket") == ""


def test_session_range_plus_research_web_clears():
    snap = {
        "session_range": {"AAPL": _bar_row()},
        "research_web": {
            "url": "https://example.com/ir",
            "title": "IR",
            "text": "announces merger",
        },
    }
    assert new_risk_research_error("AAPL", snap, strat="bracket") == ""


def test_session_range_only_is_research_thin():
    snap = {"session_range": {"AAPL": _bar_row()}}
    _thin_note(new_risk_research_error("AAPL", snap, strat="market_bracket"))


def test_naked_last_plus_news_is_research_thin():
    snap = {
        "session_range": {"AAPL": {"last": 178.5}},
        "news_items": [{"symbol": "AAPL", "headline": "AAPL prints after hours"}],
    }
    _thin_note(new_risk_research_error("AAPL", snap, strat="market_bracket"))


def test_scan_stashed_session_range_stays_thin_after_compact():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.think_stream import _compact_session_range

    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    compacted = _compact_session_range(
        {
            "AAPL": {
                "date": today,
                "today": True,
                "open": 178.0,
                "low": 177.5,
                "last": 178.5,
                "print": "live_open",
                "source": "scan",
                "gap_pct": 1.2,
            }
        }
    )
    row = compacted["AAPL"]
    assert row["print"] == "live_open"
    assert row["source"] == "scan"
    snap = {
        "session_range": compacted,
        "news_items": [{"symbol": "AAPL", "headline": "AAPL prints after hours"}],
    }
    _thin_note(new_risk_research_error("AAPL", snap, strat="market_bracket"))


def test_option_strat_session_range_plus_option_facts_clears():
    snap = {
        "session_range": {"SPY": _bar_row(open=500.0, high=501.0, low=499.0, last=500.12)},
        "option_facts": [{"symbol": "SPY", "right": "P", "strike": 500.0}],
    }
    assert new_risk_research_error("SPY", snap, strat="vertical_spread") == ""


def test_oca_is_not_new_risk():
    from abcxauto.agent_loop import is_new_risk

    assert is_new_risk("oca", {}) is False
    assert is_new_risk("cancel_order") is False


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
        capacity={
            "open_count": 0,
            "max_open_positions": 6,
            "slots_left": 6,
            "allows_new_risk": True,
        },
    )
    base.update(kwargs)
    return WorldState(**base)


def _look_quote_snap(symbol: str = "AAPL", last: float = 178.5) -> dict:
    snap = {
        "account": {"netliquidation": 37000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {symbol: last},
        "scan_hits": {"rows": [{"symbol": symbol, "last": last}]},
    }
    begin_look(snap)
    record_look_tool(
        snap,
        "quote",
        {
            "symbol": symbol,
            "last": last,
            "bid": last - 0.1,
            "ask": last + 0.1,
            "mid": last,
            "source": "ibkr",
            "freshness": "live",
        },
    )
    return snap


def _new_risk_ticket() -> dict:
    return {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "AAPL",
            "direction": "LONG",
            "stop_price": 170.0,
            "target_price": 190.0,
            "quantity": 1,
            "price_hint": 178.5,
            "card": "flush bounce",
        },
        "rationale": "new risk",
    }


def _close_ticket() -> dict:
    return {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "AAPL",
            "direction": "LONG",
            "stop_price": 170.0,
            "target_price": 190.0,
            "quantity": 10,
            "price_hint": 178.5,
            "card": "exit",
            "closing_position": True,
            "conId": 7,
        },
        "rationale": "close the lot",
    }


def _stub_siblings(monkeypatch) -> list:
    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    return sent


@pytest.mark.asyncio
async def test_execute_ticket_refuses_research_thin(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    sent = _stub_siblings(monkeypatch)
    called: list[str] = []
    real = new_risk_research_error

    def spy(symbol, snap, *, strat=""):
        called.append(str(symbol))
        return real(symbol, snap, strat=strat)

    monkeypatch.setattr("abcxauto.desk_mode.new_risk_research_error", spy)
    result = await execute_ticket(
        _new_risk_ticket(),
        MagicMock(),
        _world(),
        _look_quote_snap(),
    )
    assert called
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == REASON_RESEARCH_THIN == "research_thin"
    assert "research_thin" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_closing_position_skips_research_thin(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    sent = _stub_siblings(monkeypatch)
    called: list[str] = []

    def spy(symbol, snap, *, strat=""):
        called.append(str(symbol))
        return "research_thin: should not run"

    monkeypatch.setattr("abcxauto.desk_mode.new_risk_research_error", spy)
    pos = [
        {
            "symbol": "AAPL",
            "secType": "STK",
            "sec_type": "STK",
            "quantity": 10,
            "conId": 7,
        }
    ]
    snap = _look_quote_snap()
    snap["positions"] = pos
    result = await execute_ticket(
        _close_ticket(),
        MagicMock(),
        _world(flat=False, positions=pos),
        snap,
    )
    assert called == []
    assert result.get("reason_code") != REASON_RESEARCH_THIN
    assert "research_thin" not in str(result.get("note") or "")
    assert sent == [] or result.get("status") == "ok"


def test_send_preview_appends_research_thin_note():
    from abcxauto.send_preview import collect_would_refuse, preview_ticket

    ticket = _new_risk_ticket()
    snap = _look_quote_snap()
    world = _world()
    reasons = collect_would_refuse(ticket, world=world, snap=snap)
    blob = " ".join(str(r) for r in reasons)
    assert "research_thin" in blob
    out = preview_ticket(ticket, world=world, snap=snap)
    assert any("research_thin" in str(r) for r in (out.get("would_refuse") or []))
