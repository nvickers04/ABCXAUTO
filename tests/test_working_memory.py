"""This-flight working-memory one-liners.

Grok owns the sentences. Clerk does not invent. Tools stay facts.
Clear on hard reset / overnight park / research↔RTH chat drop.
Hygiene: F10 $2 hard, nameless card, port≠7496, SYSTEM_PROMPT lock.
Does not start looking, TWS, or 7496.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import (
    BrainTurn,
    _book_payload,
    _open_wake,
    _reset_chat,
    agent_tools,
    drop_live_chat,
)
from abcxauto.config import get_config
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.pro_engine import ProEngine
from abcxauto.risk_gates import new_risk_card_error
from abcxauto.thin_rth_kill_look import DIE_TOOLS, F10_HARD_USD, STAY_TOOLS
from abcxauto.think_stream import last_look_facts, write_last_turn
from abcxauto.working_memory import (
    MAX_LINE_CHARS,
    MAX_LINES,
    MAX_RAW_CHARS,
    clear_working_memory,
    material_beat,
    remember,
    shape_line,
    working_memory_lines,
)
from abcxauto.world_state import format_wake
from tests.test_brain_tools import _names_of, _run_tool, _stub_chat_client, _world
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


def _wm(tmp_path, monkeypatch):
    path = tmp_path / "working_memory.json"
    monkeypatch.setattr("abcxauto.working_memory.WORKING_MEMORY_PATH", path)
    monkeypatch.delenv("ABCXAUTO_WORKING_MEMORY_PATH", raising=False)
    clear_working_memory()
    return path


def test_hygiene_soften_fail_f10_nameless_7496_prompt():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert get_config().ibkr_port != 7496
    assert F10_HARD_USD == 2.0
    assert new_risk_card_error("") == "new risk requires params.card naming a play"
    assert new_risk_card_error("pcs-skew") == ""


def test_note_is_grok_owned_not_stay_or_playbook(monkeypatch):
    names = _names_of(agent_tools(session="regular"))
    assert "note" in names
    assert "note" not in STAY_TOOLS
    assert "note" in DIE_TOOLS
    assert "write_lab_playbook" not in names
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")
    assert "note" not in _names_of(agent_tools(session="regular"))


def test_shape_is_one_sentence_not_a_think_dump():
    assert shape_line("Fade the SNDK gap this open.") == "Fade the SNDK gap this open."
    assert shape_line("Fade SNDK. Then chase NVDA.") == "Fade SNDK."
    assert shape_line("a" * (MAX_RAW_CHARS + 1)) == ""
    assert len(shape_line("x" * (MAX_LINE_CHARS + 40))) <= MAX_LINE_CHARS


def test_material_beat_ignores_note_chip():
    assert material_beat(["scan"]) is True
    assert material_beat(["note"]) is False
    assert material_beat([], "Fade the open gap.") is True
    assert material_beat([], "") is False
    assert material_beat([], "?") is False


def test_remember_requires_grok_line_and_material_beat(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    empty = remember("", tool_trace=["scan"], text="")
    assert empty["reason"] == "read"
    assert empty["working_memory"] == []
    refused = remember("Fade the SNDK gap.", tool_trace=[], text="")
    assert refused["reason"] == "refused_no_material"
    assert working_memory_lines() == []
    dump = remember("word " * 200, tool_trace=["scan"], text="")
    assert dump["reason"] == "refused_dump"
    assert working_memory_lines() == []
    ok = remember("Fade the SNDK gap this open.", tool_trace=["scan"], text="")
    assert ok["reason"] == "ok"
    assert working_memory_lines() == ["Fade the SNDK gap this open."]


def test_remember_caps_at_twenty_fifo(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    for i in range(MAX_LINES + 3):
        remember(f"Conclusion {i}.", tool_trace=["book"], text="")
    lines = working_memory_lines()
    assert len(lines) == MAX_LINES
    assert lines[0] == "Conclusion 3."
    assert lines[-1] == f"Conclusion {MAX_LINES + 2}."


def test_clerk_does_not_invent_from_think_text(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    think = "I think NVDA is a fade after the scan tape printed a 4% gap."
    assert working_memory_lines() == []
    remember("", tool_trace=["scan"], text=think)
    assert working_memory_lines() == []
    remember("NVDA gap is a fade this open.", tool_trace=["scan"], text=think)
    assert working_memory_lines() == ["NVDA gap is a fade this open."]


@pytest.mark.asyncio
async def test_note_tool_read_and_write(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    turn = BrainTurn(tool_trace=["scan"])
    raw = await _run_tool(
        "note",
        {"line": "Fade the SNDK gap this open."},
        connector=None,
        world=_world(),
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data["reason"] == "ok"
    assert data["working_memory"] == ["Fade the SNDK gap this open."]
    read = json.loads(
        await _run_tool(
            "note",
            {},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert read["reason"] == "read"
    assert read["working_memory"] == ["Fade the SNDK gap this open."]


def test_book_root_shard_not_world_facts(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    monkeypatch.setattr("abcxauto.universe.legal_symbols", lambda **_k: ["SPY"])
    empty = _book_payload(_world())
    assert "working_memory" not in empty.get("world", {})
    assert "working_thesis" not in empty.get("world", {})
    remember("Fade the SNDK gap this open.", tool_trace=["scan"], text="")
    blob = _book_payload(_world())
    assert blob["working_memory"] == ["Fade the SNDK gap this open."]
    assert "working_memory" not in blob["world"]


def test_last_turn_shard_not_last_look_or_wake(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    monkeypatch.setattr("abcxauto.think_stream.LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr("abcxauto.think_stream.DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    remember("Fade the SNDK gap this open.", tool_trace=["scan"], text="")
    write_last_turn({
        "strat": "skipped",
        "rationale": "watching",
        "tool_trace": ["scan"],
        "world_state": {"flat": True, "net_liquidation": 35000},
        "reality_pulse": {"ibkr_connected": True, "session": {"status": "regular"}},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["working_memory"] == ["Fade the SNDK gap this open."]
    look = last_look_facts()
    assert "working_memory" not in look
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert "working_memory" not in brief
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"open_lots": [], "mix": {}},
    )
    assert "Fade the SNDK" not in wake
    assert "working_memory" not in wake


def test_reset_chat_and_hard_reset_clear(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    remember("Fade the SNDK gap this open.", tool_trace=["scan"], text="")
    assert working_memory_lines()
    _reset_chat(SimpleNamespace())
    assert working_memory_lines() == []
    remember("Keep the IWM put credit.", tool_trace=["book"], text="")
    drop_live_chat(SimpleNamespace(chat=object()))
    assert working_memory_lines() == []
    remember("Keep the IWM put credit.", tool_trace=["book"], text="")
    g, _created = _stub_chat_client()
    g.chat = object()
    _open_wake(g, "session=regular send.", reset=True)
    assert working_memory_lines() == []


def test_research_to_rth_roll_clears_same_session_keeps(tmp_path, monkeypatch):
    _wm(tmp_path, monkeypatch)
    from abcxauto.desk_mode import session_model

    remember("Premarket: wait for the open print.", tool_trace=["scan"], text="")
    eng = ProEngine()
    chat = object()
    want = session_model("regular") or "grok-4.6"
    g = SimpleNamespace(chat=chat, model=want)
    monkeypatch.setattr(
        ProEngine,
        "_new_grok",
        lambda self, **_k: SimpleNamespace(chat=object(), model="grok-4.6"),
    )
    same = eng._apply_desk_mode_brain(g, "regular", "regular")
    assert same.chat is chat
    assert working_memory_lines() == ["Premarket: wait for the open print."]
    out = eng._apply_desk_mode_brain(g, "premarket", "regular")
    assert out is not g
    assert working_memory_lines() == []


def test_thin_scan_stays_thin_and_note_is_not_a_fact_tool():
    from abcxauto.brain import AGENT_TOOLS

    scan = None
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        if str(getattr(fn, "name", None) or "") == "scan":
            scan = str(getattr(fn, "description", "") or "")
            break
    assert scan
    assert "thin" in scan.lower()
    assert "note" in DIE_TOOLS
    assert "scan" in DIE_TOOLS
