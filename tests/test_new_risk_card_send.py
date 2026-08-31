"""Nameless new risk is clerk_block; a named card must land in journal params_json.

2026-08-26 paper BSX market_bracket 184 long filled with no params.card and no
clerk_block row. gate_ticket already called new_risk_card_error, but:

* pydantic MarketBracketParams ignored extra ``card``, so execute_proposal
  journaled model_dump without it (scorecard saw a nameless fill).
* brain send built act from args.params only, so a top-level send.card that
  hoist missed never reached the gate or the journal.

source=cycle on that row is execute_proposal's journal label after
execute_ticket — not a bypass. Exits / cancels / protection still skip the
label gate.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from abcxauto.agent_loop import execute_ticket
from abcxauto.config import Config, get_config
from abcxauto.executor import execute_proposal
from abcxauto.memory import get_journal
from abcxauto.proposals import params_for_journal, validate_proposal
from abcxauto.tool_args import bind_send_card, hoist_send_params, normalize_tool_call
from abcxauto.world_state import WorldState

BSX_CARD = "large-cap 3pct gap hold"
_BSX_PARAMS = {
    "symbol": "BSX",
    "quantity": 5,
    "direction": "LONG",
    "stop_price": 97.0,
    "target_price": 106.0,
}




def _world() -> WorldState:
    return WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
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


def _snap() -> dict:
    return {
        "account": {"netliquidation": 100_000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {"BSX": 100.0},
    }


def _ticket(*, card: str | None = None, top_level_card: str | None = None) -> dict:
    params = dict(_BSX_PARAMS)
    if card is not None:
        params["card"] = card
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": params,
        "rationale": BSX_CARD,
    }
    if top_level_card is not None:
        act["card"] = top_level_card
    return act


def _proposal_rows() -> list[sqlite3.Row]:
    with sqlite3.connect(get_journal().path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute("SELECT * FROM proposals ORDER BY id"))


class _BracketGw:
    connected = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def place_market_bracket(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "status": "ok", "order_id": 501, "order_ids": [501, 502, 503]}


def _paper_no_risk_gates(monkeypatch) -> None:
    base = get_config()
    cfg = Config(
        **{
            **base.__dict__,
            "trading_mode": "paper",
            "ibkr_port": 7497,
            "risk_gates_enabled": False,
            "defined_risk_only": False,
            "risk_posture": "balanced",
            "max_arena_concentration_pct": 0,
        }
    )
    monkeypatch.setattr("abcxauto.executor.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.send.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: cfg)


def test_hoist_copies_top_level_card_into_existing_params():
    """BSX shape: nested params without card, top-level send.card."""
    out = hoist_send_params(
        {
            "strategy": "market_bracket",
            "card": BSX_CARD,
            "params": dict(_BSX_PARAMS),
        }
    )
    assert out["params"]["card"] == BSX_CARD
    assert out["params"]["symbol"] == "BSX"
    assert out["params"]["quantity"] == 5


def test_bind_send_card_stamps_top_level_onto_params():
    act = {
        "strategy": "market_bracket",
        "card": BSX_CARD,
        "params": dict(_BSX_PARAMS),
    }
    assert bind_send_card(act) == BSX_CARD
    assert act["params"]["card"] == BSX_CARD
    assert act["card"] == BSX_CARD


def test_validate_proposal_journals_card_off_the_wire():
    p = validate_proposal(
        "market_bracket",
        {**_BSX_PARAMS, "card": BSX_CARD},
        BSX_CARD,
        quote_last=100.0,
    )
    dumped = p.params.model_dump(exclude_none=True)
    assert "card" not in dumped
    assert p.card == BSX_CARD
    assert getattr(p.params, "card", None) == BSX_CARD
    journaled = params_for_journal(p)
    assert journaled["card"] == BSX_CARD
    assert journaled["symbol"] == "BSX"
    assert journaled["quantity"] == 5


@pytest.mark.asyncio
async def test_bsx_nameless_market_bracket_is_clerk_block():
    """Same ticket Noah filled, with no card: must not reach the broker."""
    gw = _BracketGw()
    result = await execute_ticket(_ticket(), gw, _world(), _snap())
    assert result.get("status") == "blocked"
    note = str(result.get("note") or "")
    assert "params.card" in note
    assert gw.calls == []
    rows = _proposal_rows()
    assert len(rows) == 1
    assert rows[0]["source"] == "clerk_block"
    assert rows[0]["validation_ok"] == 0
    assert rows[0]["strategy"] == "market_bracket"
    assert rows[0]["symbol"] == "BSX"
    blob = str(rows[0]["validation_reason"] or "")
    assert "gate_ticket" in blob
    assert "params.card" in blob
    params = json.loads(rows[0]["params_json"] or "{}")
    assert params.get("card") in (None, "")


@pytest.mark.asyncio
async def test_bsx_named_market_bracket_journals_card(monkeypatch):
    """Same ticket with card= on the lab book: may send; journal keeps the name."""
    _paper_no_risk_gates(monkeypatch)
    gw = _BracketGw()
    result = await execute_ticket(
        _ticket(card=BSX_CARD), gw, _world(), _snap()
    )
    assert result.get("success") is True or result.get("status") == "ok", result
    assert gw.calls and "card" not in gw.calls[0]
    assert gw.calls[0]["symbol"] == "BSX"
    assert gw.calls[0]["quantity"] == 5
    rows = [r for r in _proposal_rows() if r["source"] != "clerk_block"]
    assert rows, _proposal_rows()
    row = rows[-1]
    assert row["validation_ok"] == 1
    params = json.loads(row["params_json"] or "{}")
    assert params.get("card") == BSX_CARD


@pytest.mark.asyncio
async def test_top_level_card_without_params_card_journals_name(monkeypatch):
    """Grok put card next to strategy, not inside params. Still a named send."""
    _paper_no_risk_gates(monkeypatch)
    gw = _BracketGw()
    result = await execute_ticket(
        _ticket(top_level_card=BSX_CARD), gw, _world(), _snap()
    )
    assert result.get("success") is True or result.get("status") == "ok", result
    assert gw.calls and "card" not in gw.calls[0]
    rows = [r for r in _proposal_rows() if r["validation_ok"] == 1]
    assert rows
    params = json.loads(rows[-1]["params_json"] or "{}")
    assert params.get("card") == BSX_CARD


@pytest.mark.asyncio
async def test_brain_send_copies_top_level_card_onto_act(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool

    seen: list[dict] = []

    async def capture(act, connector, world, snap):
        seen.append(act)
        return {"status": "ok", "success": True, "order_ids": [1]}

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", capture)
    turn = BrainTurn()
    await _run_tool(
        "send",
        {
            "strategy": "market_bracket",
            "card": BSX_CARD,
            "params": dict(_BSX_PARAMS),
            "rationale": BSX_CARD,
        },
        connector=None,
        world=_world(),
        snap=_snap(),
        turn=turn,
    )
    assert seen
    assert seen[0]["params"]["card"] == BSX_CARD
    assert seen[0].get("card") == BSX_CARD


@pytest.mark.asyncio
async def test_execute_proposal_journals_card_not_on_gateway(monkeypatch):
    _paper_no_risk_gates(monkeypatch)
    proposal = validate_proposal(
        "market_bracket",
        {**_BSX_PARAMS, "card": BSX_CARD},
        BSX_CARD,
        quote_last=100.0,
    )
    gw = _BracketGw()
    result = await execute_proposal(proposal, gw)
    assert result.get("success") is True
    assert gw.calls and "card" not in gw.calls[0]
    rows = _proposal_rows()
    assert rows
    params = json.loads(rows[-1]["params_json"] or "{}")
    assert params["card"] == BSX_CARD


@pytest.mark.asyncio
async def test_cancel_without_card_is_not_clerk_block(monkeypatch):
    _paper_no_risk_gates(monkeypatch)

    class _CancelGw:
        connected = True

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.open_orders = [
                {"order_id": 42, "symbol": "NKE", "sec_type": "STK",
                 "action": "SELL", "quantity": 70, "order_type": "LMT"},
                {"order_id": 43, "symbol": "NKE", "sec_type": "STK",
                 "action": "SELL", "quantity": 70, "order_type": "STP"},
            ]
            self.positions = [
                {"symbol": "NKE", "sec_type": "STK", "quantity": 70, "conId": 9},
            ]

        async def get_open_orders(self):
            return self.open_orders

        async def get_positions(self):
            return self.positions

        async def cancel_order(self, **kwargs):
            self.calls.append(kwargs)
            return {"success": True, "status": "ok"}

    gw = _CancelGw()
    world = _world()
    world.flat = False
    world.positions = gw.positions
    world.open_orders = gw.open_orders
    result = await execute_ticket(
        {
            "action": "cancel_order",
            "strategy": "cancel_order",
            "params": {"order_id": 42},
            "rationale": "cancel child",
        },
        gw,
        world,
        {
            "account": {"netliquidation": 100_000.0},
            "positions": gw.positions,
            "open_orders": gw.open_orders,
        },
    )
    assert result.get("success") is True or result.get("status") == "ok", result
    assert gw.calls
    clerk = [r for r in _proposal_rows() if r["source"] == "clerk_block"]
    assert clerk == []
    named = [
        json.loads(r["params_json"] or "{}")
        for r in _proposal_rows()
        if r["validation_ok"] == 1
    ]
    assert named
    assert named[-1].get("card") in (None, "")


def test_normalize_send_hoists_bsx_top_level_card():
    _name, args = normalize_tool_call(
        "send",
        {
            "strategy": "market_bracket",
            "card": BSX_CARD,
            **_BSX_PARAMS,
        },
    )
    assert _name == "send"
    assert args["params"]["card"] == BSX_CARD
    assert args["params"]["symbol"] == "BSX"
