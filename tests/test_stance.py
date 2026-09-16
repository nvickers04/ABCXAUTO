"""Durable stance: Grok-owned, survives the chat, one wake line with its age.

Shell never writes or clears it. Size is capped; age is painted, not capped.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from abcxauto.brain import _reset_chat, agent_tools
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.stance import (
    MAX_CHARS,
    clear_stance,
    format_stance_bit,
    read_stance,
    set_stance,
    stance_age,
    stance_fact,
    stance_path,
)
from abcxauto.thin_rth_kill_look import DIE_TOOLS, STAY_TOOLS
from abcxauto.world_state import day_facts, format_wake
from tests.test_brain_tools import _names_of, _run_tool, _stub_chat_client, _world
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

_T0 = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class _W:
    net_liquidation = 10_000.0
    positions = []
    open_orders = []
    fills = []
    daily_pnl = 0.0


def test_conftest_points_the_stance_file_away_from_the_live_desk(tmp_path):
    assert stance_path().parent == tmp_path
    assert read_stance() == {}


def test_set_read_round_trip_with_timestamp_and_session():
    out = set_stance("IWM verticals pay; SNDK singles do not.", session="regular", now=_T0)
    assert out["reason"] == "ok"
    row = read_stance()
    assert row["text"] == "IWM verticals pay; SNDK singles do not."
    assert row["set_at"] == "2026-09-14T13:00:00+00:00"
    assert row["session"] == "regular"
    on_disk = json.loads(stance_path().read_text(encoding="utf-8"))
    assert on_disk["text"] == row["text"]
    assert not stance_path().with_suffix(".json.tmp").exists()


def test_whitespace_is_collapsed_and_a_repeat_is_a_duplicate():
    set_stance("  keep\n  premium\r\n short  ", now=_T0)
    assert read_stance()["text"] == "keep premium short"
    assert set_stance("keep premium  short", now=_T0)["reason"] == "duplicate"


def test_cap_refuses_with_the_length_and_leaves_the_file_alone():
    set_stance("first", now=_T0)
    over = "x" * (MAX_CHARS + 1)
    out = set_stance(over, now=_T0)
    assert out["reason"] == "refused_too_long"
    assert out["len"] == MAX_CHARS + 1
    assert out["max_chars"] == MAX_CHARS == 600
    assert read_stance()["text"] == "first"
    assert set_stance("y" * MAX_CHARS, now=_T0)["reason"] == "ok"
    assert len(read_stance()["text"]) == MAX_CHARS


def test_hand_edited_oversize_file_is_clipped_on_read_not_dropped():
    stance_path().parent.mkdir(parents=True, exist_ok=True)
    stance_path().write_text(
        json.dumps({"text": "z" * 900, "set_at": _T0.isoformat()}), encoding="utf-8"
    )
    assert len(read_stance()["text"]) == MAX_CHARS


def test_broken_or_empty_file_is_no_stance():
    stance_path().parent.mkdir(parents=True, exist_ok=True)
    stance_path().write_text("{not json", encoding="utf-8")
    assert read_stance() == {}
    assert stance_fact() == {}
    assert format_stance_bit(stance_fact()) == ""
    stance_path().write_text(json.dumps({"text": "   "}), encoding="utf-8")
    assert read_stance() == {}


def test_empty_text_is_a_read_and_clear_drops_the_file():
    set_stance("hold the line", now=_T0)
    assert set_stance("", now=_T0)["reason"] == "read"
    assert set_stance("", now=_T0)["stance"] == "hold the line"
    out = clear_stance()
    assert out["reason"] == "cleared"
    assert out["stance"] == ""
    assert not stance_path().exists()


def test_age_reads_as_minutes_hours_then_days():
    stamp = _T0.isoformat()
    assert stance_age(stamp, now=_T0 + timedelta(minutes=12)) == "12m"
    assert stance_age(stamp, now=_T0 + timedelta(hours=5, minutes=30)) == "5h"
    assert stance_age(stamp, now=_T0 + timedelta(hours=47)) == "47h"
    assert stance_age(stamp, now=_T0 + timedelta(days=3, hours=2)) == "3d"
    assert stance_age("garbage", now=_T0) == ""
    assert stance_age("", now=_T0) == ""


def test_stance_fact_carries_text_stamp_and_age():
    set_stance("fade SNDK gaps", now=_T0)
    fact = stance_fact(now=_T0 + timedelta(hours=3))
    assert fact == {"text": "fade SNDK gaps", "set_at": _T0.isoformat(), "age": "3h"}
    assert format_stance_bit(fact) == "stance(3h)=fade SNDK gaps"
    assert format_stance_bit({"text": "no stamp"}) == "stance=no stamp"


def test_day_facts_carries_the_stance_only_when_one_is_set():
    assert "stance" not in day_facts(_W(), None)
    set_stance("IWM verticals only.", session="regular", now=_T0)
    d = day_facts(_W(), None)
    assert d["stance"]["text"] == "IWM verticals only."
    assert d["stance"]["set_at"] == _T0.isoformat()
    assert d["stance"]["age"]
    # the open-book key still rides beside it
    assert "open_lots" in d


def _wake(day):
    return format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day=day,
    )


def test_wake_paints_one_stance_line_after_open_lots_with_its_age():
    base = {"names": 0, "lots": 0, "max_risk_per_trade_pct": 20.0}
    quiet = _wake(base)
    assert "stance" not in quiet
    day = dict(base)
    day["open_lots"] = ["IWM 250P/245P vertical x2"]
    day["stance"] = {"text": "IWM verticals pay; singles do not", "set_at": _T0.isoformat(), "age": "2d"}
    text = _wake(day)
    assert "open_lots=IWM 250P/245P vertical x2. stance(2d)=IWM verticals pay; singles do not." in text
    assert text.count("stance(") == 1
    # a stance that already ends in a period is not double-stopped
    day["stance"]["text"] = "Hold premium short."
    assert "stance(2d)=Hold premium short. " in _wake(day) + " "
    assert "short.." not in _wake(day)


def test_wake_line_is_bounded_by_the_cap():
    day = {
        "names": 0,
        "lots": 0,
        "max_risk_per_trade_pct": 20.0,
        "stance": {"text": "s" * MAX_CHARS, "set_at": _T0.isoformat(), "age": "1h"},
    }
    line = [p for p in _wake(day).split(" ") if p.startswith("stance(")][0]
    assert len(line) <= MAX_CHARS + len("stance(1h)=.")


def test_stance_is_a_tool_grok_owns_not_a_prompt_lecture():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert "stance" not in SYSTEM_PROMPT.lower()
    names = _names_of(agent_tools(session="regular"))
    assert "stance" in names
    assert "stance" in _names_of(agent_tools(session="premarket"))
    assert "stance" not in STAY_TOOLS
    assert "stance" in DIE_TOOLS


@pytest.mark.asyncio
async def test_stance_tool_set_read_clear_through_the_tool_path():
    from abcxauto.brain import BrainTurn

    async def call(args):
        raw = await _run_tool(
            "stance", args, connector=None, world=_world(), snap={}, turn=BrainTurn()
        )
        return json.loads(raw)

    out = await call({"text": "Stay defined-risk; premium sells beat directional singles."})
    assert out["reason"] == "ok"
    assert out["stance"] == "Stay defined-risk; premium sells beat directional singles."
    assert out["set_at"]
    assert read_stance()["session"] == "regular"
    read = await call({})
    assert read["reason"] == "read"
    assert read["stance"] == out["stance"]
    assert read["max_chars"] == MAX_CHARS
    refused = await call({"text": "q" * (MAX_CHARS + 50)})
    assert refused["reason"] == "refused_too_long"
    assert read_stance()["text"] == out["stance"]
    gone = await call({"clear": True})
    assert gone["reason"] == "cleared"
    assert read_stance() == {}


def test_chat_reset_drops_working_memory_but_never_the_stance(tmp_path, monkeypatch):
    from abcxauto.working_memory import remember, working_memory_lines

    monkeypatch.setenv("ABCXAUTO_WORKING_MEMORY_PATH", str(tmp_path / "wm.json"))
    remember("This-flight note.", tool_trace=["scan"])
    assert working_memory_lines() == ["This-flight note."]
    set_stance("durable conclusion", now=_T0)
    g, _created = _stub_chat_client()
    from abcxauto.brain import _ensure_chat

    _ensure_chat(g, kind="boot")
    _reset_chat(g)
    assert working_memory_lines() == []
    assert read_stance()["text"] == "durable conclusion"
