"""Linear think, screen facts, and the setup-card playbook.

One wake is one think: Grok uses tools, trades, and stops. Continuity is the
playbook, not a recycled chat. Cards carry their own P&L so a revision is a
decision about evidence rather than about whole-book drift.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from abcxauto.brain import (
    MAX_TOOL_STEPS,
    BrainTurn,
    _cached_read,
    _tool_key,
    provider_overloaded,
)
from abcxauto.world_state import WorldState


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


# --- linear think ------------------------------------------------------------


def test_step_ceiling_is_a_runaway_guard_not_a_budget():
    """High enough that an honest think never feels it."""
    assert MAX_TOOL_STEPS >= 48


def test_repeat_read_is_served_from_this_think():
    turn = BrainTurn()
    args = {"symbol": "NVDA"}
    assert _cached_read(turn, "quote", args) is None
    turn.tool_cache[_tool_key("quote", args)] = json.dumps(
        {"symbol": "NVDA", "last": 180.0}
    )
    again = _cached_read(turn, "quote", args)
    assert again is not None
    blob = json.loads(again)
    assert blob["last"] == 180.0
    assert blob["repeat_of_this_think"] is True
    # Different args are a different question.
    assert _cached_read(turn, "quote", {"symbol": "AMD"}) is None


def test_send_is_never_served_from_cache():
    """Grok owns the book — a second ticket is a decision, not a repeat read."""
    turn = BrainTurn()
    args = {"strategy": "market_bracket", "symbol": "NVDA"}
    turn.tool_cache[_tool_key("send", args)] = json.dumps({"status": "submitted"})
    assert _cached_read(turn, "send", args) is None
    assert _cached_read(turn, "self_tune", {"max_open_positions": 4}) is None


def test_a_send_invalidates_cached_reads():
    """A cached book from before the ticket is a pre-trade fact, not the book."""
    from abcxauto.brain import _dispatch_tool_calls

    turn = BrainTurn()
    turn.tool_cache[_tool_key("book", {})] = json.dumps({"flat": True})

    class _Fn:
        name = "self_tune"
        arguments = json.dumps({"max_open_positions": 4})

    class _Call:
        id = "1"
        function = _Fn()

    class _Chat:
        def append(self, *_a, **_k):
            pass

    asyncio.run(
        _dispatch_tool_calls(
            [_Call()],
            chat=_Chat(),
            connector=None,
            world=_world(),
            snap={},
            turn=turn,
        )
    )
    assert turn.tool_cache == {}


def test_a_live_poke_invalidates_cached_reads():
    from abcxauto.brain import _inject_live_poke
    from abcxauto.wake_bus import BookEvent, clear_interrupt, note_interrupt

    turn = BrainTurn()
    turn.tool_cache[_tool_key("book", {})] = json.dumps({"flat": True})

    class _Chat:
        def append(self, *_a, **_k):
            pass

    clear_interrupt()
    note_interrupt(BookEvent("fill", "NVDA"))
    try:
        ok = asyncio.run(
            _inject_live_poke(
                _Chat(), connector=None, world=_world(), snap={}, turn=turn
            )
        )
        assert ok is True
        assert turn.tool_cache == {}
    finally:
        clear_interrupt()


def test_every_wake_is_a_new_chat():
    from types import SimpleNamespace

    from abcxauto.brain import _open_wake

    created: list[object] = []

    class Chat:
        def append(self, *_a, **_k):
            pass

    class _ChatNS:
        @staticmethod
        def create(**_k):
            chat = Chat()
            created.append(chat)
            return chat

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )
    a = _open_wake(g, "wake one")
    b = _open_wake(g, "wake two")
    assert a is not b
    assert len(created) == 2


# --- backoff instead of cycling ----------------------------------------------


@pytest.mark.parametrize(
    "blob",
    [
        "RESOURCE_EXHAUSTED: at capacity",
        "The model is currently at capacity due to high demand",
        "429 too many requests",
        "StatusCode.UNAVAILABLE",
    ],
)
def test_provider_overloaded_detects_capacity_refusals(blob):
    assert provider_overloaded(blob) is True


def test_provider_overloaded_ignores_ordinary_errors():
    assert provider_overloaded("") is False
    assert provider_overloaded("empty assistant text") is False
    assert provider_overloaded(None) is False


def test_failed_look_backoff_escalates_and_caps():
    from abcxauto.wake_bus import (
        FAILED_LOOK_BACKOFF_CAP_S,
        PROVIDER_BACKOFF_CAP_S,
        PROVIDER_BACKOFF_MIN_S,
        failed_look_backoff_s,
    )

    # Jitter must never let a later strike come back sooner than an earlier one.
    for _ in range(50):
        waits = [failed_look_backoff_s(n) for n in range(1, 6)]
        assert waits == sorted(waits)
    assert failed_look_backoff_s(50) <= FAILED_LOOK_BACKOFF_CAP_S

    # Capacity refusals start well above the ordinary retry and cap higher.
    assert failed_look_backoff_s(1, overloaded=True) >= PROVIDER_BACKOFF_MIN_S
    assert failed_look_backoff_s(1, overloaded=True) > failed_look_backoff_s(1)
    assert failed_look_backoff_s(50, overloaded=True) == PROVIDER_BACKOFF_CAP_S


def test_engine_streak_resets_after_a_good_look():
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine.__new__(ProEngine)
    eng._fail_streak = 0
    eng._last_session = ""
    eng._resume_think = False

    first = eng._rearm_after_think(
        {"_failed": True, "_stream_error": "boom"}, session="regular"
    )
    second = eng._rearm_after_think(
        {"_failed": True, "_stream_error": "boom"}, session="regular"
    )
    assert second > first
    assert eng._fail_streak == 2

    assert eng._rearm_after_think({"sends": 1}, session="regular") == 0.0
    assert eng._fail_streak == 0


# --- screens arrive triageable ------------------------------------------------


def test_scan_hits_carry_scanner_rank_and_metric():
    from abcxauto.opportunity_scan import overlay_hits

    rows = overlay_hits(
        ["NVDA", "AMD"],
        positions=[{"symbol": "AMD"}],
        scanner_rows=[
            {"symbol": "NVDA", "rank": 0, "distance": "12.4", "benchmark": "pct"},
            {"symbol": "AMD", "rank": 1, "distance": "8.1"},
        ],
    )
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["rank"] == 0
    assert rows[0]["distance"] == "12.4"
    assert rows[0]["benchmark"] == "pct"
    assert rows[0]["on_book"] is False
    assert rows[1]["on_book"] is True


def test_scan_quote_sweep_stamps_live_last():
    from abcxauto.opportunity_scan import attach_live_quotes

    class Conn:
        async def get_live_quotes(self, syms, **_k):
            return {
                "quotes": [
                    {"symbol": "NVDA", "last": 181.5, "bid": 181.4, "ask": 181.6},
                    {"symbol": "AMD", "last": 0},
                ]
            }

    rows = [{"symbol": "NVDA"}, {"symbol": "AMD"}]
    n = asyncio.run(attach_live_quotes(rows, connector=Conn()))
    assert n == 1
    assert rows[0]["last"] == 181.5
    assert rows[0]["bid"] == 181.4
    assert rows[0]["quote_source"] == "ibkr_live"
    # A zero/absent last is not a price.
    assert "last" not in rows[1]


def test_scan_quote_sweep_is_a_noop_without_a_connector():
    from abcxauto.opportunity_scan import attach_live_quotes

    rows = [{"symbol": "NVDA"}]
    assert asyncio.run(attach_live_quotes(rows, connector=None)) == 0
    assert "last" not in rows[0]


# --- setup cards --------------------------------------------------------------


def test_cards_save_and_render_as_the_book(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import load_lab, notebook_text, save_lab, clamp_update

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    update = clamp_update(
        {
            "mode": "explore",
            "cards": [
                {
                    "name": "gap fade after 10:00",
                    "when_on": "high_open_gap, gap > 3%, large cap",
                    "scan": "arena=high_open_gap market_cap_above=1e10",
                    "ticket": "market_bracket",
                    "shape": "SHORT, stop above gap high",
                    "invalidation": "gap fills before 10:00",
                    "status": "testing",
                }
            ],
        }
    )
    assert update is not None
    save_lab(update)
    lab = load_lab()
    assert lab["cards"][0]["name"] == "gap fade after 10:00"
    assert lab["cards"][0]["ticket"] == "market_bracket"
    text = notebook_text(lab)
    assert "SETUP gap fade after 10:00" in text
    assert "ticket=market_bracket" in text
    assert "when_on:" in text
    # The book is the setup, not a restatement of the order schema.
    assert "open_shape" not in text


def test_card_ticket_must_be_a_sendable_type(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import apply_from_judgment, book_shape_rejects

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    bad = {"cards": [{"name": "moon", "ticket": "yolo_calls"}]}
    assert "unknown_type" in book_shape_rejects(bad)
    out = apply_from_judgment({"lab_playbook": bad})
    assert out is not None
    assert out.get("status") == "rejected"


def test_prose_observations_are_kept_not_rejected(tmp_path, monkeypatch):
    """The notebook has to be able to hold what was learned."""
    from abcxauto.lab_playbook import apply_from_judgment, load_lab

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    prose = "NVDA sold every open gap this week. Watch the 10:00 reversal."
    out = apply_from_judgment({"lab_playbook": {"instructions": prose}})
    assert out is not None
    assert load_lab()["instructions"] == prose


def test_card_scores_attribute_realized_pnl(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import card_scores, record_card_send

    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    monkeypatch.setattr(
        "abcxauto.memory.get_journal",
        lambda: type("J", (), {"realized_by_order_id": lambda self: {41: 120.0}})(),
    )
    record_card_send(
        card="gap fade",
        strategy="market_bracket",
        symbol="NVDA",
        result={"order_id": 41},
    )
    record_card_send(
        card="gap fade",
        strategy="market_bracket",
        symbol="AMD",
        result={"order_id": 99},
    )
    rows = card_scores([{"name": "gap fade"}])
    assert len(rows) == 1
    row = rows[0]
    assert row["card"] == "gap fade"
    assert row["sends"] == 2
    assert row["realized_pnl"] == 120.0
    assert row["attributed_fills"] == 1
    assert row["on_current_book"] is True
    assert set(row["symbols"]) == {"NVDA", "AMD"}


def test_unnamed_card_send_is_not_logged(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import card_scores, record_card_send

    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    record_card_send(card="", strategy="market_bracket", result={"order_id": 1})
    assert card_scores() == []


def test_promote_reads_a_window_not_lifetime(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import promote_beating, promote_window

    monkeypatch.setenv("ABCXAUTO_PROMOTE_WINDOW", "1d")
    assert promote_window() == "1d"
    # Lifetime is behind, but the promote window is ahead.
    sc = {
        "beating_model": False,
        "windows": {"1d": {"coverage": "ok", "beating_model": True}},
    }
    assert promote_beating(sc) is True
    # A thin window falls back to the full-book flag.
    thin = {
        "beating_model": False,
        "windows": {"1d": {"coverage": "thin", "beating_model": True}},
    }
    assert promote_beating(thin) is False


def test_book_payload_carries_cards_and_structure_lessons(monkeypatch, tmp_path):
    from abcxauto.brain import _book_payload

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    from abcxauto.lab_playbook import save_lab

    save_lab(
        {
            "mode": "exploit",
            "instructions": "SETUP gap fade",
            "cards": [{"name": "gap fade", "ticket": "market_bracket", "status": "working"}],
        }
    )
    world = _world(
        structure_lessons=[
            {
                "strategy": "market_bracket",
                "symbol": "NVDA",
                "reason_code": "geometry_rejected",
                "message": "stop wrong side of last",
            }
        ]
    )
    payload = _book_payload(world)
    pb = payload["playbook"]
    assert pb["mode"] == "exploit"
    assert pb["cards"][0]["name"] == "gap fade"
    assert "gap fade" in pb["notes"]
    lessons = payload["world"]["structure_lessons"]
    assert lessons[0]["reason_code"] == "geometry_rejected"
    assert lessons[0]["symbol"] == "NVDA"


# --- one desk -----------------------------------------------------------------


def test_desk_lock_blocks_a_second_desk(tmp_path, monkeypatch):
    import os

    from abcxauto.supervisor import (
        claim_desk_lock,
        desk_owner_pid,
        release_desk_lock,
    )

    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(tmp_path / "desk.lock"))
    assert claim_desk_lock() is True
    assert desk_owner_pid() == os.getpid()
    # Same process may re-claim; a different live pid may not.
    assert claim_desk_lock() is True
    (tmp_path / "desk.lock").write_text(json.dumps({"pid": 1}), encoding="utf-8")
    monkeypatch.setattr("abcxauto.supervisor._pid_alive", lambda pid: pid == 1)
    assert claim_desk_lock() is False
    release_desk_lock()


def test_desk_lock_is_free_when_owner_is_dead(tmp_path, monkeypatch):
    from abcxauto.supervisor import claim_desk_lock, desk_owner_pid

    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(tmp_path / "desk.lock"))
    (tmp_path / "desk.lock").write_text(json.dumps({"pid": 424242}), encoding="utf-8")
    monkeypatch.setattr("abcxauto.supervisor._pid_alive", lambda pid: False)
    assert desk_owner_pid() == 0
    assert claim_desk_lock() is True
