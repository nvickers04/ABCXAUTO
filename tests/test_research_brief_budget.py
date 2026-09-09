"""Rank 2: named-card research-brief budget + lineage promote gate.

Fake card → stub turns → ledger increments; trip → no further turns;
promote missing / ≠PASS ⇒ refuse. No BA / options chain.
Hygiene: F10 $15 hard, port≠7496, SYSTEM_PROMPT lock.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.config import get_config
from abcxauto.desk_mode import (
    promote_lab,
    research_keep_looking,
    write_research_brief,
)
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.research_budget import (
    BRIEF_CARD_MODEL_HARD_USD,
    BRIEF_CARD_TOOLS_MAX,
    BRIEF_CARD_TURNS_MAX,
    EST_BRIEF_TURN_USD,
    GATE_FAIL,
    GATE_INCONCLUSIVE,
    GATE_KILL,
    GATE_PASS,
    REASON_BRIEF_COST,
    REASON_BRIEF_LOOP,
    REASON_LAB_PROMOTE,
    allow_brief_turn,
    brief_budget_gate,
    brief_loop_halted,
    card_row,
    lab_promote,
    lab_promote_ok,
    note_brief_turn,
    open_research_card,
    parse_model_cost,
    reset_research_budget,
    set_gate_verdict,
    set_model_cost_window,
)
from abcxauto.self_tune import apply_self_tune
from abcxauto.thin_rth_kill_look import (
    AH_RESEARCH_LOOKS_PER_WEEK,
    F10_HARD_USD,
    RESEARCH_PROMPT_TOKENS_MAX,
    skip_look_reason,
)
from tests.test_brain_tools import _world
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

CARD = "fake-card"
WINDOW = "prove-w1"


def test_hygiene_f10_port_prompt_and_v0_constants():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert get_config().ibkr_port != 7496
    assert F10_HARD_USD == 15.0
    assert BRIEF_CARD_MODEL_HARD_USD == 1.50
    assert BRIEF_CARD_TURNS_MAX == 8
    assert BRIEF_CARD_TOOLS_MAX == 40
    assert EST_BRIEF_TURN_USD == 0.20
    assert AH_RESEARCH_LOOKS_PER_WEEK == 2
    assert RESEARCH_PROMPT_TOKENS_MAX == 200_000


def test_self_tune_cannot_raise_brief_card_constants():
    before = (
        BRIEF_CARD_MODEL_HARD_USD,
        BRIEF_CARD_TURNS_MAX,
        BRIEF_CARD_TOOLS_MAX,
        EST_BRIEF_TURN_USD,
    )
    apply_self_tune(
        {
            "BRIEF_CARD_MODEL_HARD_USD": 99.0,
            "BRIEF_CARD_TURNS_MAX": 99,
            "BRIEF_CARD_TOOLS_MAX": 400,
            "EST_BRIEF_TURN_USD": 9.0,
        },
        persist=False,
    )
    assert BRIEF_CARD_MODEL_HARD_USD == before[0] == 1.50
    assert BRIEF_CARD_TURNS_MAX == before[1] == 8
    assert BRIEF_CARD_TOOLS_MAX == before[2] == 40
    assert EST_BRIEF_TURN_USD == before[3] == 0.20
    assert F10_HARD_USD == 15.0


def test_fake_card_stub_turns_increment_ledger():
    reset_research_budget()
    row = open_research_card(CARD, WINDOW)
    assert row["research_card_id"] == CARD
    assert row["prove_window_id"] == WINDOW
    assert row["gate_verdict"] == GATE_INCONCLUSIVE
    assert row["gate_verdict"] in {GATE_PASS, GATE_FAIL, GATE_INCONCLUSIVE, GATE_KILL}
    assert parse_model_cost(row["model_cost_window_USD"]) == 0.0
    first = note_brief_turn(CARD, WINDOW, cost_usd=0.10, tool_calls=3)
    assert first["billed"] is True
    assert first["turns"] == 1
    assert first["tool_calls"] == 3
    assert first["model_cost_window_USD"] == pytest.approx(0.10)
    second = note_brief_turn(CARD, WINDOW, cost_usd=0.10, tool_calls=2)
    assert second["turns"] == 2
    assert second["tool_calls"] == 5
    assert second["model_cost_window_USD"] == pytest.approx(0.20)
    assert brief_loop_halted(CARD, WINDOW) is False
    snap = card_row(CARD, WINDOW)
    assert snap["turns"] == 2
    assert snap["tool_calls"] == 5


def test_usd_trip_stops_further_billed_turns():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    # 7 * 0.20 = 1.40; 8th projected 1.60 > 1.50.
    for _ in range(7):
        out = note_brief_turn(CARD, WINDOW)
        assert out["billed"] is True
    row = card_row(CARD, WINDOW)
    assert row["turns"] == 7
    assert row["model_cost_window_USD"] == pytest.approx(1.40)
    eighth = note_brief_turn(CARD, WINDOW)
    assert eighth["billed"] is False
    assert eighth["brief_loop_halted"] is True
    assert brief_loop_halted(CARD, WINDOW) is True
    assert card_row(CARD, WINDOW)["turns"] == 7
    ninth = note_brief_turn(CARD, WINDOW)
    assert ninth["billed"] is False
    assert card_row(CARD, WINDOW)["turns"] == 7
    write_research_brief(
        session="premarket",
        snap={"news_items": []},
        research_card_id=CARD,
        prove_window_id=WINDOW,
    )
    assert skip_look_reason("premarket") == REASON_BRIEF_LOOP
    assert research_keep_looking("premarket") is False


def test_turns_trip_stops_further_billed_turns():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    for i in range(BRIEF_CARD_TURNS_MAX):
        out = note_brief_turn(CARD, WINDOW, cost_usd=0.01)
        assert out["billed"] is True
        assert out["turns"] == i + 1
    row = card_row(CARD, WINDOW)
    assert row["turns"] == 8
    assert row["brief_loop_halted"] is True
    extra = note_brief_turn(CARD, WINDOW, cost_usd=0.01)
    assert extra["billed"] is False
    assert card_row(CARD, WINDOW)["turns"] == 8
    assert allow_brief_turn(CARD, WINDOW, est=0.01)["allow"] is False


def test_tools_trip_stops_further_billed_turns():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    out = note_brief_turn(CARD, WINDOW, cost_usd=0.01, tool_calls=40)
    assert out["billed"] is True
    assert out["tool_calls"] == 40
    assert out["brief_loop_halted"] is True
    extra = note_brief_turn(CARD, WINDOW, cost_usd=0.01, tool_calls=1)
    assert extra["billed"] is False
    assert card_row(CARD, WINDOW)["tool_calls"] == 40
    over = note_brief_turn("other-card", WINDOW, cost_usd=0.01, tool_calls=41)
    assert over["billed"] is False
    assert (card_row("other-card", WINDOW) or {}).get("turns", 0) == 0


def test_unreadable_and_nonfinite_cost_fail_closes_and_is_missing_for_promote():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    unread = brief_budget_gate(None, 0, 0)
    assert unread["allow"] is False
    assert unread["reason_code"] == REASON_BRIEF_COST
    nan = brief_budget_gate(float("nan"), 0, 0)
    assert nan["allow"] is False
    assert nan["reason_code"] == REASON_BRIEF_COST
    inf = brief_budget_gate(float("inf"), 0, 0)
    assert inf["allow"] is False
    assert parse_model_cost(float("nan")) is None
    assert parse_model_cost(float("inf")) is None
    assert parse_model_cost(-0.1) is None
    halted = set_model_cost_window(CARD, WINDOW, float("nan"))
    assert halted["brief_loop_halted"] is True
    assert halted["model_cost_window_USD"] is None
    assert brief_loop_halted(CARD, WINDOW) is True
    set_gate_verdict(CARD, WINDOW, GATE_PASS)
    refused = lab_promote(CARD, WINDOW)
    assert refused["ok"] is False
    assert refused["reason_code"] == REASON_LAB_PROMOTE
    assert "missing" in refused["note"]
    assert lab_promote_ok(card_row(CARD, WINDOW)) is False


def test_promote_refuses_missing_and_non_pass():
    reset_research_budget()
    missing = lab_promote(CARD, WINDOW)
    assert missing["ok"] is False
    assert missing["status"] == "refused"
    open_research_card(CARD, WINDOW)
    note_brief_turn(CARD, WINDOW, cost_usd=0.20)
    assert card_row(CARD, WINDOW)["gate_verdict"] == GATE_INCONCLUSIVE
    for verdict in (GATE_FAIL, GATE_INCONCLUSIVE, GATE_KILL):
        set_gate_verdict(CARD, WINDOW, verdict)
        refused = lab_promote(CARD, WINDOW)
        assert refused["ok"] is False
        assert refused["allowed"] is False
        assert refused["reason_code"] == REASON_LAB_PROMOTE
        desk = promote_lab(research_card_id=CARD, prove_window_id=WINDOW)
        assert desk["ok"] is False
    set_gate_verdict(CARD, WINDOW, GATE_PASS)
    ok = lab_promote(CARD, WINDOW)
    assert ok["ok"] is True
    assert ok["allowed"] is True
    assert ok["gate_verdict"] == GATE_PASS
    assert parse_model_cost(ok["model_cost_window_USD"]) == pytest.approx(0.20)
    assert promote_lab(research_card_id=CARD, prove_window_id=WINDOW)["ok"] is True


def test_write_research_brief_stamps_lineage(tmp_path, monkeypatch):
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    note_brief_turn(CARD, WINDOW, cost_usd=0.20)
    set_gate_verdict(CARD, WINDOW, GATE_INCONCLUSIVE)
    out = write_research_brief(
        session="premarket",
        snap={"news_items": []},
        research_card_id=CARD,
        prove_window_id=WINDOW,
    )
    assert out["research_card_id"] == CARD
    assert out["prove_window_id"] == WINDOW
    assert out["gate_verdict"] == GATE_INCONCLUSIVE
    assert parse_model_cost(out["model_cost_window_USD"]) == pytest.approx(0.20)
    rth = write_research_brief(session="regular", snap={"news_items": []})
    assert rth == {}


@pytest.mark.asyncio
async def test_grok_turn_does_not_bill_after_brief_halt(monkeypatch):
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    for _ in range(7):
        note_brief_turn(CARD, WINDOW)
    assert note_brief_turn(CARD, WINDOW)["billed"] is False
    from abcxauto.brain import grok_turn

    calls = {"n": 0}

    async def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("stream_round must not run after brief_loop_halted")

    monkeypatch.setattr("abcxauto.brain.stream_round", boom)
    g = SimpleNamespace(chat=None, model="grok-4.6")
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="premarket"),
        snap={"positions": [], "protection": {}, "research_card_id": CARD, "prove_window_id": WINDOW},
        wake="look",
    )
    assert calls["n"] == 0
    assert turn.brief_loop_halted is True
    assert turn.loop_halted is True
    assert (turn.last_result or {}).get("reason_code") == REASON_BRIEF_LOOP
    assert card_row(CARD, WINDOW)["turns"] == 7
