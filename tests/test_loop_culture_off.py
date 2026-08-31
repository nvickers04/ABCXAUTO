"""Grok surfaces are not a look-assignment engine.

Playbook is notes. leftover say, unused=, and card_gap floors do not
assign the look. Look tallies, next= tool hops, and hunt sketches do not.
"""

from __future__ import annotations

from abcxauto.brain import _book_payload
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




def test_wake_book_playbook_have_no_assignment_paint(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
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
            "playbook": {},
        },
    )
    assert "SNDK still holding the opening low" not in text
    assert "still no ticket unless news-miss" not in text
    assert "unused=" not in text
    assert "Next look:" not in text
    assert last_look_wake_bit(
        {"last_say": "still no ticket unless news-miss actually fires"}
    ) == ""
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
    blob = _book_payload(_world(), tool_trace=["book"])
    assert "playbook" not in blob or blob.get("playbook") in ({}, None)




def test_wake_and_scan_do_not_assign_card_floors(tmp_path, monkeypatch):
    """Clerk does not tell Grok 'no ticket unless news-miss' or paint card_gap=."""
    from abcxauto.brain import _scan_gate_facts

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    monkeypatch.setenv("ABCXAUTO_DESK_BRIEF_PATH", str(tmp_path / "desk_brief.json"))
    write_desk_brief(
        {
            "last_say": "only send if a card has room",
            "rationale": "loser-scan — only send if a card has room",
            "strat": "loser-scan",
            "sends": 0,
            "tool_trace": ["scan"],
            "send_calls": 0,
        }
    )
    note_wake(None)
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
            "playbook": {},
        },
    )
    assert "still no ticket unless news-miss" not in text
    assert "only send if a card has room" not in text
    assert "loser-scan" not in text
    assert "loser screens" not in text
    assert "prev=" not in text
    assert "unused=" not in text
    assert last_look_wake_bit(
        {"last_say": "still no ticket unless news-miss actually fires"}
    ) == ""
    from abcxauto.brain import _scan_gate_facts

    gate = _scan_gate_facts(
        [{"symbol": "SNDK", "open_gap_pct": -3.8}, {"symbol": "MU", "open_gap_pct": -1.2}]
    )
    assert gate["deepest_symbol"] == "SNDK"
    assert "card_gap_floors" not in gate
    assert "card_min_gap_pct" not in gate
    assert "card_gap_met" not in gate



def test_system_prompt_is_unchanged():
    from abcxauto.llm import SYSTEM_PROMPT

    assert SYSTEM_PROMPT == (
        "You own an Interactive Brokers {mode} book. Strategy is yours.\n"
        "Live only follows a promoted playbook. Risk is code.\n"
        "send tickets that match ORDER EXAMPLES.\n"
        "Size vs max_risk_per_trade_pct of NetLiq.\n"
    )
