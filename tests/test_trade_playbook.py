"""Trade-type playbook: filter, allowlist, share guard, prompt inclusion."""

from __future__ import annotations

from abcxauto.agent_loop import (
    STANCE_ACTIONS,
    _build_act_prompt,
    _build_judge_prompt,
    check_intent_coherence,
)
from abcxauto.trade_playbook import (
    OVERLAY_NO_LONG_STOCK,
    OVERLAY_SHARES_INSUFFICIENT,
    check_overlay_shares,
    format_trade_playbook,
    long_share_lots,
    world_hints_from_world,
)
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
        risk_posture="aggressive",
        effective_posture="aggressive",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
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


def test_playbook_hides_covered_call_without_long_lot():
    text = format_trade_playbook(
        "manage",
        {"flat": False, "long_lots": {"IWM": 22}, "has_trade_plan": True},
    )
    assert "TRADE PLAYBOOK" in text
    assert "Precondition:" in text or "Shell reject:" in text
    assert "covered_call" not in text
    assert "trailing_stop" in text or "modify_stop" in text


def test_playbook_shows_covered_call_with_100_shares():
    text = format_trade_playbook(
        "manage",
        {
            "flat": False,
            "long_lots": {"IWM": 122, "QQQ": 22},
            "has_trade_plan": True,
        },
    )
    assert "covered_call" in text
    assert "IWM" in text
    assert "on [IWM" in text
    assert "collar" in text
    assert "under-lot" in text and "QQQ" in text
    assert "harvest" not in text.lower()
    assert "mild bull" not in text.lower()


def test_playbook_hunt_hides_overlays():
    text = format_trade_playbook(
        "hunt",
        {"flat": True, "long_lots": {}, "has_trade_plan": False},
    )
    assert "market_bracket" in text or "bracket" in text
    assert "covered_call" not in text
    assert "vertical_spread" in text or "iron_condor" in text


def test_hunt_allowlist_accepts_vertical_rejects_overlay():
    ok, _ = check_intent_coherence(
        {"stance": "hunt", "intent": {"kind": "hunt", "symbol": "SPY"}},
        "vertical_spread",
        {"params": {"symbol": "SPY"}},
    )
    assert ok is True
    ok_ic, _ = check_intent_coherence(
        {"stance": "hunt", "intent": {"kind": "hunt", "symbol": "SPY"}},
        "iron_condor",
        {"params": {"symbol": "SPY"}},
    )
    assert ok_ic is True
    ok_roll, reason = check_intent_coherence(
        {"stance": "hunt", "intent": {"kind": "hunt", "symbol": "SPY"}},
        "roll_option",
        {"params": {"symbol": "SPY"}},
    )
    assert ok_roll is False
    assert "contradict" in reason.lower()
    assert "vertical_spread" in STANCE_ACTIONS["hunt"]
    assert "roll_option" in STANCE_ACTIONS["manage"]
    assert "roll_option" in STANCE_ACTIONS["protect"]
    assert "roll_option" not in STANCE_ACTIONS["hunt"]


def test_manage_allowlist_accepts_covered_call_hunt_rejects():
    ok, _ = check_intent_coherence(
        {"stance": "manage", "intent": {"kind": "manage"}},
        "covered_call",
        {"params": {"symbol": "IWM", "shares": 100}},
    )
    assert ok is True
    ok2, reason = check_intent_coherence(
        {"stance": "hunt", "intent": {"kind": "hunt", "symbol": "IWM"}},
        "covered_call",
        {"params": {"symbol": "IWM", "shares": 100}},
    )
    assert ok2 is False
    assert "contradict" in reason.lower()
    assert "covered_call" in STANCE_ACTIONS["manage"]
    assert "protective_put" in STANCE_ACTIONS["protect"]
    assert "covered_call" not in STANCE_ACTIONS["hunt"]


def test_share_lot_guard_rejects_without_stock():
    ok, code, msg = check_overlay_shares(
        "covered_call",
        {"symbol": "IWM", "shares": 100, "strike": 300, "expiration": "20260821"},
        [{"symbol": "IWM", "secType": "STK", "quantity": 22}],
    )
    assert ok is False
    assert code == OVERLAY_SHARES_INSUFFICIENT
    assert "22" in msg

    ok2, code2, _ = check_overlay_shares(
        "covered_call",
        {"symbol": "QQQ", "shares": 100},
        [{"symbol": "IWM", "secType": "STK", "quantity": 200}],
    )
    assert ok2 is False
    assert code2 == OVERLAY_NO_LONG_STOCK

    ok3, code3, _ = check_overlay_shares(
        "collar",
        {"symbol": "IWM", "shares": 100, "put_strike": 290, "call_strike": 310},
        [{"symbol": "IWM", "secType": "STK", "quantity": 100}],
    )
    assert ok3 is True
    assert code3 == "ok"


def test_long_share_lots_ignores_shorts():
    lots = long_share_lots(
        [
            {"symbol": "IWM", "secType": "STK", "quantity": 100},
            {"symbol": "QQQ", "secType": "STK", "quantity": -50},
        ]
    )
    assert lots == {"IWM": 100.0}


def test_act_prompt_includes_playbook_for_manage():
    world = _world(
        flat=False,
        positions=[{"symbol": "IWM", "secType": "STK", "quantity": 122, "conId": 1}],
        trade_plan={"symbol": "IWM", "direction": "LONG"},
    )
    prompt = _build_act_prompt(
        world,
        {
            "stance": "manage",
            "thesis": "manage IWM",
            "focus": "consider covered call",
            "intent": {"kind": "manage", "symbol": "IWM"},
        },
    )
    assert "TRADE PLAYBOOK" in prompt
    assert "ORDER EXAMPLES" in prompt
    assert "covered_call" in prompt
    assert "CONTROLS" in prompt


def test_judge_prompt_includes_playbook():
    world = _world(
        flat=False,
        positions=[{"symbol": "IWM", "secType": "STK", "quantity": 122}],
        trade_plan={"symbol": "IWM"},
    )
    prompt = _build_judge_prompt(world)
    assert "TRADE PLAYBOOK" in prompt
    assert "CONTROLS" in prompt

def test_strategy_diversity_observe_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    from abcxauto.memory import reset_journal

    j = reset_journal(path=str(tmp_path / "j.db"), enabled=True)
    j.record_decision(cycle=1, action="market_bracket", strategy="market_bracket", rationale="a")
    j.record_decision(cycle=2, action="hold", strategy="hold", rationale="b")
    j.record_decision(cycle=3, action="covered_call", strategy="covered_call", rationale="c")
    j.record_decision(cycle=4, action="blocked", strategy="blocked", rationale="d")
    div = j.strategy_diversity(limit=40)
    assert div["n_distinct"] == 2
    assert set(div["strategies"]) == {"market_bracket", "covered_call"}
    assert div["n_decisions"] == 4


def test_world_hints_from_world():
    world = _world(
        flat=False,
        positions=[{"symbol": "IWM", "secType": "STK", "quantity": 100}],
        trade_plan={"symbol": "IWM"},
    )
    hints = world_hints_from_world(world)
    assert hints["long_lots"]["IWM"] == 100.0
    assert hints["has_trade_plan"] is True
