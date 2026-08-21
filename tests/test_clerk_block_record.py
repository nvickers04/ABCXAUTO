"""A ticket the clerk refuses before the executor must leave a durable trace.

executor.execute_proposal journals every proposal it sees. The gates in
execute_ticket run above it and return early, so a well-formed ticket could be
refused with nothing outside the live think stream and a last_turn.json that
the next look overwrites.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from abcxauto import think_stream as ts
from abcxauto.agent_loop import execute_ticket
from abcxauto.memory import get_journal
from abcxauto.world_state import WorldState


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=True,
        unprotected=["SPY"],
        net_liquidation=37000.0,
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


def _rows(table: str) -> list[tuple]:
    with sqlite3.connect(get_journal().path) as conn:
        return conn.execute(f"select * from {table}").fetchall()


@pytest.mark.asyncio
async def test_gate_block_is_journaled_and_warned(caplog):
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "DKS",
            "quantity": 49,
            "direction": "LONG",
            "stop_price": 177.0,
            "target_price": 185.5,
        },
        "rationale": "consumer flush bounce",
    }
    with caplog.at_level(logging.WARNING, logger="abcxauto.agent_loop"):
        result = await execute_ticket(act, None, _world(), {})

    assert str(result.get("status")) == "blocked"

    proposals = _rows("proposals")
    assert len(proposals) == 1, proposals
    blob = str(proposals[0])
    assert "DKS" in blob
    assert "market_bracket" in blob
    assert "clerk_block" in blob
    assert "gate_ticket" in blob

    gates = _rows("gate_decisions")
    assert len(gates) == 1, gates
    assert gates[0][3] == 0, gates

    assert any(
        "clerk blocked" in r.message or "clerk blocked" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_hold_reject_is_not_journaled_as_a_ticket():
    """A hold / noop send is a normalization reject, not a refused ticket."""
    result = await execute_ticket(
        {"action": "hold", "strategy": "hold", "rationale": "flat"},
        None,
        _world(),
        {},
    )
    assert str(result.get("status")) == "blocked"
    assert _rows("proposals") == []
    assert _rows("gate_decisions") == []


def test_last_turn_send_calls_excludes_self_tune():
    """sends counts every mutating tool. Only send_calls means broker path."""
    import json

    ts.write_last_turn({
        "strat": "self_tune",
        "sends": 1,
        "tool_trace": ["book", "scan", "write_lab_playbook", "self_tune"],
        "world_state": {"flat": True, "net_liquidation": 35310.1},
    })
    payload = json.loads(ts.LAST_TURN_PATH.read_text(encoding="utf-8"))
    assert payload["sends"] == 1
    assert payload["send_calls"] == 0

    ts.write_last_turn({
        "strat": "market_bracket",
        "sends": 1,
        "tool_trace": ["book", "quote", "send"],
        "world_state": {"flat": False, "net_liquidation": 35310.1},
    })
    payload = json.loads(ts.LAST_TURN_PATH.read_text(encoding="utf-8"))
    assert payload["sends"] == 1
    assert payload["send_calls"] == 1
