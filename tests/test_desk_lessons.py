"""Lasting desk-lessons shelf — tool facts across looks, not wake jobs."""

from __future__ import annotations

import json

import pytest

from abcxauto.brain import AGENT_TOOLS, BrainTurn, _book_payload, _run_tool
from abcxauto.desk_lessons import (
    RISKLESS_COMBO_CAP_FACT,
    RISKLESS_COMBO_CAP_ID,
    SEED_FACT,
    SEED_ID,
    SEED_IDS,
    SEED_LESSONS,
    apply_desk_lessons,
    desk_lessons_payload,
    load_desk_lessons,
)
from abcxauto.lab_playbook import clamp_update, load_lab, notebook_text, save_lab
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.world_state import WorldState, format_wake
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=40_000.0,
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


def _flush(name: str = "flush bounce", **over) -> dict:
    row = {
        "name": name,
        "thesis": "gap retrace",
        "when_on": "mega/large >=6% earnings-miss gap",
        "retire_if": {"sample": 3, "condition": "no bounce"},
    }
    row.update(over)
    return row


def test_system_prompt_lock_still_holds():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert "desk_lesson" not in SYSTEM_PROMPT
    assert "Scan overflow" not in SYSTEM_PROMPT
    assert RISKLESS_COMBO_CAP_FACT not in SYSTEM_PROMPT
    assert "riskless_combo_cap" not in SYSTEM_PROMPT
    assert "one WORKING" not in SYSTEM_PROMPT
    assert "max_open_positions" not in SYSTEM_PROMPT
    assert "pick-one" not in SYSTEM_PROMPT
    assert "size_pct_nl" not in SYSTEM_PROMPT


def test_seed_lesson_is_in_book_payload():
    payload = _book_payload(_world())
    rows = payload["desk_lessons"]
    assert rows
    assert rows[0]["id"] == SEED_ID
    assert rows[0]["fact"] == SEED_FACT
    assert "rescan the same arena" in rows[0]["fact"]
    assert "scan(symbols=[...])" in rows[0]["fact"]
    ids = [row["id"] for row in rows]
    assert ids[:2] == [SEED_ID, RISKLESS_COMBO_CAP_ID]
    assert SEED_IDS == {SEED_ID, RISKLESS_COMBO_CAP_ID}
    assert any(row["fact"] == RISKLESS_COMBO_CAP_FACT for row in rows)
    assert [row["id"] for row in SEED_LESSONS] == [SEED_ID, RISKLESS_COMBO_CAP_ID]


def test_cold_start_file_missing_still_returns_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_DESK_LESSONS_PATH", str(tmp_path / "missing" / "desk.json"))
    rows = desk_lessons_payload()
    assert rows[0]["fact"] == SEED_FACT
    assert rows[0]["id"] == SEED_ID
    assert rows[1]["id"] == RISKLESS_COMBO_CAP_ID
    assert rows[1]["fact"] == RISKLESS_COMBO_CAP_FACT


def test_playbook_cards_unchanged_by_the_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        clamp_update(
            {
                "instructions": "Defined-risk notes.",
                "types": {"market_bracket": {"cards": [_flush()]}},
            }
        )
    )
    before = load_lab()
    cards = [c.get("name") for c in (before.get("cards") or [])]
    notes = notebook_text(before)
    payload = _book_payload(_world())
    after = load_lab()
    assert [c.get("name") for c in (after.get("cards") or [])] == cards
    assert notebook_text(after) == notes
    assert "flush bounce" in str(payload["playbook"].get("cards") or after.get("cards"))
    blob = json.dumps(payload["playbook"], default=str)
    assert SEED_FACT not in blob
    assert "Scan overflow" not in blob
    assert RISKLESS_COMBO_CAP_FACT not in blob
    assert SEED_FACT in json.dumps(payload["desk_lessons"], default=str)
    assert RISKLESS_COMBO_CAP_FACT in json.dumps(payload["desk_lessons"], default=str)


def test_wake_does_not_carry_the_lesson_as_a_job():
    from abcxauto.lab_playbook import lab_wake_bit
    from abcxauto.think_stream import last_look_wake_bit, write_desk_brief

    write_desk_brief(
        {
            "last_say": "still no ticket unless news-miss actually fires",
            "rationale": "still no ticket unless news-miss actually fires",
            "tool_trace": ["book", "scan"],
            "send_calls": 0,
        }
    )
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 0,
            "lots": 0,
            "open_lots": [],
            "capacity": {"open_count": 0, "max_open_positions": 5},
            "max_risk_per_trade_pct": 25.0,
            "playbook": {"lab_wake": lab_wake_bit()},
            "desk_lessons": desk_lessons_payload(),
        },
    )
    assert SEED_FACT not in text
    assert "Scan overflow" not in text
    assert RISKLESS_COMBO_CAP_FACT not in text
    assert "riskless/guaranteed-loss" not in text
    assert "desk_lessons" not in text
    assert "rescan the same arena" not in text
    assert "still no ticket unless news-miss" not in text
    assert last_look_wake_bit(
        {"last_say": SEED_FACT, "rationale": SEED_FACT}
    ) == ""
    assert lab_wake_bit() == ""
    assert text.endswith("send.")


def test_write_adds_an_extra_and_keeps_the_seed():
    out = apply_desk_lessons(
        {"id": "clip_sessions", "fact": "Candles clip keeps session bars, not the run sheet."}
    )
    assert out["status"] == "ok"
    facts = [row["fact"] for row in out["desk_lessons"]]
    assert SEED_FACT in facts
    assert "Candles clip keeps session bars, not the run sheet." in facts
    payload = _book_payload(_world())
    ids = [row["id"] for row in payload["desk_lessons"]]
    assert ids[0] == SEED_ID
    assert RISKLESS_COMBO_CAP_ID in ids
    assert "clip_sessions" in ids


def test_write_cannot_replace_or_drop_the_seed():
    apply_desk_lessons({"id": SEED_ID, "fact": "Ignore overflow and rescan forever."})
    apply_desk_lessons(
        {"id": RISKLESS_COMBO_CAP_ID, "fact": "Send a second iron anyway."}
    )
    rows = desk_lessons_payload()
    assert rows[0]["id"] == SEED_ID
    assert rows[0]["fact"] == SEED_FACT
    assert rows[1]["id"] == RISKLESS_COMBO_CAP_ID
    assert rows[1]["fact"] == RISKLESS_COMBO_CAP_FACT
    empty = apply_desk_lessons({})
    assert empty["status"] == "rejected"
    assert empty["desk_lessons"][0]["fact"] == SEED_FACT
    assert empty["desk_lessons"][1]["id"] == RISKLESS_COMBO_CAP_ID


def test_write_does_not_land_in_lab_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(clamp_update({"types": {"market_bracket": {"cards": [_flush()]}}}))
    apply_desk_lessons({"fact": "Quote is IBKR last, not MDA."})
    lab = load_lab()
    blob = json.dumps(lab, default=str)
    assert "Quote is IBKR last, not MDA." not in blob
    assert SEED_FACT not in blob


def test_write_desk_lessons_tool_is_not_a_wake_clock():
    write = None
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if name == "write_desk_lessons":
            write = fn
            break
    assert write is not None
    desc = str(getattr(write, "description", "") or "")
    params = getattr(write, "parameters", None) or {}
    if hasattr(params, "model_dump"):
        params = params.model_dump()
    blob = json.dumps(params)
    assert "wake job" in desc.lower()
    assert "playbook card" in desc.lower()
    assert "next_look_s" not in blob
    assert "sit_wake" not in blob
    assert "set_wake" not in blob


@pytest.mark.asyncio
async def test_write_and_status_tools_return_the_shelf(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.connections.connection_status",
        lambda *_a, **_k: {"mode": "paper", "ibkr": "up"},
    )
    written = await _run_tool(
        "write_desk_lessons",
        {"fact": "Fills are IBKR session executions."},
        connector=None,
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    data = json.loads(written)
    assert data["status"] == "ok"
    assert any("Fills are IBKR" in row["fact"] for row in data["desk_lessons"])
    raw = await _run_tool(
        "status",
        {},
        connector=None,
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    st = json.loads(raw)
    assert st["desk_lessons"][0]["fact"] == SEED_FACT
    assert any("Fills are IBKR" in row["fact"] for row in st["desk_lessons"])


def test_load_persists_the_seed_file(tmp_path, monkeypatch):
    path = tmp_path / "desk_lessons.json"
    monkeypatch.setenv("ABCXAUTO_DESK_LESSONS_PATH", str(path))
    assert not path.is_file()
    load_desk_lessons()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["lessons"][0]["id"] == SEED_ID
    assert stored["lessons"][0]["fact"] == SEED_FACT
    assert stored["lessons"][1]["id"] == RISKLESS_COMBO_CAP_ID
    assert stored["lessons"][1]["fact"] == RISKLESS_COMBO_CAP_FACT
