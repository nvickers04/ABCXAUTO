"""Linear think and screen facts.

Paper RTH / premarket stay-up keeps the live chat across successful looks.
Empty / junk: stop calling the model; chat kept until a poke. Overnight drop it.
Refused send tickets do not ride.
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
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

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


@pytest.mark.parametrize(
    "kind,detail,clears",
    [
        ("stop_dist", "PYPL STK long 50 dist=1.48", False),
        ("working_order_missing", "QQQ 260918C500 long 1", False),
        ("fill", "NVDA", True),
        ("unprotected", "AAPL STK", True),
        ("order_change", "working orders changed", True),
    ],
)
def test_poke_kind_tool_cache(kind, detail, clears):
    """Last-tick stop_dist keeps the cache; fill / real order_change / unprotected clear it."""
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    cached = json.dumps({"flat": False, "open_lots": ["PYPL STK long 50"]})
    turn = BrainTurn()
    turn.tool_cache[_tool_key("book", {})] = cached
    turn.tool_cache[_tool_key("status", {})] = json.dumps({"ibkr": "up"})

    class _Chat:
        def append(self, *_a, **_k):
            pass

    clear_interrupt()
    note_interrupt(BookEvent(kind, detail))
    try:
        ok = asyncio.run(
            _inject_live_poke(
                _Chat(), connector=None, world=_world(), snap={}, turn=turn
            )
        )
        assert ok is True
        if clears:
            assert turn.tool_cache == {}
        else:
            assert turn.tool_cache[_tool_key("book", {})] == cached
            assert _tool_key("status", {}) in turn.tool_cache
    finally:
        clear_interrupt()


def test_stop_dist_poke_omitted_when_stop_has_not_moved_a_tick(monkeypatch):
    """Last-tick closest_stop is not a poke. Stop must move more than a tick."""
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    fact = "fact: closest_stop PYPL STK long 50 dist=1.48 stop=52.61 last=54.09"
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: fact,
    )
    appended: list[object] = []

    class _Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    chat = _Chat()
    chat._abcx_last_desk_fact = fact + "."
    turn = BrainTurn()
    cached = json.dumps({"flat": False})
    turn.tool_cache[_tool_key("book", {})] = cached
    clear_interrupt()
    note_interrupt(BookEvent("stop_dist", "PYPL last tick"))
    try:
        ok = asyncio.run(
            _inject_live_poke(chat, connector=None, world=_world(), snap={}, turn=turn)
        )
        assert ok is False
        assert appended == []
        assert turn.tool_cache[_tool_key("book", {})] == cached
        assert turn.interrupted is False
    finally:
        clear_interrupt()


def test_stop_dist_poke_injects_when_stop_moved_more_than_a_tick(monkeypatch):
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt
    from xai_sdk.chat import developer

    prev = "fact: closest_stop PYPL STK long 50 dist=1.48 stop=52.61 last=54.09."
    moved = "fact: closest_stop PYPL STK long 50 dist=2.61 stop=51.50 last=54.11"
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: moved,
    )
    appended: list[object] = []

    class _Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    chat = _Chat()
    chat._abcx_last_desk_fact = prev
    turn = BrainTurn()
    cached = json.dumps({"flat": False})
    turn.tool_cache[_tool_key("book", {})] = cached
    clear_interrupt()
    note_interrupt(BookEvent("stop_dist", "PYPL stop moved"))
    try:
        ok = asyncio.run(
            _inject_live_poke(chat, connector=None, world=_world(), snap={}, turn=turn)
        )
        assert ok is True
        assert appended
        blob = ""
        for msg in appended:
            assert msg.role == developer("x").role
            blob += "".join(getattr(p, "text", "") for p in (msg.content or []))
        assert "fact: closest_stop" in blob
        assert "stop=51.50" in blob
        assert turn.interrupted is True
        assert turn.tool_cache[_tool_key("book", {})] == cached
    finally:
        clear_interrupt()


def test_wom_poke_omitted_when_missing_set_unchanged(monkeypatch):
    """Unchanged missing-order SET is not a poke. Do not stream or wipe cache."""
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    fact = "fact: working_order_missing QQQ 260918C500 long 1,SPY STK long 10"
    swapped = "fact: working_order_missing SPY STK long 10,QQQ 260918C500 long 1"
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: swapped,
    )
    appended: list[object] = []

    class _Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    chat = _Chat()
    chat._abcx_last_desk_fact = fact + "."
    turn = BrainTurn()
    cached = json.dumps({"flat": False})
    turn.tool_cache[_tool_key("book", {})] = cached
    clear_interrupt()
    note_interrupt(BookEvent("working_order_missing", "QQQ still missing"))
    try:
        ok = asyncio.run(
            _inject_live_poke(chat, connector=None, world=_world(), snap={}, turn=turn)
        )
        assert ok is False
        assert appended == []
        assert turn.tool_cache[_tool_key("book", {})] == cached
        assert turn.interrupted is False
    finally:
        clear_interrupt()


def test_wom_poke_injects_when_missing_set_changed(monkeypatch):
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt
    from xai_sdk.chat import developer

    prev = "fact: working_order_missing QQQ 260918C500 long 1."
    changed = "fact: working_order_missing QQQ 260918C500 long 1,SPY STK long 10"
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: changed,
    )
    appended: list[object] = []

    class _Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    chat = _Chat()
    chat._abcx_last_desk_fact = prev
    turn = BrainTurn()
    cached = json.dumps({"flat": False})
    turn.tool_cache[_tool_key("book", {})] = cached
    clear_interrupt()
    note_interrupt(BookEvent("working_order_missing", "SPY joined"))
    try:
        ok = asyncio.run(
            _inject_live_poke(chat, connector=None, world=_world(), snap={}, turn=turn)
        )
        assert ok is True
        assert appended
        blob = ""
        for msg in appended:
            assert msg.role == developer("x").role
            blob += "".join(getattr(p, "text", "") for p in (msg.content or []))
        assert "working_order_missing" in blob
        assert turn.interrupted is True
        assert turn.tool_cache[_tool_key("book", {})] == cached
    finally:
        clear_interrupt()


def test_unprotected_poke_omitted_when_list_unchanged(monkeypatch):
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    fact = "unprotected=AAPL STK,MSFT STK"
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: "unprotected=MSFT STK,AAPL STK",
    )
    appended: list[object] = []

    class _Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    chat = _Chat()
    chat._abcx_last_desk_fact = fact + "."
    turn = BrainTurn()
    cached = json.dumps({"flat": False})
    turn.tool_cache[_tool_key("book", {})] = cached
    clear_interrupt()
    note_interrupt(BookEvent("unprotected", "AAPL STK"))
    try:
        ok = asyncio.run(
            _inject_live_poke(chat, connector=None, world=_world(), snap={}, turn=turn)
        )
        assert ok is False
        assert appended == []
        assert turn.tool_cache[_tool_key("book", {})] == cached
        assert turn.interrupted is False
    finally:
        clear_interrupt()


def test_fill_poke_does_not_repeat_identical_fact_line(monkeypatch):
    """Fill still pokes. The closest_stop fact line stays one line."""
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    fact = "fact: closest_stop PYPL STK long 50 dist=1.48 stop=52.61 last=54.09"
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: fact,
    )
    appended: list[object] = []

    class _Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    chat = _Chat()
    chat._abcx_last_desk_fact = fact + "."
    turn = BrainTurn()
    cached = json.dumps({"flat": False})
    turn.tool_cache[_tool_key("book", {})] = cached
    clear_interrupt()
    note_interrupt(BookEvent("fill", "PYPL"))
    try:
        ok = asyncio.run(
            _inject_live_poke(chat, connector=None, world=_world(), snap={}, turn=turn)
        )
        assert ok is True
        assert appended
        blob = ""
        for msg in appended:
            blob += "".join(getattr(p, "text", "") for p in (msg.content or []))
        assert blob.count("fact: closest_stop") == 0
        assert "session=" in blob
        assert turn.interrupted is True
        assert turn.tool_cache == {}
    finally:
        clear_interrupt()


def test_every_wake_is_a_new_chat():
    """A live chat stays one messages list. Drop, then a new one."""
    from types import SimpleNamespace

    from abcxauto.brain import _open_wake, drop_live_chat

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
    assert a is b
    assert len(created) == 1
    drop_live_chat(g)
    c = _open_wake(g, "wake three")
    assert c is not a
    assert len(created) == 2


def test_stay_up_resume_reuses_live_chat():
    from types import SimpleNamespace

    from abcxauto.brain import _open_wake

    created: list[object] = []
    appended: list[object] = []

    class Chat:
        def append(self, msg, **_k):
            appended.append(msg)

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
    first = _open_wake(g, "session=regular flat=True send.")
    second = _open_wake(g, "session=regular flat=True send.", resume=True)
    assert second is first
    assert len(created) == 1
    assert len(appended) == 2
    from xai_sdk.chat import developer, user

    assert appended[1].role == developer("x").role
    assert appended[1].role != user("x").role


def test_drop_refused_send_targets_clears_the_ticket():
    from abcxauto.brain import BrainTurn, drop_refused_send_targets

    turn = BrainTurn(
        last_act={"strategy": "market_bracket", "params": {"symbol": "NVDA"}},
        last_result={"status": "blocked", "note": "clerk_block"},
        last_strat="market_bracket",
        sends=[{"act": {"strategy": "market_bracket"}, "result": {"status": "blocked"}}],
        text="tried NVDA",
        tool_trace=["book", "send"],
    )
    drop_refused_send_targets(turn)
    assert turn.last_act == {}
    assert turn.last_result == {}
    assert turn.last_strat == ""
    assert turn.sends == []
    assert turn.text == "tried NVDA"
    assert turn.tool_trace == ["book", "send"]


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
    assert first == 0.0
    assert second == 0.0
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
                    {
                        "symbol": "NVDA",
                        "last": 181.5,
                        "bid": 181.4,
                        "ask": 181.6,
                        "open": 190.0,
                        "close": 200.0,
                        "change_pct": -9.25,
                        "open_gap_pct": -5.0,
                    },
                    {"symbol": "AMD", "last": 0},
                ]
            }

    rows = [{"symbol": "NVDA"}, {"symbol": "AMD"}]
    n = asyncio.run(attach_live_quotes(rows, connector=Conn()))
    assert n == 1
    assert rows[0]["last"] == 181.5
    assert rows[0]["bid"] == 181.4
    assert rows[0]["open_gap_pct"] == -5.0
    assert rows[0]["change_pct"] == -9.25
    assert rows[0]["quote_source"] == "ibkr_live"
    assert rows[0]["ibkr"]["source"] == "ibkr"
    assert rows[0]["ibkr"]["last"] == 181.5
    # A zero/absent last is not a price.
    assert "last" not in rows[1]


def test_scan_quote_sweep_is_a_noop_without_a_connector():
    from abcxauto.opportunity_scan import attach_live_quotes

    rows = [{"symbol": "NVDA"}]
    assert asyncio.run(attach_live_quotes(rows, connector=None)) == 0
    assert "last" not in rows[0]
