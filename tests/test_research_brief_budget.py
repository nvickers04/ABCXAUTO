"""Rank 2: named-card research-brief lineage (no look latch).

Week card is not a trading document. note_brief_turn / mark_brief_loop_halt
are no-ops; allow_brief_turn always allows. Looks are not skipped for halt.
Hygiene: F10 $15 hard, port≠7496, SYSTEM_PROMPT lock.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.config import get_config
from abcxauto.desk_mode import (
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
    allow_brief_turn,
    DEFAULT_RESEARCH_CARD_ID,
    brief_budget_gate,
    brief_loop_halted,
    card_row,
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
    assert AH_RESEARCH_LOOKS_PER_WEEK == 0
    assert RESEARCH_PROMPT_TOKENS_MAX == 200_000


def test_research_prompt_token_fuse_still_skips_runaway_prompt(monkeypatch):
    from abcxauto.thin_rth_kill_look import REASON_RESEARCH_PROMPT, research_prompt_ok

    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")
    assert research_prompt_ok(199_999) is True
    assert research_prompt_ok(200_000) is False
    assert skip_look_reason("premarket", prompt_tokens=200_000) == REASON_RESEARCH_PROMPT
    assert skip_look_reason("premarket", prompt_tokens=1) == ""


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


def test_note_brief_turn_is_noop_no_increment_no_latch():
    reset_research_budget()
    row = open_research_card(CARD, WINDOW)
    assert row["research_card_id"] == CARD
    assert row["prove_window_id"] == WINDOW
    assert row["gate_verdict"] == GATE_INCONCLUSIVE
    assert row["gate_verdict"] in {GATE_PASS, GATE_FAIL, GATE_INCONCLUSIVE, GATE_KILL}
    assert parse_model_cost(row["model_cost_window_USD"]) == 0.0
    first = note_brief_turn(CARD, WINDOW, cost_usd=0.10, tool_calls=3)
    assert first["billed"] is False
    assert first["turns"] == 0
    assert first["tool_calls"] == 0
    assert first["brief_loop_halted"] is False
    assert parse_model_cost(first["model_cost_window_USD"]) == 0.0
    second = note_brief_turn(CARD, WINDOW, cost_usd=0.10, tool_calls=2)
    assert second["turns"] == 0
    assert second["tool_calls"] == 0
    assert brief_loop_halted(CARD, WINDOW) is False
    snap = card_row(CARD, WINDOW)
    assert snap["turns"] == 0
    assert snap["tool_calls"] == 0
    assert snap["brief_loop_halted"] is False


def test_usd_estimate_does_not_latch_or_skip_looks():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    for _ in range(8):
        out = note_brief_turn(CARD, WINDOW)
        assert out["billed"] is False
        assert out["brief_loop_halted"] is False
    row = card_row(CARD, WINDOW)
    assert row["turns"] == 0
    assert row["brief_loop_halted"] is False
    assert brief_loop_halted(CARD, WINDOW) is False
    assert allow_brief_turn(CARD, WINDOW)["allow"] is True
    write_research_brief(
        session="premarket",
        snap={"news_items": []},
        research_card_id=CARD,
        prove_window_id=WINDOW,
    )
    assert skip_look_reason("premarket") == ""
    assert research_keep_looking("premarket") is False


def test_turns_cap_does_not_latch_via_note():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    for _ in range(BRIEF_CARD_TURNS_MAX + 2):
        out = note_brief_turn(CARD, WINDOW, cost_usd=0.01)
        assert out["billed"] is False
        assert out["brief_loop_halted"] is False
    row = card_row(CARD, WINDOW)
    assert row["turns"] == 0
    assert row["brief_loop_halted"] is False
    assert allow_brief_turn(CARD, WINDOW, est=0.01)["allow"] is True


def test_tools_cap_does_not_latch_via_note():
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    out = note_brief_turn(CARD, WINDOW, cost_usd=0.01, tool_calls=40)
    assert out["billed"] is False
    assert out["tool_calls"] == 0
    assert out["brief_loop_halted"] is False
    over = note_brief_turn("other-card", WINDOW, cost_usd=0.01, tool_calls=41)
    assert over["billed"] is False
    assert over["brief_loop_halted"] is False
    assert card_row("other-card", WINDOW) is None
    assert allow_brief_turn(CARD, WINDOW, est=0.01, add_tools=1)["allow"] is True


def test_unreadable_and_nonfinite_cost_gate_still_fail_closes():
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
    # mark_brief_loop_halt is a no-op — unreadable cost does not latch looks.
    halted = set_model_cost_window(CARD, WINDOW, float("nan"))
    assert halted["brief_loop_halted"] is False
    assert brief_loop_halted(CARD, WINDOW) is False
    assert allow_brief_turn(CARD, WINDOW)["allow"] is True
    assert skip_look_reason("premarket") == ""


def test_prior_iso_week_halt_does_not_skip_this_week(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.research_budget import (
        default_prove_window_id,
        mark_brief_loop_halt,
        resolve_research_card,
    )

    reset_research_budget()
    now = datetime(2026, 9, 17, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    today = default_prove_window_id(now=now)
    assert today == "2026-W38"
    # No-op mark — still must not skip this week's look.
    marked = mark_brief_loop_halt(DEFAULT_RESEARCH_CARD_ID, "2026-W37")
    assert marked["brief_loop_halted"] is False
    write_research_brief(
        session="premarket",
        snap={"news_items": []},
        research_card_id=DEFAULT_RESEARCH_CARD_ID,
        prove_window_id="2026-W37",
        now=now,
    )
    card, window = resolve_research_card(now=now)
    assert card == DEFAULT_RESEARCH_CARD_ID
    assert window == today
    assert skip_look_reason("premarket", now=now) == ""
    pinned = resolve_research_card(prove_window_id="2026-W37", now=now)
    assert pinned == (DEFAULT_RESEARCH_CARD_ID, "2026-W37")


def test_write_research_brief_stamps_lineage_without_verdict(tmp_path, monkeypatch):
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
    assert "gate_verdict" not in out
    assert out.get("gate_verdict") not in {
        GATE_PASS,
        GATE_FAIL,
        GATE_INCONCLUSIVE,
        GATE_KILL,
    }
    assert parse_model_cost(out["model_cost_window_USD"]) == pytest.approx(0.0)
    assert "tickets" not in out
    assert "expectancy" not in out
    rth = write_research_brief(session="regular", snap={"news_items": []})
    assert rth.get("session") == "regular"
    assert rth.get("mode") == "research"
    assert "gate_verdict" not in rth


@pytest.mark.asyncio
async def test_grok_turn_looks_and_does_not_latch_via_note(monkeypatch):
    """note_brief_turn is a no-op. Premarket still calls the model."""
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from tests.test_brain_tools import _scripted_chat_client

    reset_research_budget()
    open_research_card(CARD, WINDOW)
    for _ in range(8):
        assert note_brief_turn(CARD, WINDOW)["brief_loop_halted"] is False
    clear_interrupt()
    g, created = _scripted_chat_client(rounds=["watching the book"])
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="premarket"),
        snap={"positions": [], "protection": {}, "research_card_id": CARD, "prove_window_id": WINDOW},
        wake="look",
    )
    assert int(getattr(created[0], "rounds", 0) or 0) == 1
    assert turn.loop_halted is False
    assert turn.brief_billed is False
    assert turn.brief_loop_halted is False
    assert card_row(CARD, WINDOW)["turns"] == 0
    assert card_row(CARD, WINDOW)["brief_loop_halted"] is False
    assert skip_look_reason("premarket") == ""


def test_budget_reloads_when_operator_edits_file(tmp_path, monkeypatch):
    import json
    import time

    path = tmp_path / "research_budget.json"
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BUDGET_PATH", str(path))
    reset_research_budget()
    open_research_card(CARD, WINDOW)
    # note_brief_turn does not write; operator edit is the reload path.
    assert card_row(CARD, WINDOW)["turns"] == 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    key = f"{CARD}::{WINDOW}"
    raw["cards"][key]["turns"] = 4
    time.sleep(0.02)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    assert card_row(CARD, WINDOW)["turns"] == 4
    assert not (tmp_path / "research_budget.json.tmp").exists()
    # Still no new latch from note / allow.
    assert note_brief_turn(CARD, WINDOW)["brief_loop_halted"] is False
    assert allow_brief_turn(CARD, WINDOW)["allow"] is True


def test_empty_or_failed_research_round_is_not_billed():
    from abcxauto.brain import (
        BrainTurn,
        _bill_research_brief_round,
        _should_bill_research_round,
    )

    reset_research_budget()
    open_research_card(CARD, WINDOW)
    snap = {"research_card_id": CARD, "prove_window_id": WINDOW}
    empty = BrainTurn(text="")
    assert _should_bill_research_round(empty) is False
    assert _bill_research_brief_round(empty, session="premarket", snap=snap) is False
    question = BrainTurn(text="?")
    assert _should_bill_research_round(question) is False
    failed = BrainTurn(text="watching the open", failed=True)
    assert _should_bill_research_round(failed, tool_calls=3) is False
    assert _bill_research_brief_round(
        failed, session="premarket", snap=snap, tool_calls=3
    ) is False
    parked = BrainTurn(text="watching the open", parked=True)
    assert _should_bill_research_round(parked) is False
    assert card_row(CARD, WINDOW)["turns"] == 0
    spoken = BrainTurn(text="watching the open")
    assert _should_bill_research_round(spoken) is True
    tools_only = BrainTurn(text="")
    assert _should_bill_research_round(tools_only, tool_calls=2) is True
    assert _bill_research_brief_round(spoken, session="premarket", snap=snap) is False
    # note_brief_turn no-op: bill path runs but does not latch or increment.
    assert spoken.brief_billed is False
    assert card_row(CARD, WINDOW)["turns"] == 0
    assert card_row(CARD, WINDOW)["brief_loop_halted"] is False
    assert _bill_research_brief_round(spoken, session="premarket", snap=snap) is False
    assert card_row(CARD, WINDOW)["turns"] == 0
    rth = BrainTurn(text="watching the open")
    assert _bill_research_brief_round(rth, session="regular", snap=snap) is False
    assert rth.brief_billed is False
    assert card_row(CARD, WINDOW)["turns"] == 0


@pytest.mark.asyncio
async def test_one_research_look_calls_note_once_without_latch(monkeypatch):
    import json

    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from tests.test_brain_tools import _scripted_chat_client

    reset_research_budget()
    open_research_card(CARD, WINDOW)
    billed: list[dict] = []
    real = note_brief_turn

    def spy(card, window, **kwargs):
        billed.append(dict(kwargs))
        return real(card, window, **kwargs)

    monkeypatch.setattr("abcxauto.research_budget.note_brief_turn", spy)

    class TC:
        id = "1"
        function = SimpleNamespace(name="news", arguments="{}")

    async def fake_read(name, args, **_k):
        return json.dumps({"ok": name, "items": [{"symbol": "NVDA", "headline": "beats"}]})

    monkeypatch.setattr("abcxauto.brain._run_tool", fake_read)
    clear_interrupt()
    g, created = _scripted_chat_client(
        rounds=[("", [TC()]), "watching NVDA after the news."]
    )
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="premarket"),
        snap={
            "positions": [],
            "protection": {},
            "research_card_id": CARD,
            "prove_window_id": WINDOW,
        },
        wake="session=premarket desk_mode=research.",
    )
    assert turn.failed is False
    assert turn.parked is False
    assert turn.brief_billed is False
    assert turn.brief_loop_halted is False
    assert int(getattr(created[0], "rounds", 0) or 0) == 2
    assert len(billed) == 1
    assert card_row(CARD, WINDOW)["turns"] == 0
    assert card_row(CARD, WINDOW)["brief_loop_halted"] is False
    assert skip_look_reason("premarket") == ""


@pytest.mark.asyncio
async def test_rth_look_writes_brief_without_billing_ah_card(monkeypatch):
    from abcxauto.brain import grok_turn
    from abcxauto.desk_mode import load_research_brief
    from abcxauto.park_clock import clear_interrupt
    from tests.test_brain_tools import _scripted_chat_client

    reset_research_budget()
    open_research_card(CARD, WINDOW)
    billed: list[int] = []
    real = note_brief_turn

    def spy(*_a, **_k):
        billed.append(1)
        return real(*_a, **_k)

    monkeypatch.setattr("abcxauto.research_budget.note_brief_turn", spy)
    clear_interrupt()
    g, _created = _scripted_chat_client(rounds=["watching SPY. No ticket."])
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="regular"),
        snap={
            "positions": [],
            "protection": {},
            "research_card_id": CARD,
            "prove_window_id": WINDOW,
        },
        wake="session=regular send.",
    )
    assert "watching SPY" in (turn.text or "")
    assert turn.brief_billed is False
    assert billed == []
    assert card_row(CARD, WINDOW)["turns"] == 0
    brief = load_research_brief()
    assert brief.get("session") == "regular"
    assert brief.get("mode") == "research"
    assert "gate_verdict" not in brief
