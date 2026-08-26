"""A book event mid-message defers the reads, never the ticket.

The defect this guards: reads ran in parallel, then ``_dispatch_tool_calls``
returned early on a pending interrupt, so a ``send`` in the same assistant
message was abandoned with no marker, no trace entry, no log, no journal row,
and no tool result for its ``tool_call_id``. Grok believed it had sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

from abcxauto import brain
from abcxauto.brain import BrainTurn, _dispatch_tool_calls, _inject_live_poke
from abcxauto.memory import get_journal
from abcxauto.park_clock import (
    BookEvent,
    clear_interrupt,
    note_interrupt,
    peek_interrupt,
)
from abcxauto.world_state import WorldState


class _Fn:
    def __init__(self, name: str, args: dict):
        self.name = name
        self.arguments = json.dumps(args)


class _Call:
    def __init__(self, cid: str, name: str, args: dict):
        self.id = cid
        self.function = _Fn(name, args)


class _Chat:
    def append(self, *_a, **_k):
        pass


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=35310.1,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={"max_risk_per_trade_pct": 1.0},
        envelope={},
        regime={"session_phase": "mid", "trend_bias": "mixed", "vol_proxy": "normal"},
        portfolio_risk={"n_positions": 0, "top_symbol": "", "top_concentration_pct": 0},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _ticket_message() -> list[_Call]:
    """Two reads and a fully formed bracket in one assistant message."""
    return [
        _Call("c1", "book", {}),
        _Call("c2", "quote", {"symbol": "DKS"}),
        _Call(
            "c3",
            "send",
            {
                "strategy": "market_bracket",
                "params": {
                    "symbol": "DKS",
                    "quantity": 49,
                    "direction": "LONG",
                    "stop_price": 177.0,
                    "target_price": 185.5,
                },
            },
        ),
    ]


def _deferred_rows() -> list[tuple]:
    with sqlite3.connect(get_journal().path) as conn:
        return conn.execute(
            "select strategy, rationale from decisions where action = 'tool_deferred'"
        ).fetchall()


def _dispatch(calls, turn):
    return asyncio.run(
        _dispatch_tool_calls(
            calls,
            chat=_Chat(),
            connector=None,
            world=_world(),
            snap={},
            turn=turn,
        )
    )


@pytest.fixture(autouse=True)
def _no_pending_interrupt():
    clear_interrupt()
    yield
    clear_interrupt()


@pytest.fixture
def appended(monkeypatch):
    """Every tool_call_id must get exactly one result appended to the chat."""
    rows: list[tuple[str, str]] = []
    monkeypatch.setattr(
        brain,
        "_append_tool_result",
        lambda _chat, tc, result: rows.append((getattr(tc, "id", None), result)),
    )
    return rows


def test_interrupt_in_flight_still_dispatches_the_send(monkeypatch, appended, caplog):
    """The regression: a fill mid-read must not take the ticket with it."""
    invoked: list[str] = []

    async def _fake_run_tool(name, args, *, connector, world, snap, turn):
        invoked.append(name)
        if name == "send":
            return json.dumps({"status": "submitted", "order_id": 4443})
        # The fill lands while this read is still on the wire.
        note_interrupt(BookEvent("fill", "WMT target filled"))
        await asyncio.sleep(1.0)
        return json.dumps({"symbol": "DKS", "last": 179.75})

    monkeypatch.setattr(brain, "_run_tool", _fake_run_tool)
    turn = BrainTurn()

    with caplog.at_level(logging.WARNING, logger="abcxauto.brain"):
        pending = _dispatch(_ticket_message(), turn)

    assert "send" in invoked
    assert "send" in turn.tool_trace
    assert sorted(cid for cid, _ in appended) == ["c1", "c2", "c3"]
    assert json.loads(dict(appended)["c3"])["status"] == "submitted"

    assert {r[0] for r in _deferred_rows()} == {"book", "quote"}
    assert any("deferred" in r.getMessage() for r in caplog.records)

    # The poke is still waiting — deferring reads must not swallow it.
    assert pending is True
    assert peek_interrupt() is not None


def test_interrupt_before_reads_defers_them_unrun(monkeypatch, appended, caplog):
    invoked: list[str] = []

    async def _fake_run_tool(name, args, *, connector, world, snap, turn):
        invoked.append(name)
        return json.dumps({"status": "submitted", "order_id": 4443})

    monkeypatch.setattr(brain, "_run_tool", _fake_run_tool)
    note_interrupt(BookEvent("order_change", "stop replaced"))
    turn = BrainTurn()

    with caplog.at_level(logging.WARNING, logger="abcxauto.brain"):
        pending = _dispatch(_ticket_message(), turn)

    assert invoked == ["send"]
    assert sorted(cid for cid, _ in appended) == ["c1", "c2", "c3"]

    # A deferred read must never read as a flat book or a clean scan.
    for cid in ("c1", "c2"):
        notice = json.loads(dict(appended)[cid])
        assert notice["status"] == "deferred"
        assert set(notice) == {"status", "tool", "note"}
        assert "not an empty book" in notice["note"]
        assert "Ask for this read again" in notice["note"]

    assert {r[0] for r in _deferred_rows()} == {"book", "quote"}
    assert any("deferred" in r.getMessage() for r in caplog.records)
    assert pending is True
    assert peek_interrupt() is not None


def test_no_interrupt_defers_nothing(monkeypatch, appended):
    invoked: list[str] = []

    async def _fake_run_tool(name, args, *, connector, world, snap, turn):
        invoked.append(name)
        return json.dumps({"ok": True})

    monkeypatch.setattr(brain, "_run_tool", _fake_run_tool)
    turn = BrainTurn()
    pending = _dispatch(_ticket_message(), turn)

    assert sorted(invoked) == ["book", "quote", "send"]
    assert sorted(cid for cid, _ in appended) == ["c1", "c2", "c3"]
    assert _deferred_rows() == []
    assert pending is False


def test_the_poke_still_lands_after_the_send(monkeypatch, appended):
    """What changed is which call gets dropped, not whether Grok hears the fill."""

    async def _fake_run_tool(name, args, *, connector, world, snap, turn):
        return json.dumps({"status": "submitted"})

    monkeypatch.setattr(brain, "_run_tool", _fake_run_tool)
    note_interrupt(BookEvent("fill", "DKS entry filled"))
    turn = BrainTurn()

    assert _dispatch(_ticket_message(), turn) is True
    ok = asyncio.run(
        _inject_live_poke(
            _Chat(), connector=None, world=_world(), snap={}, turn=turn
        )
    )
    assert ok is True
    assert turn.interrupted is True
    assert peek_interrupt() is None


def test_a_deferred_read_is_never_cached_as_a_fact(monkeypatch, appended):
    """Cached, it would come back stamped repeat_of_this_think — a settled fact."""

    async def _fake_run_tool(name, args, *, connector, world, snap, turn):
        note_interrupt(BookEvent("fill", "QQQ"))
        await asyncio.sleep(1.0)
        return json.dumps({"flat": True})

    monkeypatch.setattr(brain, "_run_tool", _fake_run_tool)
    turn = BrainTurn()
    _dispatch([_Call("c1", "book", {})], turn)

    assert turn.tool_cache == {}
    assert json.loads(dict(appended)["c1"])["status"] == "interrupted"


def test_a_write_that_explodes_still_answers_its_tool_call_id(
    monkeypatch, appended, caplog
):
    async def _boom(*_a, **_k):
        raise RuntimeError("connector vanished")

    monkeypatch.setattr(brain, "_invoke_named_tool", _boom)
    turn = BrainTurn()

    with caplog.at_level(logging.WARNING, logger="abcxauto.brain"):
        _dispatch([_ticket_message()[2]], turn)

    assert [cid for cid, _ in appended] == ["c3"]
    assert "connector vanished" in appended[0][1]
    assert {r[0] for r in _deferred_rows()} == {"send"}
