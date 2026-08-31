"""Structure referee: geometry gates + lessons (Grok owns prices)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.proposals import ProposalValidationError, validate_proposal
from abcxauto.structure_grade import (
    GEOMETRY_ENTRY_STALE,
    GEOMETRY_QUOTE_REQUIRED,
    GEOMETRY_STOP_TOO_TIGHT,
    GEOMETRY_STOP_TOO_WIDE,
    GEOMETRY_STOP_WRONG_SIDE,
    SCRAPE_SUSPECT,
    STRUCTURE_OK,
    append_structure_event,
    check_live_geometry,
    detect_scrape_from_fills,
    recent_structure_lessons,
    structure_cooldown_symbols,
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


def test_new_risk_geometry_rejects_a_price_hint_as_live():
    params = {
        "symbol": "SNDK",
        "quantity": 10,
        "direction": "LONG",
        "stop_price": 88.0,
        "target_price": 93.0,
        "price_hint": 91.5,
    }
    ok, code, msg = check_live_geometry(
        "market_bracket", params, quote_last=None, require_live=True
    )
    assert ok is False
    assert code == GEOMETRY_QUOTE_REQUIRED
    assert "IBKR live last" in msg
    ok2, code2, _ = check_live_geometry(
        "market_bracket", params, quote_last=91.5, require_live=True,
        session={"low": 88.0, "today": True},
    )
    assert ok2 is True
    assert code2 == STRUCTURE_OK


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


def test_opening_low_stop_skips_percent_bands():
    """Opening-low tape is still session-level; invented % is not a send reject."""
    params = {
        "symbol": "SNDK",
        "quantity": 2,
        "direction": "LONG",
        "stop_price": 92.0,
        "target_price": 104.0,
        "price_hint": 100.0,
    }
    wide = check_live_geometry(
        "market_bracket", params, quote_last=100.0, posture="balanced"
    )
    assert wide[0] is True
    assert wide[1] == STRUCTURE_OK
    ok, code, msg = check_live_geometry(
        "market_bracket",
        params,
        quote_last=100.0,
        posture="balanced",
        session={"low": 92.0, "high": 101.0, "today": True},
    )
    assert ok is True
    assert code == STRUCTURE_OK
    assert "session" in msg
    tight = {
        **params,
        "stop_price": 99.85,
        "target_price": 104.0,
        "price_hint": 100.0,
    }
    bare_tight = check_live_geometry(
        "market_bracket", tight, quote_last=100.0, posture="balanced"
    )
    assert bare_tight[0] is True
    assert bare_tight[1] == STRUCTURE_OK
    pinned, pin_code, _ = check_live_geometry(
        "market_bracket",
        tight,
        quote_last=100.0,
        posture="balanced",
        session={"low": 99.85, "high": 101.0, "today": True},
    )
    assert pinned is True
    assert pin_code == STRUCTURE_OK
    stale = check_live_geometry(
        "market_bracket",
        params,
        quote_last=100.0,
        posture="balanced",
        session={"low": 92.0, "high": 101.0, "today": False},
    )
    assert stale[0] is True
    assert stale[1] == STRUCTURE_OK


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
            "screen QQQ",
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
        "legal screen",
        quote_last=710.0,
        posture="balanced",
    )
    assert prop.strategy == "market_bracket"


def test_invented_stop_pct_is_not_a_send_gate():
    """Grok owns stop distance. Clerk posture % bands are not a send reject."""
    wide = {
        "symbol": "QQQ",
        "quantity": 2,
        "direction": "LONG",
        "stop_price": 639.0,  # ~10% — used to be geometry_stop_too_wide
        "target_price": 750.0,
    }
    ok, code, _ = check_live_geometry(
        "market_bracket", wide, quote_last=710.0, posture="defensive",
    )
    assert ok is True
    assert code == STRUCTURE_OK

    tight = {
        "symbol": "QQQ",
        "quantity": 2,
        "direction": "LONG",
        "stop_price": 709.65,  # ~0.05% — used to be geometry_stop_too_tight
        "target_price": 712.0,
    }
    ok, code, _ = check_live_geometry(
        "market_bracket", tight, quote_last=710.0, posture="defensive",
    )
    assert ok is True
    assert code == STRUCTURE_OK


def test_bracket_entry_far_from_quote_is_not_a_pct_gate():
    params = {
        "symbol": "QQQ",
        "quantity": 2,
        "direction": "LONG",
        "entry_price": 680.0,  # ~4% below last — used to be geometry_entry_stale
        "stop_price": 670.0,
        "target_price": 720.0,
    }
    ok, code, _ = check_live_geometry(
        "bracket", params, quote_last=710.0, posture="balanced",
    )
    assert ok is True
    assert code == STRUCTURE_OK


def test_validate_proposal_does_not_invent_stop_pct_gate():
    prop = validate_proposal(
        "market_bracket",
        {
            "symbol": "QQQ",
            "quantity": 2,
            "direction": "LONG",
            "stop_price": 639.0,
            "target_price": 750.0,
            "price_hint": 710.0,
        },
        "wide stop is grok's",
        quote_last=710.0,
        posture="defensive",
    )
    assert prop.strategy == "market_bracket"


def test_invented_pct_lessons_do_not_cooldown():
    cool = structure_cooldown_symbols(
        [
            {
                "symbol": "QQQ",
                "reason_code": GEOMETRY_STOP_TOO_WIDE,
                "outcome": "geometry_rejected",
            },
            {
                "symbol": "SPY",
                "reason_code": GEOMETRY_STOP_TOO_TIGHT,
                "outcome": "geometry_rejected",
            },
            {
                "symbol": "IWM",
                "reason_code": GEOMETRY_ENTRY_STALE,
                "outcome": "geometry_rejected",
            },
            {
                "symbol": "AAPL",
                "reason_code": GEOMETRY_STOP_WRONG_SIDE,
                "outcome": "geometry_rejected",
            },
        ]
    )
    assert "QQQ" not in cool
    assert "SPY" not in cool
    assert "IWM" not in cool
    assert cool.get("AAPL") == GEOMETRY_STOP_WRONG_SIDE


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
    assert lessons[0]["reason_code"] == GEOMETRY_STOP_WRONG_SIDE


def test_detect_scrape_from_fills():
    fills = [
        {"ts": "2026-07-16T20:45:59Z", "symbol": "QQQ", "side": "BOT", "quantity": 9},
        {"ts": "2026-07-16T20:46:01Z", "symbol": "QQQ", "side": "SLD", "quantity": 9},
    ]
    assert detect_scrape_from_fills(fills, symbol="QQQ") is True
    assert detect_scrape_from_fills(fills, symbol="SPY") is False


def test_detect_scrape_from_fills_stk_only():
    """2026-08-19: same-second SPY OPT/BAG fills are not a stock scrape."""
    ts = "2026-08-19T18:13:00Z"
    mixed = [
        {"ts": ts, "symbol": "SPY", "side": "BOT", "sec_type": "STK", "quantity": 11},
        {"ts": ts, "symbol": "SPY", "side": "SLD", "sec_type": "OPT", "quantity": 1},
        {"ts": ts, "symbol": "SPY", "side": "BOT", "secType": "BAG", "quantity": 1},
    ]
    assert detect_scrape_from_fills(mixed, symbol="SPY") is False
    stk_round_trip = [
        {"ts": ts, "symbol": "SPY", "side": "BOT", "sec_type": "STK", "quantity": 11},
        {"ts": "2026-08-19T18:13:01Z", "symbol": "SPY", "side": "SLD", "sec_type": "STK", "quantity": 11},
    ]
    assert detect_scrape_from_fills(stk_round_trip, symbol="SPY") is True


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


def _spy_bracket_act():
    return {
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "direction": "LONG",
            "stop_price": 766.7,
            "target_price": 773.8,
            "quantity": 11,
        },
    }


def _spy_bracket_result():
    return {
        "success": True,
        "filled": True,
        "symbol": "SPY",
        "entry_price": 769.5,
    }


@pytest.mark.asyncio
async def test_post_act_opt_bag_fills_keep_live_stk_plan(monkeypatch, tmp_path):
    """OPT/BAG fills in the 15s window must not scrape-close a live SPY STK 11."""
    from abcxauto.agent_loop import _post_act_structure_and_plan
    from abcxauto.trade_plan import load_trade_plan

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))

    ts = "2026-08-19T18:13:00Z"

    class Conn:
        async def get_positions(self):
            return [{"symbol": "SPY", "secType": "STK", "quantity": 11}]

        async def get_recent_executions(self):
            return [
                {"ts": ts, "symbol": "SPY", "side": "BOT", "sec_type": "STK", "quantity": 11},
                {"ts": ts, "symbol": "SPY", "side": "SLD", "sec_type": "OPT", "quantity": 1},
                {"ts": ts, "symbol": "SPY", "side": "BOT", "sec_type": "BAG", "quantity": 1},
            ]

    await _post_act_structure_and_plan(
        act=_spy_bracket_act(),
        strat="market_bracket",
        result=_spy_bracket_result(),
        judgment={"thesis": "spy long"},
        snap={"positions": []},
        quote_last=769.5,
        connector=Conn(),
    )
    plan = load_trade_plan()
    assert plan is not None
    assert plan.symbol == "SPY"
    assert plan.quantity == pytest.approx(11)
    assert plan.close_reason == ""
    lessons = recent_structure_lessons(3)
    assert lessons
    assert lessons[0]["outcome"] == STRUCTURE_OK


@pytest.mark.asyncio
async def test_post_act_stk_scrape_keeps_plan_when_qty_live(monkeypatch, tmp_path):
    """close_trade_plan(scrape_suspect) must not run while stk_qty_for_symbol > 0."""
    from abcxauto.agent_loop import _post_act_structure_and_plan
    from abcxauto.trade_plan import load_trade_plan

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))

    class Conn:
        async def get_positions(self):
            return [{"symbol": "SPY", "secType": "STK", "quantity": 11}]

        async def get_recent_executions(self):
            return [
                {
                    "ts": "2026-08-19T18:13:00Z",
                    "symbol": "SPY",
                    "side": "BOT",
                    "sec_type": "STK",
                    "quantity": 11,
                },
                {
                    "ts": "2026-08-19T18:13:01Z",
                    "symbol": "SPY",
                    "side": "SLD",
                    "sec_type": "STK",
                    "quantity": 11,
                },
            ]

    closed: list[str] = []

    def _record(reason="", *_a, **_k):
        closed.append(reason)

    monkeypatch.setattr("abcxauto.agent_loop.close_trade_plan", _record)
    await _post_act_structure_and_plan(
        act=_spy_bracket_act(),
        strat="market_bracket",
        result=_spy_bracket_result(),
        judgment={"thesis": "spy long"},
        snap={"positions": []},
        quote_last=769.5,
        connector=Conn(),
    )
    assert "scrape_suspect" not in closed
    plan = load_trade_plan()
    assert plan is not None
    assert abs(plan.quantity or 0) == pytest.approx(11)


@pytest.mark.asyncio
async def test_post_act_stk_scrape_closes_when_flat(monkeypatch, tmp_path):
    """A real STK round-trip with no leftover qty still scrape-closes the plan."""
    from abcxauto.agent_loop import _post_act_structure_and_plan
    from abcxauto.trade_plan import load_trade_plan

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))

    class Conn:
        async def get_positions(self):
            return []

        async def get_recent_executions(self):
            return [
                {
                    "ts": "2026-07-16T20:45:59Z",
                    "symbol": "QQQ",
                    "side": "BOT",
                    "sec_type": "STK",
                    "quantity": 9,
                },
                {
                    "ts": "2026-07-16T20:46:01Z",
                    "symbol": "QQQ",
                    "side": "SLD",
                    "sec_type": "STK",
                    "quantity": 9,
                },
            ]

    await _post_act_structure_and_plan(
        act={
            "strategy": "market_bracket",
            "params": {
                "symbol": "QQQ",
                "direction": "LONG",
                "stop_price": 700.0,
                "target_price": 720.0,
                "quantity": 9,
            },
        },
        strat="market_bracket",
        result={"success": True, "filled": True, "symbol": "QQQ", "entry_price": 710.0},
        judgment={"thesis": "x"},
        snap={"positions": []},
        quote_last=710.0,
        connector=Conn(),
    )
    assert load_trade_plan() is None
    lessons = recent_structure_lessons(3)
    assert lessons
    assert lessons[0]["outcome"] == SCRAPE_SUSPECT


@pytest.mark.asyncio
async def test_agent_loop_blocks_inverted_before_send(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from abcxauto.pro_engine import ProEngine

    monkeypatch.setenv("ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "rev.json"))

    from abcxauto.memory import reset_journal

    # A carded ticket still has to survive geometry.
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
        "stance": "new_entry",
        "thesis": "QQQ pullback",
        "focus": "QQQ #1",
        "dismissed": "",
        "intent": {"kind": "new_entry", "symbol": "QQQ", "direction": "LONG", "urgency": "med"},
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
            "card": "qqq pullback",
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
    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_json_as_turn(grok))
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
    class Conn:
        connected = True

    from abcxauto.agent_loop import snap

    eng = ProEngine()
    eng.conn = Conn()
    s = await snap(eng.conn)
    out = await eng._host_think(1, None, s)
    assert out["strat"] == "blocked"
    assert send_calls == []
    assert "geometry_stop_wrong_side" in str(out.get("structure_grade") or out.get("result"))


async def _async(val):
    return val
