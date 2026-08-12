"""Smoke + PJA pipeline tests for agent_loop."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.agent_loop import (
    ALLOWED_ACTIONS,
    check_intent_coherence,
    extract_kahneman,
    normalize_action,
    run_cycle,
    snap,
    validate_judgment,
)
from abcxauto.world_state import WorldState, idle_streak_threshold, reset_idle_streak


class FakeConnector:
    connected = True

    async def connect(self):
        return True

    async def get_positions(self):
        return []

    async def get_open_orders(self):
        return []

    async def get_account_summary(self):
        return {"netliquidation": 1000, "unrealizedpnl": 0}


async def _fake_tool(_c, name: str, _a=None):
    return {
        "account_summary": {"netliquidation": 1000, "unrealizedpnl": 0},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "regular"},
        "quote": {"symbol": "SPY", "last": 500},
    }.get(name, {})


def _idle_judgment(**extra):
    base = {
        "stance": "idle",
        "thesis": "No edge — stay flat.",
        "focus": "Empty book, no A-setup.",
        "dismissed": "",
        "intent": {"kind": "idle", "symbol": None, "direction": None, "urgency": "low"},
        "risk_budget_pct": 0.5,
        "regime_fit": True,
        "setup_grade": "C",
    }
    base.update(extra)
    return base


def _hold_act(rationale: str = "flat"):
    return {"action": "hold", "strategy": "hold", "rationale": rationale}


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
            trading_mode="paper",
            grok_min_interval_s=0,
            signal_only=False,
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: SimpleNamespace(
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
            trading_mode="paper",
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.connection_status",
        lambda _c=None: {
            "ibkr_connected": True,
            "mda_configured": False,
            "trading_mode": "paper",
        },
    )
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.scan_opportunities",
        _async_empty_list,
    )
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)
    reset_idle_streak()


async def _async_empty_list(*_a, **_k):
    return []


async def _async_news(*_a, **_k):
    return []


@pytest.fixture(autouse=True)
def _stub_news(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.news_feed.fetch_agent_news",
        _async_news,
    )


def _pja_grok(judgment: dict, act: dict):
    calls: list[str] = []

    async def fake_grok(_g, prompt: str, *, stage: str = "act") -> str:
        calls.append(stage)
        if stage == "judge" or "JUDGE STAGE" in prompt:
            return json.dumps(judgment)
        return json.dumps(act)

    return fake_grok, calls


@pytest.mark.asyncio
async def test_hold_path_skips_send(monkeypatch):
    send_calls: list = []
    fake_grok, calls = _pja_grok(_idle_judgment(), _hold_act())

    async def boom_send(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("send_action must not run on hold")

    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "hold"
    assert out["result"]["status"] == "hold"
    assert send_calls == []
    # Always Judge + Act (no thrift skip). Hold comes from Act, not skip_act.
    assert calls == ["judge", "act"]
    assert out["judgment"]["stance"] == "idle"
    assert "world_state" in out


@pytest.mark.asyncio
async def test_act_prompt_includes_order_examples(monkeypatch):
    """Hunt still calls Act — ORDER EXAMPLES live on the Act prompt only."""
    prompts: list[tuple[str, str]] = []
    hunt_j = {
        "stance": "hunt",
        "thesis": "Look for a clean entry.",
        "focus": "Tape.",
        "dismissed": "QQQ - no edge this cycle",
        "intent": {
            "kind": "hunt", "symbol": "SPY", "direction": "LONG", "urgency": "med",
        },
        "risk_budget_pct": 0.5,
        "regime_fit": True,
        "setup_grade": "B",
    }
    hunt_act = {
        "action": "hold",
        "strategy": "hold",
        "rationale": "no clean structure yet",
    }

    async def tracking_grok(_g, prompt: str, *, stage: str = "act") -> str:
        prompts.append((stage, prompt))
        if stage == "judge" or "JUDGE STAGE" in prompt:
            return json.dumps(hunt_j)
        return json.dumps(hunt_act)

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(prompts) == 2
    assert prompts[0][0] == "judge"
    assert "JUDGE STAGE" in prompts[0][1]
    assert "ORDER EXAMPLES" not in prompts[0][1]
    assert prompts[1][0] == "act"
    assert "ORDER EXAMPLES" in prompts[1][1]
    assert "market_bracket" in prompts[1][1]
    assert "ACT STAGE" in prompts[1][1]


def test_extract_kahneman_stub_incomplete():
    k = extract_kahneman({"kahneman": {"system1_scan": "x"}})
    assert k["complete"] is False


def test_normalize_noop_to_hold():
    strat, forced = normalize_action({"action": "noop"})
    assert strat == "hold"
    assert forced is None
    assert "hold" in ALLOWED_ACTIONS
    assert "self_tune" in ALLOWED_ACTIONS


@pytest.mark.asyncio
async def test_snap_has_reality_pulse():
    out = await snap(FakeConnector())
    assert "reality_pulse" in out
    assert "portfolio_state" in out


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
        gates={"max_risk_per_trade_pct": 1.0},
        envelope={},
        regime={"session_phase": "mid", "trend_bias": "mixed", "vol_proxy": "normal"},
        portfolio_risk={"n_positions": 0, "top_symbol": "", "top_concentration_pct": 0},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        idle_streak=0,
        idle_top_symbol="",
        prep={},
        review={},
    )
    base.update(kwargs)
    return WorldState(**base)


def test_idle_requires_dismissed_when_ideas_present():
    """Structured field only — non-empty dismissed, no ticker-citation court."""
    world = _world(
        opportunities=[{"symbol": "QQQ", "source": "mda", "freshness": "delayed"}],
    )
    ok, reason, _ = validate_judgment(
        _idle_judgment(dismissed=""),
        world,
    )
    assert ok is False
    assert "dismissed" in reason.lower()

    ok2, _, j = validate_judgment(
        _idle_judgment(dismissed="QQQ chop — no clean pullback entry"),
        world,
    )
    assert ok2 is True
    assert j["stance"] == "idle"

    # Real reason without ticker citation is OK now (was process theater)
    ok3, reason3, _ = validate_judgment(
        _idle_judgment(dismissed="no edge in mega-caps today"),
        world,
    )
    assert ok3 is True, reason3


def test_idle_streak_is_soft_not_reject():
    """Repeated dismiss is prompt pressure + soft lesson, not a hard reject."""
    from abcxauto.world_state import save_idle_streak

    save_idle_streak(
        {
            "count": 3,
            "top_symbol": "QQQ",
            "last_dismiss": "QQQ chop — no clean pullback entry",
        }
    )
    world = _world(
        opportunities=[{"symbol": "QQQ", "source": "mda", "freshness": "delayed"}],
        idle_streak=3,
        idle_top_symbol="QQQ",
        effective_posture="aggressive",
    )
    assert idle_streak_threshold("aggressive") == 2
    ok, reason, j = validate_judgment(
        _idle_judgment(dismissed="QQQ chop — no clean pullback entry"),
        world,
    )
    assert ok is True, reason
    assert any("idle_streak" in s for s in (j.get("_soft_lessons") or []))


@pytest.mark.asyncio
async def test_hunt_quote_ignores_mda_tape():
    from abcxauto.agent_loop import _quote_for_action

    act = {
        "strategy": "market_bracket",
        "params": {"symbol": "NVDA", "price_hint": 999.0},
    }
    snap = {
        "opportunities": [
            {
                "symbol": "NVDA",
                "last": 100.0,
                "mda_last": 100.0,
                "source": "mda",
                "freshness": "delayed",
            }
        ],
    }
    # No connector → hunt must not fall back to MDA tape
    q = await _quote_for_action(act, snap, connector=None)
    assert q is None


def test_hunt_symbol_must_be_on_tape(monkeypatch):
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    world = _world(
        opportunities=[{"symbol": "AAPL", "source": "mda", "freshness": "delayed"}],
        effective_posture="aggressive",
    )
    j = {
        "stance": "hunt",
        "thesis": "Hunt single name.",
        "focus": "AAPL tape metrics.",
        "dismissed": "",
        "intent": {
            "kind": "hunt", "symbol": "ZZZZ", "direction": "LONG", "urgency": "med",
        },
        "risk_budget_pct": 1.0,
        "regime_fit": True,
        "setup_grade": "B",
    }
    ok, reason, _ = validate_judgment(j, world)
    assert ok is False
    assert "tape" in reason.lower()
    j["intent"]["symbol"] = "AAPL"
    ok2, _, _ = validate_judgment(j, world)
    assert ok2 is True


def test_intent_mismatch_blocks_hunt_symbol():
    judgment = {
        "stance": "hunt",
        "intent": {"kind": "hunt", "symbol": "QQQ", "direction": "LONG"},
    }
    ok, reason = check_intent_coherence(
        judgment,
        "market_bracket",
        {"params": {"symbol": "IWM", "direction": "LONG", "quantity": 1}},
    )
    assert ok is False
    assert "symbol" in reason.lower()


def test_idle_stance_allows_hold_only():
    judgment = {"stance": "idle", "intent": {"kind": "idle"}}
    ok, _ = check_intent_coherence(judgment, "hold", {})
    assert ok is True
    ok2, reason = check_intent_coherence(
        judgment,
        "market_bracket",
        {"params": {"symbol": "QQQ"}},
    )
    assert ok2 is False
    assert "idle" in reason.lower()


@pytest.mark.asyncio
async def test_intent_mismatch_end_to_end(monkeypatch):
    judgment = {
        "stance": "hunt",
        "thesis": "QQQ pullback continuation.",
        "focus": "Opportunity #1 QQQ",
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
            "symbol": "IWM",
            "direction": "LONG",
            "quantity": 1,
            "entry_price": 100,
            "stop_price": 99,
            "target_price": 102,
        },
        "rationale": "wrong symbol",
    }

    async def fake_scan(*_a, **_k):
        return [{"symbol": "QQQ", "bias": "LONG", "score": 0.9, "note": "up"}]

    monkeypatch.setattr("abcxauto.agent_loop.scan_opportunities", fake_scan)
    fake_grok, _ = _pja_grok(judgment, act)
    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)

    async def boom_send(*_a, **_k):
        raise AssertionError("must not send on intent mismatch")

    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert "intent_mismatch" in str(out["result"].get("note") or "").lower() or (
        "intent_mismatch" in str(out.get("rationale") or "").lower()
    )


def test_soft_hunt_cooldown_does_not_block_judgment(tmp_path, monkeypatch):
    """Recent entry is prompt pressure only — Judge may still hunt (or switch)."""
    from abcxauto.memory import reset_journal

    j = reset_journal(path=str(tmp_path / "j2.db"), enabled=True)
    j.record_decision(
        cycle=1,
        action="market_bracket",
        strategy="market_bracket",
        rationale="QQQ entry",
        outcome={"status": "ok", "symbol": "QQQ"},
    )
    world = _world(
        opportunities=[{"symbol": "QQQ", "bias": "LONG", "score": 0.9}],
        recent_decisions=j.recent_decisions(limit=3),
        flat=True,
        structure_cooldown={},
    )
    ok, reason, _ = validate_judgment(
        {
            "stance": "hunt",
            "thesis": "Re-enter QQQ",
            "focus": "QQQ #1",
            "dismissed": "",
            "intent": {"kind": "hunt", "symbol": "QQQ", "direction": "LONG"},
            "risk_budget_pct": 0.5,
            "regime_fit": True,
            "setup_grade": "A",
        },
        world,
    )
    assert ok is True, reason


def test_structure_scrape_cooldown_is_soft_lesson():
    world = _world(
        opportunities=[{"symbol": "QQQ", "bias": "LONG", "score": 0.9}],
        flat=True,
        structure_cooldown={"QQQ": "scrape_suspect"},
    )
    ok, reason, j = validate_judgment(
        {
            "stance": "hunt",
            "thesis": "Re-enter QQQ after scrape",
            "focus": "QQQ #1",
            "dismissed": "",
            "intent": {"kind": "hunt", "symbol": "QQQ", "direction": "LONG"},
            "risk_budget_pct": 0.5,
            "regime_fit": True,
            "setup_grade": "A",
        },
        world,
    )
    assert ok is True, reason
    lessons = j.get("_soft_lessons") or []
    assert any("structure_cooldown" in s for s in lessons)


def test_protect_forbids_idle_when_unprotected():
    world = _world(needs_protection=True, flat=False, unprotected=["AAPL"])
    ok, reason, _ = validate_judgment(_idle_judgment(), world)
    assert ok is False
    assert "protect" in reason.lower()


def test_should_skip_act_always_false(monkeypatch):
    """Thrift skip retired — model cost is not a cycle control."""
    from abcxauto.agent_loop import _should_skip_act
    from abcxauto.config import clear_runtime_overrides, get_config, update_controls_config

    clear_runtime_overrides()
    get_config.cache_clear()
    world = _world(flat=False, needs_protection=False)
    world.trade_plan = {"symbol": "SPY"}
    j = {
        "stance": "manage",
        "intent": {"kind": "manage", "symbol": "SPY"},
    }
    update_controls_config(control_deliberation_pct=40, persist=False)
    assert _should_skip_act(j, world, needs_prot=False) is False
    update_controls_config(control_deliberation_pct=80, persist=False)
    assert _should_skip_act(j, world, needs_prot=False) is False
    # idle also runs Act
    assert _should_skip_act(
        {"stance": "idle", "intent": {"kind": "idle"}},
        _world(flat=True, needs_protection=False),
        needs_prot=False,
    ) is False
    clear_runtime_overrides()
