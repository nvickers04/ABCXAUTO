"""Grok surfaces are not a look-assignment engine.

Unused starter type names may appear. Look tallies, next= tool hops,
playbook rev= openers, and hunt send sketches do not.
"""

from __future__ import annotations

import json

from abcxauto.brain import AGENT_TOOLS, _book_payload
from abcxauto.lab_playbook import (
    apply_from_judgment,
    apply_hunt_send_sketch,
    clamp_update,
    lab_facts,
    lab_wake_bit,
    live_card_book_error,
    live_card_session_error,
    live_card_tape_error,
    load_lab,
    playbook_payload,
    playbook_run_sheets,
    record_card_send,
    save_lab,
    unused_open_types,
)
from abcxauto.park_clock import note_wake
from abcxauto.think_stream import last_look_wake_bit, write_desk_brief
from abcxauto.world_state import WorldState, format_wake


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


def test_unused_type_names_survive_a_flush_send_and_carry_no_look_tally(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "types": {
                    "market_bracket": {"cards": [_flush()]},
                    "buy_option": {
                        "cards": [
                            {
                                **_flush("premium spray"),
                                "status": "retired",
                            }
                        ]
                    },
                }
            }
        )
    )
    lab = load_lab()
    unused = unused_open_types(lab)
    assert "market_bracket" in unused
    assert "bracket" in unused
    assert "vertical_spread" in unused
    bit = lab_wake_bit(lab)
    assert bit.startswith("unused=")
    assert "bracket" in bit
    assert "looks" not in bit
    assert "0sends" not in bit
    assert "next=" not in bit
    assert "Nlooks" not in bit
    sheets = playbook_run_sheets(lab, flat=True)
    names = [row["card"] for row in sheets]
    assert "flush bounce" in names
    assert "premium spray" not in names
    assert any(row.get("locked") is True for row in sheets)
    for row in sheets:
        assert "next" not in row
        assert "send" not in row
        assert "gate" not in row

    record_card_send(card="flush bounce", strategy="market_bracket", symbol="SNDK")
    after = unused_open_types()
    assert "market_bracket" not in after
    assert "bracket" in after
    after_bit = lab_wake_bit()
    assert "market_bracket" not in after_bit
    assert "bracket" in after_bit
    assert "looks" not in after_bit


def test_wake_book_playbook_have_no_assignment_paint(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "instructions": "Defined-risk notes.",
                "types": {"market_bracket": {"cards": [_flush()]}},
            }
        )
    )
    monkeypatch.setenv("ABCXAUTO_DESK_BRIEF_PATH", str(tmp_path / "desk_brief.json"))
    write_desk_brief(
        {
            "rationale": "SNDK still holding the opening low - size the bracket",
            "tool_trace": ["book", "scan", "news"],
            "send_calls": 0,
            "scan_hits": {
                "rows": [{"symbol": "SNDK", "open_gap_pct": -6.5}],
            },
        }
    )
    note_wake(None)
    text = format_wake(
        cycle=4,
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
            "playbook": {"lab_wake": lab_wake_bit(load_lab())},
        },
    )
    assert "SNDK still holding the opening low" in text
    assert "Next look:" not in text
    assert last_look_wake_bit(
        {"last_say": "hold the opening low and send the bracket"}
    ) == "hold the opening low and send the bracket"
    assert "loser screens" not in text
    assert "playbook rev=" not in text
    assert "next=" not in text
    assert "Nlooks" not in text
    assert "0sends" not in text
    assert "last_scan" not in text
    assert "send SNDK" not in text
    assert last_look_wake_bit(
        {"rationale": "Next look: same mega/large loser screens + news first."}
    ) == ""
    pb = _book_payload(_world(), tool_trace=["book"])["playbook"]
    assert "run" not in pb
    payload = playbook_payload()
    assert "run" not in payload
    assert "TYPE market_bracket" in payload["tree"]
    assert "flush bounce" in payload["tree"]
    awaiting = payload["lab"]["cards_awaiting_first_trade"]
    assert awaiting
    for row in awaiting:
        assert set(row) == {"card"}
    assert apply_from_judgment(
        {
            "lab_playbook": {
                "types": {
                    "market_bracket": {
                        "cards": [_flush(thesis="gap retrace still holds")]
                    }
                }
            }
        }
    ) is not None
    assert "gap retrace still holds" in playbook_payload()["tree"]


def test_playbook_and_write_stay_in_agent_tools_without_cadence_fields():
    names = set()
    write_blob = ""
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        names.add(name)
        if name == "write_lab_playbook":
            params = getattr(fn, "parameters", None) or {}
            if hasattr(params, "model_dump"):
                params = params.model_dump()
            write_blob = json.dumps(params)
    assert "playbook" in names
    assert "write_lab_playbook" in names
    assert "next_look_s" not in write_blob
    assert "max_looks_without_trigger" not in write_blob


def test_card_prose_cannot_refuse_and_hunt_sketch_stays_a_noop():
    assert live_card_session_error({"card": "flush bounce"}) == ""
    assert live_card_book_error({"symbol": "SNDK"}, []) == ""
    assert live_card_tape_error({"card": "flush bounce"}, {"spread": 2.0, "last": 90.0}) == ""
    assert apply_hunt_send_sketch({"strategy": "market_bracket"}, {"SNDK": {}}) is None


def test_lab_facts_awaiting_is_names_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(clamp_update({"types": {"market_bracket": {"cards": [_flush()]}}}))
    facts = lab_facts(load_lab())
    for row in facts["cards_awaiting_first_trade"]:
        assert "looks" not in row
        assert "days" not in row
        assert "sends" not in row
        assert "max_looks_without_trigger" not in row
    assert "bracket" in facts["unused_open_types"]
