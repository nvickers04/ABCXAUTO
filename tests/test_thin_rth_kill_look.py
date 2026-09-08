"""pcs-skew Arm v0 kill-window LOOK contract — tests for thin-prompt spec §4.

Look budget, STAY allowlist, mill/turns, dual-mode, F10 $, hygiene.
Does not start looking, TWS, or 7496.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import EMPTY_GROK_TRIES, agent_tools
from abcxauto.config import Config, get_config
from abcxauto.desk_mode import (
    SYNTHESIZE_MILL_TRIES,
    research_keep_looking,
    session_model,
    spoken_synthesize_mill,
    write_research_brief,
)
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.session_caps import (
    consume_open_entry_look,
    f10_loop_halted,
    kill_entry_looks,
    mark_f10_loop_halt,
    note_research_look,
    research_week_looks,
    reset_session_caps,
    usage,
)
from abcxauto.thin_rth_kill_look import (
    DIE_TOOLS,
    EST_THIS_LOOK_USD,
    F10_HARD_USD,
    F10_PREFERRED_USD,
    MODE_ABORT,
    MODE_MANAGE,
    MODE_OPEN,
    MODEL_TURNS_MAX,
    PCS_CARD,
    REASON_ALLOWLIST,
    REASON_NAMELESS,
    REASON_DIE_TOOL,
    REASON_ENTRY_BUDGET,
    REASON_F10,
    REASON_MODEL_COST,
    REASON_ONE_SEND,
    REASON_PORT,
    REASON_RESEARCH_PROMPT,
    REASON_RESEARCH_WEEK,
    REASON_TOOLS,
    REASON_TURNS,
    RESEARCH_PROMPT_TOKENS_MAX,
    STAY_TOOLS,
    TOOLS_ENTRY_MAX,
    TOOLS_MANAGE_MAX,
    WINDOW_MODEL_USD,
    WINDOW_N,
    die_tool_block,
    f10_gate,
    f10_hard_tripped,
    f10_open_look_halted,
    is_f10_look_halt,
    mark_f10_hard_trip,
    record_f10_loop_halt,
    force_skip_or_manage,
    has_open_pcs_skew_lot,
    one_open_send_block,
    kill_look_enabled,
    kill_look_port_ok,
    kill_look_rth,
    kill_look_send_block,
    kill_mode,
    look_gather_spin_mill,
    look_kill_mill,
    max_model_turns,
    max_tools,
    pcs_send_ok,
    research_prompt_ok,
    rth_model_no_xhigh,
    skip_look_reason,
    spoken_gather_spin,
    tool_allowed,
    turns_or_tools_breached,
)
from tests.test_brain_tools import _world
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

PCS_OPEN = {
    "symbol": "SPY",
    "expiration": "20260918",
    "long_strike": 500.0,
    "short_strike": 505.0,
    "right": "P",
    "quantity": 1,
    "limit_price": 0.85,
    "card": PCS_CARD,
}


def _kill_on(monkeypatch) -> None:
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")


def _tool_names(session: str) -> set[str]:
    names: set[str] = set()
    for t in agent_tools(session=session):
        fn = getattr(t, "function", None)
        names.add(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    return names


def _pcs_lot() -> dict:
    return {
        "symbol": "SPY",
        "quantity": -1,
        "secType": "BAG",
        "right": "P",
        "card": PCS_CARD,
        "strategy": "vertical_spread",
    }


def _allow_f10() -> dict:
    return {
        "allow_new_risk": True,
        "preferred_trip": False,
        "reason_code": "",
        "note": "",
        "projected": EST_THIS_LOOK_USD,
    }


def test_hygiene_prompt_port_empty_grok_mill_tries():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert get_config().ibkr_port != 7496
    assert kill_look_port_ok() is True
    assert EMPTY_GROK_TRIES == 2
    assert SYNTHESIZE_MILL_TRIES == 2
    assert Config().pcs_kill_look is True
    assert F10_HARD_USD == 2.0
    assert F10_PREFERRED_USD == 1.0
    assert WINDOW_MODEL_USD == 40.0
    assert WINDOW_N == 20


def test_kill_look_env_gate(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "0")
    assert kill_look_enabled() is False
    assert kill_look_rth("regular") is False
    _kill_on(monkeypatch)
    assert kill_look_enabled() is True
    assert kill_look_rth("regular") is True
    assert kill_look_rth("premarket") is False


def test_look_budget_open_one_then_abort(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    f10 = _allow_f10()
    assert (
        kill_mode("regular", positions=[], open_lots=[], entry_looks=0, f10=f10)
        == MODE_OPEN
    )
    assert skip_look_reason("regular", positions=[], f10=_allow_f10()) == ""
    consume_open_entry_look("regular")
    assert kill_entry_looks("regular") == 1
    assert (
        kill_mode("regular", positions=[], open_lots=[], f10=f10) == MODE_ABORT
    )
    assert skip_look_reason("regular", positions=[], same_look=False) == REASON_ENTRY_BUDGET
    # Mill re-enter of look #1 still runs.
    assert skip_look_reason("regular", positions=[], same_look=True) == ""
    consume_open_entry_look("regular")
    assert kill_entry_looks("regular") == 1


def test_start_bypass_spent_entry_budget_once(monkeypatch):
    """Operator Start one-shot bypasses spent entry; later pulses do not reset it."""
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    assert kill_entry_looks("regular") == 1
    assert (
        skip_look_reason(
            "regular",
            positions=[],
            f10=_allow_f10(),
            bypass_entry_budget=True,
        )
        == ""
    )
    assert kill_entry_looks("regular") == 1
    assert skip_look_reason(
        "regular", positions=[], f10=_allow_f10(), bypass_entry_budget=False
    ) == REASON_ENTRY_BUDGET


def test_start_bypass_does_not_soften_f10(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    assert (
        skip_look_reason(
            "regular",
            positions=[],
            f10=hard,
            bypass_entry_budget=True,
        )
        == REASON_F10
    )


def test_start_bypass_does_not_soften_7496(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: SimpleNamespace(ibkr_port=7496, pcs_kill_look=True),
    )
    assert kill_look_port_ok() is False
    assert (
        skip_look_reason(
            "regular",
            positions=[],
            f10=_allow_f10(),
            bypass_entry_budget=True,
        )
        == REASON_ENTRY_BUDGET
    )


def test_in_flight_open_can_still_send_after_consume(monkeypatch):
    """Consume-before-think must not abort the legal look-#1 SEND."""
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    f10 = _allow_f10()
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    assert kill_mode("regular", positions=[], f10=f10) == MODE_ABORT
    assert (
        kill_mode("regular", positions=[], f10=f10, in_flight=True) == MODE_OPEN
    )
    blocked = kill_look_send_block(act, session="regular", f10=f10)
    assert blocked is not None
    assert kill_look_send_block(act, session="regular", f10=f10, in_flight=True) is None
    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    assert skip_look_reason("regular", positions=[], same_look=True, f10=hard) == (
        REASON_F10
    )


def test_look_budget_manage_replaces_second_entry(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    lot = _pcs_lot()
    assert has_open_pcs_skew_lot([lot], ["pcs-skew SPY vert"]) is True
    assert (
        kill_mode(
            "regular",
            positions=[lot],
            open_lots=["pcs-skew SPY vert"],
            f10=_allow_f10(),
        )
        == MODE_MANAGE
    )
    assert (
        skip_look_reason(
            "regular",
            positions=[lot],
            open_lots=["pcs-skew SPY vert"],
        )
        == ""
    )
    new_risk = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    blocked = kill_look_send_block(
        new_risk,
        session="regular",
        positions=[lot],
        open_lots=["pcs-skew SPY vert"],
        f10=_allow_f10(),
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_ALLOWLIST
    close = {
        "strategy": "vertical_spread",
        "params": {**PCS_OPEN, "closing_position": True},
        "card": PCS_CARD,
    }
    assert (
        kill_look_send_block(
            close,
            session="regular",
            positions=[lot],
            open_lots=["pcs-skew SPY vert"],
            f10=_allow_f10(),
        )
        is None
    )


def test_allowlist_stay_only_on_rth_kill_look(monkeypatch):
    _kill_on(monkeypatch)
    rth = _tool_names("regular")
    assert STAY_TOOLS <= rth
    assert "send" in rth
    for dead in DIE_TOOLS:
        assert dead not in rth, dead
    enum = []
    for t in agent_tools(session="regular"):
        fn = getattr(t, "function", None)
        if str(getattr(fn, "name", None) or "") != "send":
            continue
        params = getattr(fn, "parameters", None) or {}
        if isinstance(params, str):
            params = json.loads(params)
        enum = list(((params.get("properties") or {}).get("strategy") or {}).get("enum") or [])
    assert enum == ["vertical_spread"]
    research = _tool_names("premarket")
    assert "send" not in research
    assert "web" in research
    assert "news" in research
    assert "scan" in research


def test_allowlist_tool_gate_and_die_block(monkeypatch):
    _kill_on(monkeypatch)
    assert tool_allowed("book", session="regular") is True
    assert tool_allowed("send", session="regular") is True
    assert tool_allowed("scan", session="regular") is False
    assert tool_allowed("web", session="regular") is False
    assert tool_allowed("self_tune", session="regular") is False
    assert die_tool_block("scan", session="regular")["reason_code"] == REASON_DIE_TOOL
    assert die_tool_block("book", session="regular") is None
    assert die_tool_block("scan", session="premarket") is None
    ok, why = pcs_send_ok("vertical_spread", PCS_OPEN, PCS_CARD, mode=MODE_OPEN)
    assert ok is True
    ok, why = pcs_send_ok("market_bracket", {"symbol": "SPY", "card": "x"}, "x", mode=MODE_OPEN)
    assert ok is False
    assert why == REASON_ALLOWLIST
    ok, why = pcs_send_ok(
        "vertical_spread",
        {**PCS_OPEN, "closing_position": True},
        PCS_CARD,
        mode=MODE_ABORT,
    )
    assert ok is True
    ok, why = pcs_send_ok("vertical_spread", PCS_OPEN, PCS_CARD, mode=MODE_MANAGE)
    assert ok is False
    assert why == REASON_ALLOWLIST


@pytest.mark.asyncio
async def test_die_tools_rejected_in_run_tool(monkeypatch):
    _kill_on(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool

    world = _world(session_status="regular", flat=True)
    raw = await _run_tool(
        "scan",
        {},
        connector=None,
        world=world,
        snap={},
        turn=BrainTurn(),
    )
    data = json.loads(raw)
    assert data.get("reason_code") == REASON_DIE_TOOL
    web = await _run_tool(
        "web",
        {"url": "https://example.com"},
        connector=None,
        world=world,
        snap={},
        turn=BrainTurn(),
    )
    assert json.loads(web).get("reason_code") == REASON_DIE_TOOL


def test_mill_gather_spin_and_kill_mill(monkeypatch):
    _kill_on(monkeypatch)
    assert spoken_gather_spin("Let me gather more tape color.")
    assert spoken_synthesize_mill("Let me gather the tape.", sends=0)
    assert look_gather_spin_mill(
        {"rationale": "gathering", "sends": 0, "tool_trace": ["scan"]}
    )
    assert look_kill_mill(
        {"rationale": "Let me synthesize the picture.", "sends": 0, "tool_trace": []},
        session="regular",
    )
    assert not look_kill_mill(
        {"rationale": "Let me synthesize the picture.", "sends": 0, "tool_trace": ["book"]},
        session="regular",
    )
    assert SYNTHESIZE_MILL_TRIES == 2


def test_turns_and_tool_caps_force_skip_or_manage():
    assert max_model_turns(MODE_OPEN) == MODEL_TURNS_MAX == 4
    assert max_tools(MODE_OPEN) == TOOLS_ENTRY_MAX == 6
    assert max_tools(MODE_MANAGE) == TOOLS_MANAGE_MAX == 4
    assert turns_or_tools_breached(MODE_OPEN, model_turns=4, tool_count=6) == ""
    assert turns_or_tools_breached(MODE_OPEN, model_turns=5, tool_count=1) == REASON_TURNS
    assert turns_or_tools_breached(MODE_OPEN, model_turns=1, tool_count=7) == REASON_TOOLS
    assert turns_or_tools_breached(MODE_MANAGE, model_turns=1, tool_count=5) == REASON_TOOLS
    skip = force_skip_or_manage(
        MODE_OPEN, strategy="vertical_spread", params=dict(PCS_OPEN), breached=REASON_TOOLS
    )
    assert skip is not None
    assert skip["strategy"] == "skipped"
    assert skip["reason_code"] == REASON_TOOLS
    close = force_skip_or_manage(
        MODE_MANAGE,
        strategy="vertical_spread",
        params={**PCS_OPEN, "closing_position": True},
        breached=REASON_TOOLS,
    )
    assert close is None
    hold_new = force_skip_or_manage(
        MODE_MANAGE, strategy="vertical_spread", params=dict(PCS_OPEN), breached=REASON_TURNS
    )
    assert hold_new is not None
    assert "MANAGE-only" in str(hold_new.get("note") or "")


def test_kill_look_send_allows_legal_pcs_and_blocks_live_port(monkeypatch):
    _kill_on(monkeypatch)
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    assert kill_look_send_block(act, session="regular", f10=_allow_f10()) is None
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_port_ok", lambda cfg=None: False
    )
    blocked = kill_look_send_block(act, session="regular", f10=_allow_f10())
    assert blocked is not None
    assert blocked["reason_code"] == REASON_PORT


@pytest.mark.asyncio
async def test_run_tool_send_force_skip_on_tool_cap(monkeypatch):
    _kill_on(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool

    world = _world(session_status="regular", flat=True)
    turn = BrainTurn()
    turn.kill_mode = MODE_OPEN
    turn.tool_trace = ["book", "status", "quote", "option_chain", "option_quote", "fills", "send"]
    raw = await _run_tool(
        "send",
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        connector=None,
        world=world,
        snap={"positions": []},
        turn=turn,
    )
    data = json.loads(raw)
    assert data.get("reason_code") == REASON_TOOLS
    assert data.get("strategy") == "skipped"


@pytest.mark.asyncio
async def test_open_one_send_then_stop(monkeypatch):
    _kill_on(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool

    world = _world(session_status="regular", flat=True)
    turn = BrainTurn()
    turn.kill_mode = MODE_OPEN
    turn.sends = [
        {
            "act": {"strategy": "vertical_spread"},
            "result": {"status": "submitted"},
            "strat": "vertical_spread",
        }
    ]
    raw = await _run_tool(
        "send",
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        connector=None,
        world=world,
        snap={"positions": [], "kill_entry_in_flight": True},
        turn=turn,
    )
    data = json.loads(raw)
    assert data.get("reason_code") == REASON_ONE_SEND
    assert data.get("strategy") == "skipped"
    assert one_open_send_block(MODE_OPEN, turn) is not None
    assert one_open_send_block(MODE_MANAGE, turn) is None


def test_dual_mode_rth_strips_xhigh_ah_one_shot_and_week_cap(monkeypatch):
    _kill_on(monkeypatch)
    assert rth_model_no_xhigh("grok-4.6-xhigh") == "grok-4.6"
    assert rth_model_no_xhigh("grok-4.6-xhigh-fast") == "grok-4.6-fast"
    cfg = SimpleNamespace(model="grok-4.6-xhigh", model_rth="", model_research="grok-4.6-xhigh")
    assert session_model("regular", cfg) == "grok-4.6"
    assert session_model("premarket", cfg) == "grok-4.6-xhigh"
    assert research_keep_looking("premarket") is False
    assert research_keep_looking("regular") is False
    reset_session_caps()
    assert research_week_looks() == 0
    assert skip_look_reason("premarket", prompt_tokens=RESEARCH_PROMPT_TOKENS_MAX) == (
        REASON_RESEARCH_PROMPT
    )
    note_research_look()
    note_research_look()
    assert research_week_looks() == 2
    assert skip_look_reason("premarket") == REASON_RESEARCH_WEEK
    assert research_prompt_ok(199_999) is True
    assert research_prompt_ok(RESEARCH_PROMPT_TOKENS_MAX) is False


def test_write_research_brief_still_skips_rth(tmp_path, monkeypatch):
    _kill_on(monkeypatch)
    path = tmp_path / "research_brief.json"
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(path))
    out = write_research_brief(session="regular", snap={"news_items": []})
    assert out == {}
    assert not path.is_file()


def test_f10_hard_preferred_unreadable_window_exits(monkeypatch):
    _kill_on(monkeypatch)
    gate = f10_gate(0.0, est_this_look=EST_THIS_LOOK_USD, window_cost=0.0)
    assert gate["allow_new_risk"] is True
    assert gate["preferred_trip"] is False
    pref = f10_gate(0.80, est_this_look=0.35, window_cost=0.0)
    assert pref["allow_new_risk"] is True
    assert pref["preferred_trip"] is True
    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    assert hard["allow_new_risk"] is False
    assert hard["reason_code"] == REASON_F10
    unread = f10_gate(None, est_this_look=0.35, window_cost=0.0)
    assert unread["allow_new_risk"] is False
    assert unread["reason_code"] == REASON_MODEL_COST
    win = f10_gate(0.0, est_this_look=0.35, window_cost=40.0)
    assert win["allow_new_risk"] is False
    assert win["reason_code"] == REASON_MODEL_COST
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    blocked = kill_look_send_block(
        act,
        session="regular",
        f10=hard,
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_F10
    close = {
        "strategy": "vertical_spread",
        "params": {**PCS_OPEN, "closing_position": True},
        "card": PCS_CARD,
    }
    assert (
        kill_look_send_block(close, session="regular", f10=hard) is None
    )


@pytest.mark.asyncio
async def test_execute_ticket_kill_look_blocks_non_pcs(monkeypatch):
    _kill_on(monkeypatch)
    from abcxauto.agent_loop import execute_ticket

    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate",
        lambda: _allow_f10(),
    )
    world = _world(session_status="regular", flat=True)
    result = await execute_ticket(
        {
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "quantity": 1,
                "direction": "LONG",
                "stop_price": 1.0,
                "target_price": 2.0,
                "card": "other",
            },
            "card": "other",
        },
        object(),
        world,
        {"positions": []},
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == REASON_ALLOWLIST


@pytest.mark.asyncio
async def test_execute_ticket_in_flight_does_not_abort_legal_pcs(monkeypatch):
    """Look #1 consume must not F10-abort the legal BAG. Later clerk gates may still block."""
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    from abcxauto.agent_loop import execute_ticket

    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate",
        lambda: _allow_f10(),
    )
    world = _world(session_status="regular", flat=True)
    ticket = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    aborted = await execute_ticket(ticket, object(), world, {"positions": []})
    assert aborted.get("status") == "blocked"
    assert aborted.get("reason_code") == REASON_F10
    inflight = await execute_ticket(
        ticket,
        object(),
        world,
        {"positions": [], "kill_entry_in_flight": True},
    )
    assert inflight.get("reason_code") not in {REASON_F10, REASON_ALLOWLIST, REASON_ENTRY_BUDGET}
    close = await execute_ticket(
        {
            "strategy": "vertical_spread",
            "params": {**PCS_OPEN, "closing_position": True},
            "card": PCS_CARD,
        },
        object(),
        world,
        {"positions": []},
    )
    assert close.get("reason_code") not in {REASON_F10, REASON_ALLOWLIST}


def test_pro_engine_skip_reason_entry_budget(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    consume_open_entry_look("regular")
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    why = eng._kill_look_skip_reason("regular", {"positions": [], "protection": {}})
    assert why == REASON_ENTRY_BUDGET
    eng._mill_wake = True
    assert eng._kill_look_skip_reason("regular", {"positions": []}) == ""
    eng2 = ProEngine()
    eng2._kill_entry_in_flight = True
    assert eng2._kill_look_skip_reason("regular", {"positions": []}) == ""
    start_eng = ProEngine()
    start_eng._force_first_look = True
    assert start_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == ""
    start_eng._force_first_look = False
    assert start_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == REASON_ENTRY_BUDGET
    assert kill_entry_looks("regular") == 1


def test_f10_trip_halts_open_look_exits_still_ok(monkeypatch):
    """KEEP-1: hard F10 latches; OPEN looks skip; MANAGE / close still go."""
    from abcxauto.pro_engine import ProEngine

    _kill_on(monkeypatch)
    reset_session_caps()
    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    assert hard["allow_new_risk"] is False
    assert hard["reason_code"] == REASON_F10
    assert f10_hard_tripped(hard) is True
    assert f10_open_look_halted(hard) is True
    assert skip_look_reason("regular", positions=[], f10=hard) == REASON_F10
    assert f10_loop_halted() is True
    assert usage("regular")["f10_tripped"] is True
    assert usage("regular")["loop_halted"] is True
    assert skip_look_reason("regular", positions=[], f10=_allow_f10()) == REASON_F10
    assert skip_look_reason(
        "regular", positions=[], same_look=True, f10=_allow_f10()
    ) == REASON_F10
    mill_eng = ProEngine()
    mill_eng._mill_wake = True
    assert mill_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == REASON_F10
    unpaid_eng = ProEngine()
    unpaid_eng._ticket_wake = True
    assert unpaid_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == REASON_F10
    recover_eng = ProEngine()
    recover_eng._recover_same_chat = True
    assert recover_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == REASON_F10
    lot = _pcs_lot()
    assert (
        skip_look_reason(
            "regular",
            positions=[lot],
            open_lots=["pcs-skew SPY vert"],
            f10=hard,
        )
        == ""
    )
    close = {
        "strategy": "vertical_spread",
        "params": {**PCS_OPEN, "closing_position": True},
        "card": PCS_CARD,
    }
    assert (
        kill_look_send_block(
            close,
            session="regular",
            positions=[lot],
            open_lots=["pcs-skew SPY vert"],
            f10=hard,
        )
        is None
    )
    blocked = kill_look_send_block(
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        session="regular",
        f10=hard,
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_F10
    assert skip_look_reason("premarket", f10=hard) == REASON_F10
    assert skip_look_reason("regular", positions=[], f10=hard, unprotected=True) == ""


def test_f10_preferred_does_not_halt_loop(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    pref = f10_gate(0.80, est_this_look=0.35, window_cost=0.0)
    assert pref["allow_new_risk"] is True
    assert pref["preferred_trip"] is True
    assert f10_hard_tripped(pref) is False
    assert f10_open_look_halted(pref) is False
    assert skip_look_reason("regular", positions=[], f10=pref) == ""
    assert f10_loop_halted() is False
    assert mark_f10_hard_trip(pref) is False


def test_record_f10_loop_halt_last_turn_and_scorecard(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    out = record_f10_loop_halt(session="regular")
    assert out["f10_tripped"] is True
    assert out["loop_halted"] is True
    assert out["skip_reason"] == REASON_F10
    assert f10_loop_halted() is True
    from abcxauto.think_stream import LAST_TURN_PATH

    last = json.loads(LAST_TURN_PATH.read_text(encoding="utf-8"))
    assert last["f10_tripped"] is True
    assert last["loop_halted"] is True
    assert last["skip_reason"] == REASON_F10
    assert float(last.get("model_cost_post_trip_USD") or 0) == 0.0
    assert out["model_cost_post_trip_USD"] == 0.0
    from abcxauto.pcs_kill_scorecard import normalize_session_row

    row = normalize_session_row(
        {
            "session_id": "pcs-v0-20260907-01",
            "session_date": "2026-09-07",
            "NL": 100_000.0,
            "conservative_pnl": 0.0,
            "model_cost": 2.1,
            "lambda": 0.0,
            "credit": 0.0,
            "max_loss": 0.0,
            "send": "NO_SEND",
            "send_reason": "NO_SEND:f10",
            "qty": 0,
            "f10_ok": False,
            "dd_pct": 0.0,
            "fill_mark": "ibkr_fill",
            "session_start_NL": 100_000.0,
            "ba_commission_USD": 0.0,
            "f10_tripped": True,
            "loop_halted": True,
            "model_cost_post_trip_USD": 0,
        }
    )
    assert row["f10_tripped"] is True
    assert row["loop_halted"] is True
    assert row.get("model_cost_post_trip_USD") == 0
    assert row["f10_ok"] is False


@pytest.mark.asyncio
async def test_projected_hard_cross_skips_grok_turn_billing(monkeypatch):
    """SPEC A: next billed look that would cross $2 must not call the model."""
    _kill_on(monkeypatch)
    reset_session_caps()
    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    assert hard["projected"] > F10_HARD_USD
    monkeypatch.setattr("abcxauto.thin_rth_kill_look.live_f10_gate", lambda: hard)
    from abcxauto.brain import grok_turn

    calls = {"n": 0}

    async def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("stream_round must not run when next look would cross $2")

    monkeypatch.setattr("abcxauto.brain.stream_round", boom)
    g = SimpleNamespace(chat=None, model="grok-4.6")
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={"positions": [], "protection": {}},
        wake="look",
    )
    assert calls["n"] == 0
    assert turn.loop_halted is True
    assert turn.f10_tripped is True
    assert f10_loop_halted() is True
    assert usage("regular")["model_cost_post_trip_usd"] == 0.0


@pytest.mark.asyncio
async def test_f10_halt_skips_grok_turn_billing(monkeypatch):
    """Already-tripped F10 must not open a billed chat."""
    _kill_on(monkeypatch)
    reset_session_caps()
    mark_f10_loop_halt()
    from abcxauto.brain import grok_turn

    calls = {"n": 0}

    async def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("stream_round must not run after F10 loop halt")

    monkeypatch.setattr("abcxauto.brain.stream_round", boom)
    g = SimpleNamespace(chat=None, model="grok-4.6")
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={"positions": [], "protection": {}},
        wake="look",
    )
    assert calls["n"] == 0
    assert turn.loop_halted is True
    assert turn.f10_tripped is True
    assert (turn.last_result or {}).get("reason_code") == REASON_F10


@pytest.mark.asyncio
async def test_execute_ticket_f10_blocks_new_risk_allows_close(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    from abcxauto.agent_loop import execute_ticket

    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate",
        lambda: hard,
    )
    world = _world(session_status="regular", flat=True)
    blocked = await execute_ticket(
        {
            "strategy": "vertical_spread",
            "params": dict(PCS_OPEN),
            "card": PCS_CARD,
        },
        object(),
        world,
        {"positions": []},
    )
    assert blocked.get("status") == "blocked"
    assert blocked.get("reason_code") == REASON_F10
    assert f10_loop_halted() is True
    assert skip_look_reason("regular", positions=[], f10=hard) == REASON_F10
    close = await execute_ticket(
        {
            "strategy": "vertical_spread",
            "params": {**PCS_OPEN, "closing_position": True},
            "card": PCS_CARD,
        },
        object(),
        world,
        {"positions": []},
    )
    assert close.get("reason_code") not in {REASON_F10, REASON_ALLOWLIST}

def test_f10_unreadable_fail_closes_loop(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    unread = f10_gate(None, est_this_look=0.35, window_cost=0.0)
    assert unread["reason_code"] == REASON_MODEL_COST
    assert f10_open_look_halted(unread) is True
    assert skip_look_reason("regular", positions=[], f10=unread) == REASON_MODEL_COST
    assert f10_loop_halted() is True
    assert usage("regular")["model_cost_post_trip_usd"] == 0.0
    assert skip_look_reason("premarket", f10=unread) == REASON_MODEL_COST


def test_f10_nonfinite_fail_closes_loop(monkeypatch):
    """SPEC D: NaN / inf model_cost fail-closes and sticky-latches."""
    _kill_on(monkeypatch)
    reset_session_caps()
    nan = f10_gate(float("nan"), est_this_look=0.35, window_cost=0.0)
    assert nan["allow_new_risk"] is False
    assert nan["reason_code"] == REASON_MODEL_COST
    inf = f10_gate(float("inf"), est_this_look=0.35, window_cost=0.0)
    assert inf["allow_new_risk"] is False
    assert inf["reason_code"] == REASON_MODEL_COST
    win_inf = f10_gate(0.0, est_this_look=0.35, window_cost=float("inf"))
    assert win_inf["allow_new_risk"] is False
    assert win_inf["reason_code"] == REASON_MODEL_COST
    assert skip_look_reason("regular", positions=[], f10=inf) == REASON_MODEL_COST
    assert f10_loop_halted() is True
    assert usage("regular")["model_cost_post_trip_usd"] == 0.0
    blocked = kill_look_send_block(
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        session="regular",
        f10=inf,
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_MODEL_COST
    assert (
        kill_look_send_block(
            {
                "strategy": "vertical_spread",
                "params": {**PCS_OPEN, "closing_position": True},
                "card": PCS_CARD,
            },
            session="regular",
            f10=inf,
        )
        is None
    )


def test_is_f10_look_halt_covers_hard_and_unreadable():
    assert is_f10_look_halt(REASON_F10) is True
    assert is_f10_look_halt(REASON_MODEL_COST) is True
    assert is_f10_look_halt(REASON_ENTRY_BUDGET) is False
    assert is_f10_look_halt("") is False


@pytest.mark.asyncio
async def test_unreadable_model_cost_skips_grok_turn_billing(monkeypatch):
    """SPEC D: unreadable cost must not open a billed new-risk chat."""
    _kill_on(monkeypatch)
    reset_session_caps()
    unread = f10_gate(None, est_this_look=0.35, window_cost=0.0)
    monkeypatch.setattr("abcxauto.thin_rth_kill_look.live_f10_gate", lambda: unread)
    from abcxauto.brain import grok_turn

    calls = {"n": 0}

    async def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("stream_round must not run after unreadable F10 halt")

    monkeypatch.setattr("abcxauto.brain.stream_round", boom)
    g = SimpleNamespace(chat=None, model="grok-4.6")
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={"positions": [], "protection": {}},
        wake="look",
    )
    assert calls["n"] == 0
    assert turn.loop_halted is True
    assert turn.f10_tripped is True
    assert f10_loop_halted() is True
    assert usage("regular")["model_cost_post_trip_usd"] == 0.0


@pytest.mark.asyncio
async def test_execute_ticket_unreadable_latches_exit_ok(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    from abcxauto.agent_loop import execute_ticket

    unread = f10_gate(None, est_this_look=0.35, window_cost=0.0)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate",
        lambda: unread,
    )
    world = _world(session_status="regular", flat=True)
    blocked = await execute_ticket(
        {
            "strategy": "vertical_spread",
            "params": dict(PCS_OPEN),
            "card": PCS_CARD,
        },
        object(),
        world,
        {"positions": []},
    )
    assert blocked.get("status") == "blocked"
    assert blocked.get("reason_code") == REASON_MODEL_COST
    assert f10_loop_halted() is True
    close = await execute_ticket(
        {
            "strategy": "vertical_spread",
            "params": {**PCS_OPEN, "closing_position": True},
            "card": PCS_CARD,
        },
        object(),
        world,
        {"positions": []},
    )
    assert close.get("reason_code") not in {REASON_F10, REASON_ALLOWLIST, REASON_MODEL_COST}


def test_hygiene_port_not_live_7496():
    assert get_config().ibkr_port != 7496


NAMED_VERT = {
    "symbol": "SPY",
    "expiration": "20260918",
    "long_strike": 745.0,
    "short_strike": 750.0,
    "right": "P",
    "quantity": 1,
    "limit_price": 0.85,
    "card": "spy-bp-750-745",
}


def test_named_card_allowlist_and_no_credit_floor(monkeypatch):
    """Named cards unlock; empty card refuses; C<$1 is not a credit-floor refuse."""
    _kill_on(monkeypatch)
    f10 = _allow_f10()

    empty = {k: v for k, v in PCS_OPEN.items() if k != "card"}
    ok, why = pcs_send_ok("vertical_spread", empty, None, mode=MODE_OPEN)
    assert ok is False
    assert why in {REASON_NAMELESS, REASON_ALLOWLIST}
    ok, why = pcs_send_ok(
        "vertical_spread", {**PCS_OPEN, "card": ""}, "", mode=MODE_OPEN
    )
    assert ok is False
    assert why in {REASON_NAMELESS, REASON_ALLOWLIST}
    ok, why = pcs_send_ok(
        "vertical_spread", {**PCS_OPEN, "card": "   "}, "   ", mode=MODE_OPEN
    )
    assert ok is False
    assert why in {REASON_NAMELESS, REASON_ALLOWLIST}
    blocked_empty = kill_look_send_block(
        {"strategy": "vertical_spread", "params": empty},
        session="regular",
        f10=f10,
    )
    assert blocked_empty is not None
    assert blocked_empty["reason_code"] in {REASON_NAMELESS, REASON_ALLOWLIST}

    ok, why = pcs_send_ok(
        "vertical_spread", NAMED_VERT, "spy-bp-750-745", mode=MODE_OPEN
    )
    assert ok is True
    named_act = {
        "strategy": "vertical_spread",
        "params": dict(NAMED_VERT),
        "card": "spy-bp-750-745",
    }
    assert kill_look_send_block(named_act, session="regular", f10=f10) is None
    ok, why = pcs_send_ok("vertical_spread", PCS_OPEN, PCS_CARD, mode=MODE_OPEN)
    assert ok is True

    incomplete = {
        "symbol": "SPY",
        "right": "P",
        "quantity": 1,
        "card": "spy-bp-750-745",
    }
    ok, why = pcs_send_ok(
        "vertical_spread", incomplete, "spy-bp-750-745", mode=MODE_OPEN
    )
    assert ok is False
    assert why == REASON_ALLOWLIST
    missing_strikes = kill_look_send_block(
        {
            "strategy": "vertical_spread",
            "params": incomplete,
            "card": "spy-bp-750-745",
        },
        session="regular",
        f10=f10,
    )
    assert missing_strikes is not None
    assert missing_strikes["reason_code"] == REASON_ALLOWLIST

    assert F10_HARD_USD == 2.0
    assert get_config().ibkr_port != 7496
    assert Config().ibkr_port != 7496

    close = {**NAMED_VERT, "closing_position": True}
    ok, why = pcs_send_ok(
        "vertical_spread", close, "spy-bp-750-745", mode=MODE_ABORT
    )
    assert ok is True
    assert why == "closing"
    hard = f10_gate(1.80, est_this_look=0.35, window_cost=0.0)
    assert (
        kill_look_send_block(
            {
                "strategy": "vertical_spread",
                "params": close,
                "card": "spy-bp-750-745",
            },
            session="regular",
            f10=hard,
        )
        is None
    )

    cheap = {**PCS_OPEN, "limit_price": 0.50}
    ok, why = pcs_send_ok("vertical_spread", cheap, PCS_CARD, mode=MODE_OPEN)
    assert ok is True
    assert why != "NO_SEND:credit_lt_floor"
    cheap_block = kill_look_send_block(
        {"strategy": "vertical_spread", "params": cheap, "card": PCS_CARD},
        session="regular",
        f10=f10,
    )
    assert cheap_block is None
    ok, why = pcs_send_ok(
        "ratio_spread",
        {
            "symbol": "SPY",
            "expiration": "20260918",
            "long_strike": 500.0,
            "short_strike": 510.0,
            "right": "C",
            "ratio": 2,
            "quantity": 1,
            "card": "named-ratio",
        },
        "named-ratio",
        mode=MODE_OPEN,
    )
    assert ok is False
    assert why == REASON_ALLOWLIST

    from abcxauto import thin_rth_kill_look as kl

    src = inspect.getsource(kl)
    assert "CREDIT_FLOOR" not in src
    assert "min_credit" not in src
    assert "credit_lt_floor" not in inspect.getsource(kl.pcs_send_ok)
    assert "credit_lt_floor" not in inspect.getsource(kl.kill_look_send_block)
