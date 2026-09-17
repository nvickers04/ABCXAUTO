"""Durable journal notes: fetch-only, park-safe, never send geometry."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from abcxauto.brain import AGENT_TOOLS, BrainTurn, _book_payload, _reset_chat, agent_tools, drop_live_chat
from abcxauto.brain_tools import _run_tool
from abcxauto.thin_rth_kill_look import F10_HARD_USD
from abcxauto.desk_mode import (
    load_research_brief,
    research_brief_stale,
    rth_research_color,
    write_research_brief,
)
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.look_snapshot import REASON_CODE, begin_look, check_ticket_numbers, record_look_tool
from abcxauto.memory import get_journal
from abcxauto.memory.notes import MAX_BODY, lecture_error
from abcxauto.working_memory import remember, working_memory_lines
from abcxauto.world_state import WorldState, day_facts, format_wake
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


# Billed JSON schema of name+description+parameters. chars/4 ~ tokens.
# Before trim: recall 1027/257, research_brief 237/60.
RECALL_SCHEMA_MAX_CHARS = 700
RESEARCH_BRIEF_SCHEMA_MAX_CHARS = 200

def _tool_schema_json(name: str) -> str:
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        if str(getattr(fn, "name", None) or "") != name:
            continue
        blob = {
            "type": "function",
            "function": {
                "name": fn.name,
                "description": fn.description,
                "parameters": fn.parameters,
            },
        }
        return json.dumps(blob, separators=(",", ":"), sort_keys=True)
    raise AssertionError(f"missing tool {name}")


def _names_of(tools) -> set[str]:
    names = set()
    for t in tools:
        fn = getattr(t, "function", None)
        names.add(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    return names


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
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _write(body="book_unreliable=true while lots still existed", **kwargs):
    return get_journal().write_note(body=body, **kwargs)


def test_system_prompt_still_byte_locked():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_recall_exists_write_desk_lessons_does_not():
    names = _names_of(AGENT_TOOLS)
    assert "recall" in names
    assert "research_brief" in names
    assert "note" in names
    assert "write_desk_lessons" not in names
    assert "desk_lessons" not in names


def test_imperative_body_is_rejected():
    for line in (
        "Always flatten when NL is 0",
        "Never treat nl=0 as a wipe",
        "Do not send on book_unreliable",
        "You must wait for a nonzero NL",
        "Ban zero NL sizing",
    ):
        assert lecture_error(line) == "imperative_body", line
        out = _write(line, id="lecture")
        assert out.get("ok") is False
        assert out.get("error") == "imperative_body"
    ok = _write(
        "book() returned nl=0 with book_unreliable=true while lots existed",
        id="obs-nl",
        tags=["nl", "gate"],
        evidence="book_unreliable=true",
        invalidate="book_unreliable=false and NL>0",
    )
    assert ok.get("ok") is True
    assert lecture_error("book() returned nl=0 with book_unreliable=true") == ""


def test_kind_rule_is_rejected():
    out = _write("a fact with evidence", id="rule-no", kind="rule")
    assert out.get("ok") is False
    assert out.get("error") == "kind_rule_forbidden"


def test_note_survives_park_and_chat_reset():
    body = "halt disconnect cleared on reconnect this morning"
    out = _write(body, id="park-survives", tags=["halt"], evidence="halts.kind=disconnect")
    assert out.get("ok") is True
    remember("this-flight scratch dies on park", tool_trace=["scan"])
    assert working_memory_lines()
    _reset_chat(SimpleNamespace())
    assert working_memory_lines() == []
    remember("scratch again", tool_trace=["scan"])
    drop_live_chat(SimpleNamespace(chat=object()))
    assert working_memory_lines() == []
    got = get_journal().get_notes(ids=["park-survives"])
    rows = got.get("notes") or []
    assert rows and rows[0]["body"] == body
    assert rows[0]["status"] == "live"


def test_wake_pointer_has_counts_not_bodies():
    body = "unique_pointer_body_unreliable_zero_nl_xyz"
    _write(body, id="ptr-nl", tags=["nl", "gate"], evidence="snap")
    _write("halt disconnect printed twice", id="ptr-halt", tags=["halt"])
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": False},
    )
    ptr = get_journal().notes_pointer()
    assert ptr.startswith("notes=")
    assert "tags=" in ptr
    assert "age=" in ptr
    assert body not in ptr
    assert body not in wake
    assert ptr in wake
    assert len(ptr) <= 80
    assert (len(ptr) + 3) // 4 <= 25


def test_book_status_day_facts_prompt_have_no_notes(monkeypatch):
    body = "unique_antidiary_body_unreliable_zero_nl_xyz"
    _write(body, id="anti-diary", tags=["nl"])
    world = _world()
    book = _book_payload(world, snap={})
    day = day_facts(world, {})
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={},
    )
    assert body not in json.dumps(book, default=str)
    assert body not in json.dumps(day, default=str)
    assert body not in wake
    assert body not in SYSTEM_PROMPT
    assert "notes" not in book
    assert "recall" not in book
    from abcxauto.brain import brain_system_prompt

    assert body not in brain_system_prompt()


@pytest.mark.asyncio
async def test_status_tool_omits_note_bodies(monkeypatch):
    body = "unique_status_antidiary_nl_unknown_zzz"
    _write(body, id="status-diary", tags=["nl"])
    monkeypatch.setattr(
        "abcxauto.connections.connection_status",
        lambda *_a, **_k: {"ibkr": "up", "mda": "up"},
    )
    raw = await _run_tool(
        "status",
        {},
        connector=SimpleNamespace(connected=True),
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    assert body not in raw
    assert "unique_status_antidiary" not in raw


@pytest.mark.asyncio
async def test_status_and_book_tools_omit_note_bodies():
    body = "unique_tool_antidiary_nl_unknown_abc"
    _write(body, id="tool-diary", tags=["nl"])
    world = _world()
    book_raw = await _run_tool(
        "book",
        {},
        connector=None,
        world=world,
        snap={},
        turn=BrainTurn(),
    )
    assert body not in book_raw


def test_expiry_and_invalidate_leave_pointer():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    _write(
        "gate refused book_unreliable on a live send",
        id="exp-me",
        tags=["gate"],
        now=now,
    )
    _write(
        "fill AAPL BUY 10 @ 12.3",
        id="kill-me",
        tags=["fill"],
        now=now,
    )
    listed = get_journal().list_notes(now=now)
    assert "exp-me" in listed["ids"]
    assert "kill-me" in listed["ids"]
    gone = get_journal().invalidate_note("kill-me", evidence="position closed", now=now)
    assert gone.get("ok") is True
    assert gone["note"]["status"] == "invalidated"
    later = now + timedelta(days=14, seconds=1)
    listed2 = get_journal().list_notes(now=later)
    assert "exp-me" not in listed2["ids"]
    assert "kill-me" not in listed2["ids"]
    got = get_journal().get_notes(ids=["exp-me", "kill-me"], now=later)
    by_id = {r["id"]: r for r in got["notes"]}
    assert by_id["exp-me"]["status"] == "expired"
    assert by_id["kill-me"]["status"] == "invalidated"
    ptr = get_journal().notes_pointer(now=later)
    assert ptr == ""
    still = get_journal().list_notes(now=now + timedelta(days=13))
    assert "exp-me" in still["ids"]


def test_code_notes_capped_per_reason_code_per_day():
    j = get_journal()
    for i in range(6):
        out = j.record_code_note(
            source="halt",
            reason_code="disconnect",
            body=f"halt disconnect print {i}",
            tags=["halt"],
        )
        assert out.get("ok") is True
    live = j.list_notes()
    assert live["n"] == 1
    other = j.record_code_note(
        source="halt",
        reason_code="daily_loss",
        body="halt daily_loss tripped",
        tags=["halt"],
    )
    assert other.get("ok") is True
    assert j.list_notes()["n"] == 2
    gate_a = j.record_gate_decision(None, False, "book_unreliable: snap incomplete")
    gate_b = j.record_gate_decision(None, False, "book_unreliable: snap incomplete again")
    _ = gate_a, gate_b
    codes = {
        r.get("reason_code")
        for r in j.get_notes(tags=["gate"]).get("notes") or []
        if r.get("status") == "live"
    }
    assert "book_unreliable" in codes


def test_note_number_is_not_send_geometry():
    body = "AAPL last 123.45 only exists inside this note"
    _write(body, id="px-note", tags=["aapl"])
    got = get_journal().get_notes(ids=["px-note"])
    assert "123.45" in json.dumps(got)
    snap: dict = {}
    begin_look(snap)
    record_look_tool(snap, "recall", got)
    ok, code, msg = check_ticket_numbers(
        "bracket",
        {
            "symbol": "AAPL",
            "quantity": 1,
            "direction": "LONG",
            "entry_price": 123.45,
            "last": 123.45,
            "stop_price": 99.0,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE == "stale_or_invented_number"
    assert "123.45" in msg


@pytest.mark.asyncio
async def test_recall_list_get_write_invalidate_separated_from_note():
    remember("this-flight only", tool_trace=["scan"])
    raw = await _run_tool(
        "recall",
        {
            "op": "write",
            "id": "sep-1",
            "body": "scan overflow dropped ranked hits this look",
            "tags": ["scan"],
            "evidence": "scan",
            "invalidate": "full tape returned",
        },
        connector=None,
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    wrote = json.loads(raw)
    assert wrote.get("ok") is True
    assert wrote.get("store") == "notes"
    assert wrote.get("send_geometry") is False
    listed = json.loads(
        await _run_tool(
            "recall",
            {"op": "list"},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert "sep-1" in listed.get("ids") or []
    assert "body" not in listed
    assert "this-flight only" not in json.dumps(listed)
    got = json.loads(
        await _run_tool(
            "recall",
            {"op": "get", "ids": ["sep-1"]},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert got["notes"][0]["body"].startswith("scan overflow")
    note_read = json.loads(
        await _run_tool(
            "note",
            {},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert "this-flight only" in (note_read.get("working_memory") or [])
    assert "scan overflow" not in json.dumps(note_read)
    inv = json.loads(
        await _run_tool(
            "recall",
            {"op": "invalidate", "id": "sep-1", "evidence": "tape returned"},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert inv["note"]["status"] == "invalidated"


def test_first_rth_wake_is_brief_pointer_not_full_expectancy(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    write_research_brief(
        session="premarket",
        snap={
            "news_items": [
                {
                    "symbol": "AMD",
                    "headline": "AMD raises guidance after hours",
                    "publisher": "MDA",
                }
            ]
        },
        now=datetime.now(timezone.utc),
    )
    pointer = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": False},
    )
    assert "desk_mode=rth" in pointer
    assert "send=allowed" in pointer
    assert "on_disk" in pointer
    assert "expectancy=" in pointer
    assert "AMD raises guidance after hours" not in pointer
    color = rth_research_color(full=False)
    assert "on_disk" in color
    assert "age=" in color
    full = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "AMD" in full


def test_stale_or_missing_brief_does_not_block_rth(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    missing = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={},
    )
    assert "prior_session_research=missing" in missing
    assert "desk_mode=rth" in missing
    assert "send=allowed" in missing
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    write_research_brief(
        session="premarket",
        snap={"news_items": [{"symbol": "OLD", "headline": "OLD announces merger"}]},
        now=old,
    )
    brief = load_research_brief()
    assert research_brief_stale(brief) is True
    stale = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={},
    )
    assert "stale" in stale
    assert "send=allowed" in stale
    assert "desk_mode=rth" in stale


@pytest.mark.asyncio
async def test_research_brief_tool_is_fetch_only_not_geometry(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    write_research_brief(
        session="premarket",
        snap={"news_items": [{"symbol": "NVDA", "headline": "NVDA beats estimates after hours"}]},
    )
    raw = await _run_tool(
        "research_brief",
        {},
        connector=None,
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    data = json.loads(raw)
    assert data.get("send_geometry") is False
    assert data.get("use", "").startswith("color")
    snap: dict = {}
    begin_look(snap)
    record_look_tool(snap, "research_brief", data)
    ok, code, _msg = check_ticket_numbers(
        "bracket",
        {"symbol": "NVDA", "last": 1.23, "entry_price": 1.23, "stop_price": 1.0, "quantity": 1, "direction": "LONG"},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE


def test_body_cap_and_fifo_eviction():
    assert _write("x" * (MAX_BODY + 1), id="too-long").get("error") == "body_too_long"
    j = get_journal()
    for i in range(40):
        out = j.write_note(id=f"fifo-{i:02d}", body=f"observation {i} with evidence on the tape")
        assert out.get("ok") is True
    live = j.list_notes()
    assert live["n"] == 32
    assert "fifo-00" not in live["ids"]
    assert "fifo-39" in live["ids"]
    old = j.get_notes(ids=["fifo-00"])
    assert old["notes"][0]["status"] == "expired"


def test_ingest_look_records_book_unreliable_once():
    j = get_journal()
    snap = {"book_unreliable": True, "net_liquidation": None, "fills": []}
    j.ingest_look(snap)
    j.ingest_look(snap)
    tags = j.get_notes(tags=["nl"])
    live = [r for r in tags.get("notes") or [] if r.get("status") == "live"]
    codes = {r.get("reason_code") for r in live}
    assert "book_unreliable" in codes or any("book_unreliable" in (r.get("body") or "") for r in live)
    assert "nl_unknown" in codes or any("NL unknown" in (r.get("body") or "") for r in live)
    assert len(live) <= 4


def test_recall_and_brief_are_on_rth_with_hunt_tools(monkeypatch):
    """STAY/DIE split is gone: recall, brief, and hunt tools all offered on RTH."""
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")
    rth = _names_of(agent_tools(session="regular"))
    assert "recall" in rth
    assert "research_brief" in rth
    assert "note" in rth
    assert "scan" in rth
    assert "news" in rth
    assert "send" in rth
    assert F10_HARD_USD == 15.0


@pytest.mark.asyncio
async def test_fetch_notes_and_brief_during_rth_kill_look(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    body = "book_unreliable printed while lots still existed"
    _write(body, id="rth-fetch", tags=["nl"])
    world = _world(session_status="regular")
    listed = json.loads(
        await _run_tool(
            "recall",
            {"op": "list"},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert listed.get("error") is None
    assert "rth-fetch" in (listed.get("ids") or [])
    got = json.loads(
        await _run_tool(
            "recall",
            {"op": "get", "ids": ["rth-fetch"]},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert body in json.dumps(got)
    missing = json.loads(
        await _run_tool(
            "research_brief",
            {},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert missing.get("error") is None
    assert missing.get("missing") is True
    write_research_brief(
        session="premarket",
        snap={"news_items": [{"symbol": "AMD", "headline": "AMD raises guidance after hours"}]},
        now=datetime.now(timezone.utc) - timedelta(hours=30),
    )
    stale = json.loads(
        await _run_tool(
            "research_brief",
            {},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert stale.get("stale") is True
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={},
    )
    assert "stale" in wake
    assert "send=allowed" in wake
    assert "desk_mode=rth" in wake


def test_recall_and_brief_schema_stay_under_budget():
    recall = _tool_schema_json("recall")
    brief = _tool_schema_json("research_brief")
    assert len(recall) <= RECALL_SCHEMA_MAX_CHARS
    assert len(brief) <= RESEARCH_BRIEF_SCHEMA_MAX_CHARS
    assert "e.g." not in recall
    assert "e.g." not in brief
    assert "Never" not in recall
    assert "law" not in recall.lower()


def test_cards_wake_pointer_stays_inside_budget():
    from abcxauto.memory.cards import MAX_POINTER_CHARS, MAX_POINTER_TOKENS

    j = get_journal()
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    for i in range(8):
        out = j.write_card(
            label=f"longlabelplay{i:02d}",
            screen="top_gainers",
            scan_code="TOP_PERC_GAIN",
            evidence=[{"tool": "scan", "facts": {"rank": i}}],
            direction="long",
            expectation="unique_card_expectancy_xyz",
            invalidate="fade",
            now=now,
        )
        assert out.get("ok") is True
    ptr = j.cards_pointer(now=now)
    assert ptr.startswith("cards=8")
    assert "longlabelplay" in ptr
    assert len(ptr) <= MAX_POINTER_CHARS
    assert (len(ptr) + 3) // 4 <= MAX_POINTER_TOKENS
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": False},
    )
    assert "cards=8" in wake
    assert "longlabelplay" in wake
    assert "unique_card_expectancy_xyz" not in wake
    notes_ptr = j.notes_pointer(now=now)
    if notes_ptr:
        assert notes_ptr in wake


@pytest.mark.asyncio
async def test_recall_list_advertises_cards_and_store_cards_round_trips():
    raw = await _run_tool(
        "recall",
        {
            "op": "write",
            "store": "cards",
            "label": "amd-iv",
            "screen": "hot_by_option_volume",
            "scan_code": "HOT_BY_OPT_VOLUME",
            "evidence": [{"tool": "option_quote", "facts": {"iv": 0.42}}],
            "direction": "long",
            "expectation": "iv crush fades",
            "invalidate": "iv > 0.6",
        },
        connector=None,
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    wrote = json.loads(raw)
    assert wrote.get("ok") is True
    assert wrote.get("store") == "cards"
    listed = json.loads(
        await _run_tool(
            "recall",
            {"op": "list"},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert listed.get("cards_n") == 1
    assert "amd-iv" in (listed.get("cards_ids") or [])
    assert "cards=" in (listed.get("cards_pointer") or "")
    got = json.loads(
        await _run_tool(
            "recall",
            {"op": "get", "store": "cards", "ids": ["amd-iv"]},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert got["cards"][0]["label"] == "amd-iv"
    assert got["cards"][0]["evidence"][0]["tool"] == "option_quote"
    inv = json.loads(
        await _run_tool(
            "recall",
            {"op": "invalidate", "store": "cards", "id": "amd-iv", "evidence": "iv 0.7"},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert inv["card"]["status"] == "inert"
    listed2 = json.loads(
        await _run_tool(
            "recall",
            {"op": "list", "store": "cards"},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert listed2.get("n") == 0
