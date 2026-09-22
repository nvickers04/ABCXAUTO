"""pcs-skew Arm v0 kill-window LOOK contract — tests for thin-prompt spec §4.

Look budget, one tool surface, #165 mill, dual-mode, F10 $, hygiene.
Does not start looking, TWS, or 7496.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import EMPTY_GROK_TRIES, MAX_TOOL_STEPS, agent_tools
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
    f10_loop_halted,
    mark_f10_loop_halt,
    note_research_look,
    research_week_looks,
    reset_session_caps,
    usage,
)
from abcxauto.thin_rth_kill_look import (
    AH_RESEARCH_LOOKS_PER_WEEK,
    EST_THIS_LOOK_USD,
    F10_HARD_USD,
    F10_PREFERRED_USD,
    MODE_ABORT,
    MODE_MANAGE,
    MODE_OPEN,
    MODE_RESEARCH,
    PCS_CARD,
    REASON_ALLOWLIST,
    REASON_BOOK_UNRELIABLE,
    REASON_NAMELESS,
    REASON_F10,
    REASON_MODEL_COST,
    REASON_PORT,
    REASON_RESEARCH_PROMPT,
    REASON_RESEARCH_WEEK,
    RESEARCH_PROMPT_TOKENS_MAX,
    WINDOW_MODEL_USD,
    WINDOW_N,
    f10_gate,
    f10_hard_tripped,
    f10_open_look_halted,
    is_f10_look_halt,
    mark_f10_hard_trip,
    record_f10_loop_halt,
    has_open_pcs_skew_lot,
    has_open_stk_lot,
    kill_look_enabled,
    kill_look_port_ok,
    kill_look_rth,
    kill_look_send_block,
    kill_mode,
    look_kill_mill,
    pcs_send_ok,
    research_prompt_ok,
    rth_model_no_xhigh,
    skip_look_reason,
)
from tests.test_brain_tools import _scripted_chat_client, _world
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


def _stk_lot() -> dict:
    return {
        "symbol": "AVGO",
        "quantity": 89,
        "secType": "STK",
        "sec_type": "STK",
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
    assert F10_HARD_USD == 15.0
    assert F10_PREFERRED_USD == 10.0
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


def test_entry_budget_deleted_allows_n_plus_one_looks(monkeypatch):
    """No one-look RTH entry budget: N>1 looks still OPEN; send not force-blocked."""
    import abcxauto.session_caps as caps
    import abcxauto.thin_rth_kill_look as kill

    _kill_on(monkeypatch)
    reset_session_caps()
    assert not hasattr(kill, "RTH_ENTRY_LOOKS_MAX")
    assert not hasattr(caps, "KILL_ENTRY_LOOKS_MAX")
    assert not hasattr(caps, "consume_open_entry_look")
    f10 = _allow_f10()
    assert kill_mode("regular", positions=[], open_lots=[], f10=f10) == MODE_OPEN
    assert skip_look_reason("regular", positions=[], f10=f10) == ""
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    assert kill_look_send_block(act, session="regular", f10=f10) is None
    # Unknown abort fuse must not lie as NO_SEND:f10.
    ok, why = pcs_send_ok(
        "vertical_spread",
        dict(PCS_OPEN),
        PCS_CARD,
        mode=MODE_ABORT,
        abort_fuse="none",
    )
    assert ok is False
    assert why == REASON_PORT
    assert why != REASON_F10
    ok_f10, why_f10 = pcs_send_ok(
        "vertical_spread",
        dict(PCS_OPEN),
        PCS_CARD,
        mode=MODE_ABORT,
        abort_fuse="F10",
    )
    assert ok_f10 is False
    assert why_f10 == REASON_F10


def test_start_does_not_soften_f10(monkeypatch):
    """Dollar F10 latches and blocks new risk; Start still looks."""
    _kill_on(monkeypatch)
    reset_session_caps()
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
    assert skip_look_reason("regular", positions=[], f10=hard) == ""
    assert f10_loop_halted() is True
    blocked = kill_look_send_block(
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        session="regular",
        f10=hard,
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_F10


def test_start_does_not_soften_7496(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: SimpleNamespace(ibkr_port=7496, pcs_kill_look=True),
    )
    assert kill_look_port_ok() is False
    assert (
        skip_look_reason("regular", positions=[], f10=_allow_f10()) == REASON_PORT
    )


def test_open_send_ok_after_multiple_looks_f10_still_hard(monkeypatch):
    """After repeated OPEN looks, legal send still goes; real F10 still blocks."""
    _kill_on(monkeypatch)
    reset_session_caps()
    f10 = _allow_f10()
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    for _ in range(3):
        assert kill_mode("regular", positions=[], f10=f10) == MODE_OPEN
        assert kill_look_send_block(act, session="regular", f10=f10) is None
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
    assert skip_look_reason("regular", positions=[], same_look=True, f10=hard) == ""
    assert f10_loop_halted() is True
    blocked = kill_look_send_block(act, session="regular", f10=hard)
    assert blocked is not None
    assert blocked["reason_code"] == REASON_F10


def test_manage_replaces_second_entry(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
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


def test_open_stk_lot_is_manage_like_pcs(monkeypatch):
    """Open STK (e.g. 89 AVGO) is MANAGE for look-skip; F10 still blocks new risk."""
    _kill_on(monkeypatch)
    reset_session_caps()
    lot = _stk_lot()
    label = "AVGO STK LONG 89"
    assert has_open_stk_lot([lot], [label]) is True
    assert has_open_pcs_skew_lot([lot], [label]) is False
    assert (
        kill_mode(
            "regular",
            positions=[lot],
            open_lots=[label],
            f10=_allow_f10(),
        )
        == MODE_MANAGE
    )
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
    assert hard["reason_code"] == REASON_F10
    # Flat book and open STK: F10 still looks; new risk stays blocked at send.
    assert skip_look_reason("regular", positions=[], f10=hard) == ""
    assert (
        skip_look_reason(
            "regular",
            positions=[lot],
            open_lots=[label],
            f10=hard,
        )
        == ""
    )
    # Premarket research label must not hide open STK from manage.
    assert (
        kill_mode(
            "premarket",
            positions=[lot],
            open_lots=[label],
            f10=_allow_f10(),
        )
        == MODE_MANAGE
    )
    assert kill_mode("premarket", positions=[], f10=_allow_f10()) == MODE_RESEARCH
    new_risk = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    blocked = kill_look_send_block(
        new_risk,
        session="regular",
        positions=[lot],
        open_lots=[label],
        f10=hard,
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_F10
    # Even under preferred F10 allow, MANAGE blocks new named risk.
    blocked_open = kill_look_send_block(
        new_risk,
        session="regular",
        positions=[lot],
        open_lots=[label],
        f10=_allow_f10(),
    )
    assert blocked_open is not None
    assert blocked_open["reason_code"] == REASON_ALLOWLIST
    close = {
        "strategy": "market_bracket",
        "params": {
            "symbol": "AVGO",
            "quantity": 89,
            "stop_price": 100.0,
            "closing_position": True,
            "card": "avgo-exit",
        },
        "card": "avgo-exit",
    }
    assert (
        kill_look_send_block(
            close,
            session="regular",
            positions=[lot],
            open_lots=[label],
            f10=hard,
        )
        is None
    )


def _catalog_names() -> set[str]:
    from abcxauto.brain import AGENT_TOOLS

    names = set()
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        names.add(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    names.add("web")
    return names


def test_catalog_offered_in_rth_and_outside(monkeypatch):
    """Every catalog tool including send on RTH and outside (paper stay-up)."""
    _kill_on(monkeypatch)
    catalog = _catalog_names()
    rth = _tool_names("regular")
    assert catalog == rth
    assert "send" in rth
    assert "scan" in rth
    assert "news" in rth
    assert "candles" in rth
    assert "odds" in rth
    assert "web" in rth
    assert "note" not in rth
    assert "recall" in rth
    assert "self_tune" in rth
    assert "option_facts" in rth
    enum = []
    for t in agent_tools(session="regular"):
        fn = getattr(t, "function", None)
        if str(getattr(fn, "name", None) or "") != "send":
            continue
        params = getattr(fn, "parameters", None) or {}
        if isinstance(params, str):
            params = json.loads(params)
        enum = list(((params.get("properties") or {}).get("strategy") or {}).get("enum") or [])
    from abcxauto.thin_rth_kill_look import send_strategy_names
    assert enum == send_strategy_names(session="regular")
    assert "vertical_spread" in enum
    assert "iron_condor" in enum
    assert "market_bracket" in enum
    assert "ratio_spread" not in enum
    for sess in ("premarket", "postmarket", "closed"):
        research = _tool_names(sess)
        assert research == catalog, sess
        assert "send" in research
        assert "scan" in research
        assert "web" in research


def test_named_card_and_defined_risk_send_gates(monkeypatch):
    _kill_on(monkeypatch)
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
async def test_send_not_session_banned_outside_rth(monkeypatch):
    """Paper stay-up: session alone is not research_no_send; thin gates still apply."""
    _kill_on(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.desk_mode import REASON_RESEARCH_NO_SEND

    async def _fake_execute(act, connector, world, snap):
        return {
            "status": "ok",
            "note": "execute_ticket reached",
            "strategy": str(act.get("strategy") or ""),
        }

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", _fake_execute)
    # _run_tool may already have bound execute_ticket via brain_tools import.
    monkeypatch.setattr(
        "abcxauto.brain_tools.execute_ticket",
        _fake_execute,
        raising=False,
    )
    for sess in ("premarket", "postmarket", "closed"):
        raw = await _run_tool(
            "send",
            {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
            connector=None,
            world=_world(session_status=sess, flat=True),
            snap={"positions": []},
            turn=BrainTurn(),
        )
        data = json.loads(raw)
        assert data.get("reason_code") != REASON_RESEARCH_NO_SEND, sess
        assert data.get("status") == "ok", sess


def test_run_tool_has_no_die_tool_block():
    from abcxauto import brain_tools, thin_rth_kill_look as kl

    assert "die_tool_block" not in inspect.getsource(brain_tools._run_tool)
    assert "kill_look_die_tool" not in inspect.getsource(brain_tools._run_tool)
    assert not hasattr(kl, "STAY_TOOLS")
    assert not hasattr(kl, "DIE_TOOLS")
    assert not hasattr(kl, "die_tool_block")
    assert not hasattr(kl, "look_gather_spin_mill")
    assert not hasattr(kl, "filter_agent_tool_names")


def test_kill_mill_is_synthesize_only_not_hunt(monkeypatch):
    _kill_on(monkeypatch)
    assert spoken_synthesize_mill("Let me gather the tape.", sends=0)
    assert look_kill_mill(
        {"rationale": "Let me synthesize the picture.", "sends": 0, "tool_trace": []},
        session="regular",
    )
    assert not look_kill_mill(
        {"rationale": "Let me synthesize the picture.", "sends": 0, "tool_trace": ["book"]},
        session="regular",
    )
    assert not look_kill_mill(
        {"rationale": "Gathering more scan color.", "sends": 0, "tool_trace": ["scan"]},
        session="regular",
    )
    assert SYNTHESIZE_MILL_TRIES == 2


def test_kill_look_tool_turn_caps_are_deleted():
    """Mid-thesis 6-tool / 4-turn / 4-manage guillotines are gone. Soften=FAIL."""
    from abcxauto import brain, brain_tools
    from abcxauto import thin_rth_kill_look as kl
    from abcxauto.risk_gates import new_risk_card_error

    kl_src = inspect.getsource(kl)
    brain_src = inspect.getsource(brain)
    tools_src = inspect.getsource(brain_tools)
    joined = "\n".join((kl_src, brain_src, tools_src))
    assert "TOOLS_ENTRY_MAX" not in kl_src
    assert "TOOLS_MANAGE_MAX" not in kl_src
    assert "MODEL_TURNS_MAX" not in kl_src
    assert "turns_or_tools_breached" not in joined
    assert "force_skip_or_manage" not in joined
    assert "kill_look_capped" not in joined
    assert "kill-look tool cap" not in joined
    assert "kill_look_turns" not in joined
    assert "kill_look_tools" not in joined
    assert "auto_resume" not in joined
    assert "auto-resume" not in joined
    assert MAX_TOOL_STEPS >= 48
    assert F10_HARD_USD == 15.0
    assert get_config().ibkr_port != 7496
    assert kill_look_port_ok() is True
    assert new_risk_card_error("") == "new risk requires params.card naming a play"
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


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


async def _stub_send_exec(monkeypatch):
    async def fake_exec(act, *_a, **_k):
        return {"status": "submitted", "strategy": act.get("strategy"), "note": "test"}

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)


@pytest.mark.asyncio
async def test_run_tool_send_not_force_skipped_after_entry_tool_budget(monkeypatch):
    """OPEN send after 7 tools is not a kill-look tool-cap skip."""
    _kill_on(monkeypatch)
    await _stub_send_exec(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool

    world = _world(session_status="regular", flat=True)
    turn = BrainTurn()
    turn.kill_mode = MODE_OPEN
    turn.steps = 5
    turn.tool_trace = [
        "book",
        "status",
        "quote",
        "option_chain",
        "option_quote",
        "fills",
        "quote",
    ]
    raw = await _run_tool(
        "send",
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        connector=None,
        world=world,
        snap={"positions": []},
        turn=turn,
    )
    data = json.loads(raw)
    assert data.get("reason_code") not in {
        "kill_look_tools",
        "kill_look_turns",
        "kill_look_capped",
    }
    assert "force SKIP" not in str(data.get("note") or "")
    assert data.get("strategy") != "skipped"
    assert data.get("status") == "submitted"


@pytest.mark.asyncio
async def test_run_tool_send_not_force_skipped_after_manage_tool_budget(monkeypatch):
    """MANAGE send after 5 tools is not a mid-thesis manage-cap skip."""
    _kill_on(monkeypatch)
    await _stub_send_exec(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool

    world = _world(session_status="regular", flat=False)
    turn = BrainTurn()
    turn.kill_mode = MODE_MANAGE
    turn.steps = 5
    turn.tool_trace = ["book", "status", "quote", "fills", "option_quote"]
    raw = await _run_tool(
        "send",
        {
            "strategy": "vertical_spread",
            "params": {**PCS_OPEN, "closing_position": True},
            "card": PCS_CARD,
        },
        connector=None,
        world=world,
        snap={"positions": [_pcs_lot()]},
        turn=turn,
    )
    data = json.loads(raw)
    assert "MANAGE-only" not in str(data.get("note") or "")
    assert data.get("reason_code") not in {"kill_look_tools", "kill_look_turns"}
    assert data.get("status") == "submitted"


@pytest.mark.asyncio
async def test_grok_turn_does_not_hard_stop_mid_thesis_on_entry_tool_budget(
    monkeypatch,
):
    """OPEN RTH look keeps thinking past the old 6-tool / 4-turn caps."""
    from abcxauto import brain
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from abcxauto.think_stream import reset_speaker, subscribe, unsubscribe

    _kill_on(monkeypatch)
    reset_session_caps()
    clear_interrupt()
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate", lambda: _allow_f10()
    )

    async def fake_read(name, args, **_k):
        return json.dumps({"ok": name, "last": 248.1})

    monkeypatch.setattr(brain, "_run_tool", fake_read)

    class TC:
        id = "1"
        function = SimpleNamespace(name="book", arguments="{}")

    painted: list[str] = []

    def cap(_kind: str, text: str, piece: str = "") -> None:
        painted.append(piece or text)

    reset_speaker()
    subscribe(cap)
    g, created = _scripted_chat_client(
        rounds=[("", [TC()])] * 7 + ["pcs-skew after the quotes. No more tools."]
    )
    try:
        turn = await grok_turn(
            g,
            connector=None,
            world=_world(session_status="regular", flat=True),
            snap={"positions": [], "protection": {}},
            wake="session=regular send.",
        )
    finally:
        unsubscribe(cap)
        reset_speaker()
    assert not any("kill-look tool cap" in p for p in painted)
    assert not any("step ceiling" in p for p in painted)
    assert turn.tool_budget_hit is False
    assert getattr(turn, "kill_look_capped", False) is False
    assert turn.loop_halted is False
    assert turn.failed is False
    assert turn.steps > 4
    assert turn.tool_trace.count("book") == 7
    assert "pcs-skew after the quotes" in (turn.text or "")
    assert int(getattr(created[0], "rounds", 0) or 0) == 8


@pytest.mark.asyncio
async def test_open_second_named_send_not_blocked_as_one_send(monkeypatch):
    """After a non-blocked preview/send in OPEN, a second legal named send is not kill_look_one_send."""
    _kill_on(monkeypatch)
    from abcxauto.brain import BrainTurn, _run_tool

    async def fake_exec(act, *_a, **_k):
        return {
            "status": "ok",
            "note": "IBKR combo (BAG)",
            "strategy": act.get("strategy"),
            "preview": bool(act.get("preview")),
        }

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)
    world = _world(session_status="regular", flat=True)
    turn = BrainTurn()
    turn.kill_mode = MODE_OPEN
    preview = await _run_tool(
        "send",
        {
            "strategy": "vertical_spread",
            "params": dict(PCS_OPEN),
            "card": PCS_CARD,
            "preview": True,
        },
        connector=None,
        world=world,
        snap={"positions": [], "kill_entry_in_flight": True},
        turn=turn,
    )
    preview_data = json.loads(preview)
    assert preview_data.get("reason_code") != "kill_look_one_send"
    assert preview_data.get("status") != "blocked"
    assert len(turn.sends) == 1

    place = await _run_tool(
        "send",
        {"strategy": "vertical_spread", "params": dict(PCS_OPEN), "card": PCS_CARD},
        connector=None,
        world=world,
        snap={"positions": [], "kill_entry_in_flight": True},
        turn=turn,
    )
    place_data = json.loads(place)
    assert place_data.get("reason_code") != "kill_look_one_send"
    assert place_data.get("status") != "blocked"
    assert "one pcs-skew BAG then stop" not in str(place_data.get("note") or "")
    assert len(turn.sends) == 2


def test_dual_mode_rth_strips_xhigh_ah_no_week_quota(monkeypatch):
    _kill_on(monkeypatch)
    assert rth_model_no_xhigh("grok-4.6-xhigh") == "grok-4.6"
    assert rth_model_no_xhigh("grok-4.6-xhigh-fast") == "grok-4.6-fast"
    cfg = SimpleNamespace(model="grok-4.6-xhigh", model_rth="", model_research="grok-4.6-xhigh")
    assert session_model("regular", cfg) == "grok-4.6"
    assert session_model("premarket", cfg) == "grok-4.6-xhigh"
    assert research_keep_looking("premarket") is False
    assert research_keep_looking("regular") is False
    assert AH_RESEARCH_LOOKS_PER_WEEK == 0
    reset_session_caps()
    assert research_week_looks() == 0
    assert skip_look_reason(
        "premarket", prompt_tokens=RESEARCH_PROMPT_TOKENS_MAX, f10=_allow_f10()
    ) == REASON_RESEARCH_PROMPT
    for _ in range(8):
        note_research_look()
    assert research_week_looks() == 8
    assert skip_look_reason("premarket", f10=_allow_f10()) == ""
    assert skip_look_reason("premarket", f10=_allow_f10()) != REASON_RESEARCH_WEEK
    assert research_prompt_ok(199_999) is True
    assert research_prompt_ok(RESEARCH_PROMPT_TOKENS_MAX) is False


def test_write_research_brief_writes_rth_under_kill(tmp_path, monkeypatch):
    _kill_on(monkeypatch)
    path = tmp_path / "research_brief.json"
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(path))
    out = write_research_brief(session="regular", snap={"news_items": []})
    assert out.get("session") == "regular"
    assert path.is_file()


def test_f10_hard_preferred_unreadable_window_exits(monkeypatch):
    _kill_on(monkeypatch)
    gate = f10_gate(0.0, est_this_look=EST_THIS_LOOK_USD, window_cost=0.0)
    assert gate["allow_new_risk"] is True
    assert gate["preferred_trip"] is False
    pref = f10_gate(9.80, est_this_look=0.35, window_cost=0.0)
    assert pref["allow_new_risk"] is True
    assert pref["preferred_trip"] is True
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
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
            "strategy": "ratio_spread",
            "params": {
                "symbol": "SPY",
                "expiration": "20260718",
                "long_strike": 500.0,
                "short_strike": 510.0,
                "right": "C",
                "ratio": 2,
                "quantity": 1,
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
async def test_execute_ticket_no_entry_budget_f10_lie(monkeypatch):
    """Without entry budget, legal BAG is not abort-stamped NO_SEND:f10."""
    _kill_on(monkeypatch)
    reset_session_caps()
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
    out = await execute_ticket(ticket, object(), world, {"positions": []})
    assert out.get("reason_code") not in {REASON_F10, REASON_ALLOWLIST, REASON_PORT}
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


def test_pro_engine_skip_reason_no_entry_budget(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    assert eng._kill_look_skip_reason("regular", {"positions": [], "protection": {}}) == ""
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate",
        lambda: hard,
    )
    assert (
        eng._kill_look_skip_reason("regular", {"positions": [], "protection": {}})
        == ""
    )
    assert f10_loop_halted() is True


def test_skip_look_reason_book_unreliable(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    snap = {"book_unreliable": True, "positions": []}
    assert (
        skip_look_reason("regular", positions=[], snap=snap, f10=_allow_f10())
        == REASON_BOOK_UNRELIABLE
    )
    assert (
        skip_look_reason(
            "regular",
            positions=[],
            snap=snap,
            f10=_allow_f10(),
            unprotected=True,
        )
        == ""
    )
    assert skip_look_reason("regular", positions=[], f10=_allow_f10()) == ""


def test_f10_trip_halts_open_look_exits_still_ok(monkeypatch):
    """KEEP-1: hard F10 latches; looks still run; new risk blocked; MANAGE / close still go."""
    from abcxauto.pro_engine import ProEngine

    _kill_on(monkeypatch)
    reset_session_caps()
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
    assert hard["allow_new_risk"] is False
    assert hard["reason_code"] == REASON_F10
    assert f10_hard_tripped(hard) is True
    assert f10_open_look_halted(hard) is True
    assert skip_look_reason("regular", positions=[], f10=hard) == ""
    assert f10_loop_halted() is True
    assert usage("regular")["f10_tripped"] is True
    assert usage("regular")["loop_halted"] is True
    assert skip_look_reason("regular", positions=[], f10=_allow_f10()) == ""
    assert skip_look_reason(
        "regular", positions=[], same_look=True, f10=_allow_f10()
    ) == ""
    mill_eng = ProEngine()
    mill_eng._mill_wake = True
    assert mill_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == ""
    unpaid_eng = ProEngine()
    unpaid_eng._ticket_wake = True
    assert unpaid_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == ""
    recover_eng = ProEngine()
    recover_eng._recover_same_chat = True
    assert recover_eng._kill_look_skip_reason(
        "regular", {"positions": [], "protection": {}}
    ) == ""
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
    stk = _stk_lot()
    assert (
        skip_look_reason(
            "regular",
            positions=[stk],
            open_lots=["AVGO STK LONG 89"],
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
    assert skip_look_reason("premarket", f10=hard) == ""
    assert (
        kill_mode(
            "premarket",
            positions=[stk],
            open_lots=["AVGO STK LONG 89"],
            f10=hard,
        )
        == MODE_MANAGE
    )
    assert skip_look_reason(
        "premarket",
        positions=[stk],
        open_lots=["AVGO STK LONG 89"],
        f10=hard,
    ) == ""
    assert skip_look_reason("regular", positions=[], f10=hard, unprotected=True) == ""


def test_f10_preferred_does_not_halt_loop(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    pref = f10_gate(9.80, est_this_look=0.35, window_cost=0.0)
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
    from abcxauto.think_stream import last_turn_path

    last = json.loads(last_turn_path().read_text(encoding="utf-8"))
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
async def test_projected_hard_cross_still_calls_model(monkeypatch):
    """Dollar F10 that would cross $15 still calls the model; new risk stays blocked."""
    _kill_on(monkeypatch)
    reset_session_caps()
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
    assert hard["projected"] > F10_HARD_USD
    monkeypatch.setattr("abcxauto.thin_rth_kill_look.live_f10_gate", lambda: hard)
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from tests.test_brain_tools import _scripted_chat_client

    clear_interrupt()
    g, created = _scripted_chat_client(rounds=["watching the book"])
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={"positions": [], "protection": {}},
        wake="look",
    )
    assert int(getattr(created[0], "rounds", 0) or 0) == 1
    assert turn.loop_halted is False
    # Latch via skip path / send gate — look itself must not set turn.loop_halted.
    assert skip_look_reason("regular", positions=[], f10=hard) == ""
    assert f10_loop_halted() is True
    assert usage("regular")["model_cost_post_trip_usd"] == 0.0


@pytest.mark.asyncio
async def test_f10_halt_still_calls_grok_turn(monkeypatch):
    """Already-tripped F10 still opens a chat; turn.loop_halted stays false."""
    _kill_on(monkeypatch)
    reset_session_caps()
    mark_f10_loop_halt()
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from tests.test_brain_tools import _scripted_chat_client

    clear_interrupt()
    g, created = _scripted_chat_client(rounds=["watching the book"])
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={"positions": [], "protection": {}},
        wake="look",
    )
    assert int(getattr(created[0], "rounds", 0) or 0) == 1
    assert turn.loop_halted is False
    assert f10_loop_halted() is True
    assert skip_look_reason("regular", positions=[], f10=_allow_f10()) == ""


@pytest.mark.asyncio
async def test_execute_ticket_f10_blocks_new_risk_allows_close(monkeypatch):
    _kill_on(monkeypatch)
    reset_session_caps()
    from abcxauto.agent_loop import execute_ticket

    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
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
    assert skip_look_reason("regular", positions=[], f10=hard) == ""
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
    assert skip_look_reason("regular", positions=[], f10=unread) == ""
    assert f10_loop_halted() is True
    assert usage("regular")["model_cost_post_trip_usd"] == 0.0
    assert skip_look_reason("premarket", f10=unread) == ""


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
    assert skip_look_reason("regular", positions=[], f10=inf) == ""
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
    assert is_f10_look_halt(REASON_PORT) is False
    assert is_f10_look_halt("") is False


@pytest.mark.asyncio
async def test_unreadable_model_cost_still_calls_grok_turn(monkeypatch):
    """Unreadable cost latches the fuse but still calls the model."""
    _kill_on(monkeypatch)
    reset_session_caps()
    unread = f10_gate(None, est_this_look=0.35, window_cost=0.0)
    monkeypatch.setattr("abcxauto.thin_rth_kill_look.live_f10_gate", lambda: unread)
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from tests.test_brain_tools import _scripted_chat_client

    clear_interrupt()
    g, created = _scripted_chat_client(rounds=["watching the book"])
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={"positions": [], "protection": {}},
        wake="look",
    )
    assert int(getattr(created[0], "rounds", 0) or 0) == 1
    assert turn.loop_halted is False
    assert skip_look_reason("regular", positions=[], f10=unread) == ""
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

    assert F10_HARD_USD == 15.0
    assert get_config().ibkr_port != 7496
    assert Config().ibkr_port != 7496

    close = {**NAMED_VERT, "closing_position": True}
    ok, why = pcs_send_ok(
        "vertical_spread", close, "spy-bp-750-745", mode=MODE_ABORT
    )
    assert ok is True
    assert why == "closing"
    hard = f10_gate(14.80, est_this_look=0.35, window_cost=0.0)
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


def test_skip_glass_line_names_brief_and_f10():
    from abcxauto.thin_rth_kill_look import REASON_F10, skip_glass_line

    assert "brief loop halt" in skip_glass_line("brief_loop_halted")
    assert "F10 loop halt" in skip_glass_line(REASON_F10)
    assert skip_glass_line("") == ""
