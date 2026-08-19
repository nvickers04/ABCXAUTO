"""Structure referee: geometry gates + lessons (Grok owns prices)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.proposals import ProposalValidationError, validate_proposal
from abcxauto.structure_grade import (
    GEOMETRY_STOP_WRONG_SIDE,
    SCRAPE_SUSPECT,
    STRUCTURE_OK,
    append_structure_event,
    check_live_geometry,
    detect_scrape_from_fills,
    format_structure_lessons_for_prompt,
    recent_structure_lessons,
)


def test_qqq_inverted_stop_rejected():
    """Replay of 2026-07-16 QQQ scrape: LONG stop above live fill."""
    params = {
        "symbol": "QQQ",
        "quantity": 9,
        "direction": "LONG",
        "stop_price": 711.99,
        "target_price": 729.22,
    }
    ok, code, msg = check_live_geometry(
        "market_bracket", params, quote_last=709.83, posture="aggressive",
    )
    assert ok is False
    assert code == GEOMETRY_STOP_WRONG_SIDE
    assert "below" in msg.lower() or "wrong-side" in msg.lower()


def test_valid_long_market_bracket_passes():
    params = {
        "symbol": "QQQ",
        "quantity": 9,
        "direction": "LONG",
        "stop_price": 700.0,
        "target_price": 720.0,
        "price_hint": 710.0,
    }
    ok, code, _ = check_live_geometry(
        "market_bracket", params, quote_last=710.0, posture="balanced",
    )
    assert ok is True
    assert code == STRUCTURE_OK


def test_validate_proposal_blocks_qqq_geometry():
    with pytest.raises(ProposalValidationError) as ei:
        validate_proposal(
            "market_bracket",
            {
                "symbol": "QQQ",
                "quantity": 9,
                "direction": "LONG",
                "stop_price": 711.99,
                "target_price": 729.22,
            },
            "hunt QQQ",
            quote_last=709.83,
            posture="aggressive",
        )
    assert "geometry_stop_wrong_side" in str(ei.value)


def test_validate_proposal_accepts_legal_geometry():
    prop = validate_proposal(
        "market_bracket",
        {
            "symbol": "QQQ",
            "quantity": 2,
            "direction": "LONG",
            "stop_price": 700.0,
            "target_price": 720.0,
            "price_hint": 710.0,
        },
        "legal hunt",
        quote_last=710.0,
        posture="balanced",
    )
    assert prop.strategy == "market_bracket"


def test_structure_lessons_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    append_structure_event(
        {
            "strategy": "market_bracket",
            "symbol": "QQQ",
            "outcome": "geometry_rejected",
            "reason_code": GEOMETRY_STOP_WRONG_SIDE,
            "message": "stop above live",
            "quote": 709.83,
        }
    )
    lessons = recent_structure_lessons(3)
    assert lessons
    assert lessons[0]["symbol"] == "QQQ"
    text = format_structure_lessons_for_prompt(lessons)
    assert "STRUCTURE LESSONS" in text
    assert "QQQ" in text


def test_detect_scrape_from_fills():
    fills = [
        {"ts": "2026-07-16T20:45:59Z", "symbol": "QQQ", "side": "BOT", "quantity": 9},
        {"ts": "2026-07-16T20:46:01Z", "symbol": "QQQ", "side": "SLD", "quantity": 9},
    ]
    assert detect_scrape_from_fills(fills, symbol="QQQ") is True
    assert detect_scrape_from_fills(fills, symbol="SPY") is False


@pytest.mark.asyncio
async def test_quote_prefers_live_over_stale_opp(monkeypatch):
    from abcxauto.agent_loop import _quote_for_action

    class Conn:
        connected = True

    async def _tool(_c, name, a=None):
        assert name == "quote"
        assert (a or {}).get("symbol") == "QQQ"
        return {"symbol": "QQQ", "last": 708.20}

    monkeypatch.setattr("abcxauto.agent_loop._tool", _tool)
    act = {
        "params": {
            "symbol": "QQQ",
            "price_hint": 717.74,  # stale opp last Grok recycled
            "stop_price": 711.99,
            "target_price": 729.22,
        }
    }
    snap = {
        "opportunities": [{"symbol": "QQQ", "last": 717.74}],
        "spy_quote": {"last": 500},
    }
    q = await _quote_for_action(act, snap, Conn())
    assert q == pytest.approx(708.20)
    # Stale hint overwritten for gates
    assert act["params"]["price_hint"] == pytest.approx(708.20)


@pytest.mark.asyncio
async def test_wrong_side_fill_records_scrape(monkeypatch, tmp_path):
    from abcxauto.agent_loop import _post_act_structure_and_plan
    from abcxauto.structure_grade import recent_structure_lessons

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    await _post_act_structure_and_plan(
        act={
            "params": {
                "symbol": "QQQ",
                "direction": "LONG",
                "stop_price": 711.99,
                "target_price": 729.22,
                "quantity": 9,
            }
        },
        strat="market_bracket",
        result={
            "success": True,
            "filled": True,
            "symbol": "QQQ",
            "entry_price": 708.17,
        },
        judgment={"thesis": "x"},
        snap={"positions": []},
        quote_last=708.17,
        connector=None,
    )
    lessons = recent_structure_lessons(3)
    assert lessons
    assert lessons[0]["outcome"] == "scrape_suspect"
    assert lessons[0]["symbol"] == "QQQ"


@pytest.mark.asyncio
async def test_successful_bracket_persists_plan_and_structure(monkeypatch, tmp_path):
    """Regression: inner save_trade_plan import must not shadow bracket success path."""
    from abcxauto.agent_loop import _post_act_structure_and_plan
    from abcxauto.structure_grade import recent_structure_lessons
    from abcxauto.trade_plan import load_trade_plan

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    await _post_act_structure_and_plan(
        act={
            "strategy": "market_bracket",
            "params": {
                "symbol": "AAPL",
                "direction": "LONG",
                "stop_price": 220.0,
                "target_price": 240.0,
                "quantity": 10,
            },
        },
        strat="market_bracket",
        result={
            "success": True,
            "filled": True,
            "symbol": "AAPL",
            "entry_price": 230.0,
        },
        judgment={"thesis": "aapl bounce"},
        snap={"positions": []},
        quote_last=230.0,
        connector=None,
    )
    plan = load_trade_plan()
    assert plan is not None
    assert plan.symbol == "AAPL"
    assert plan.entry_price == pytest.approx(230.0)
    lessons = recent_structure_lessons(3)
    assert lessons
    assert lessons[0]["outcome"] == STRUCTURE_OK
    assert lessons[0]["symbol"] == "AAPL"
    assert lessons[0]["message"] == "dispatched"


@pytest.mark.asyncio
async def test_agent_loop_blocks_inverted_before_send(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from abcxauto.agent_loop import run_cycle

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "rev.json"))

    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "j.db"), enabled=True)

    async def _tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 37000, "unrealizedpnl": 0},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": (_a or {}).get("symbol") or "SPY", "last": 709.83},
        }.get(name, {"last": 709.83})

    judgment = {
        "stance": "hunt",
        "thesis": "QQQ pullback",
        "focus": "QQQ #1",
        "dismissed": "",
        "intent": {"kind": "hunt", "symbol": "QQQ", "direction": "LONG", "urgency": "med"},
        "risk_budget_pct": 1.0,
        "regime_fit": True,
        "setup_grade": "A",
    }
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "QQQ",
            "quantity": 9,
            "direction": "LONG",
            "stop_price": 711.99,
            "target_price": 729.22,
        },
        "rationale": "bad geometry",
    }

    async def grok(_g, prompt, *, stage="act"):
        if stage == "judge" or "JUDGE STAGE" in prompt:
            return json.dumps(judgment)
        return json.dumps(act)

    send_calls = []

    async def boom(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("must not send inverted stop")

    from tests.conftest import grok_json_as_turn

    monkeypatch.setattr("abcxauto.agent_loop._tool", _tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_json_as_turn(grok))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom)
    async def _opps(*_a, **_k):
        return [{"symbol": "QQQ", "bias": "LONG", "score": 0.9}]

    async def _news(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _news)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper",
            risk_posture="aggressive",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper", risk_posture="aggressive",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.connection_status",
        lambda _c=None: {"ibkr_connected": True, "mda_configured": False, "trading_mode": "paper"},
    )

    class Conn:
        connected = True

    out = await run_cycle(1, Conn(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert send_calls == []
    assert "geometry_stop_wrong_side" in str(out.get("structure_grade") or out.get("result"))


async def _async(val):
    return val
