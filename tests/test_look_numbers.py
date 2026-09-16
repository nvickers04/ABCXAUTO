"""Send gate: ticket last / IV / credit / width must be in this look's cache."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.look_snapshot import (
    REASON_CODE,
    begin_look,
    check_ticket_numbers,
    record_look_tool,
    ticket_claims,
)
from abcxauto.llm import SYSTEM_PROMPT
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


def test_system_prompt_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def _snap_with_quote(symbol: str = "SPY", **fields) -> dict:
    snap: dict = {}
    begin_look(snap)
    row = {"symbol": symbol, "source": "ibkr", "freshness": "live", **fields}
    record_look_tool(snap, "quote", row)
    return snap


def test_invented_last_is_rejected():
    snap = _snap_with_quote("SPY", last=500.12, bid=500.10, ask=500.14, mid=500.12)
    ok, code, msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "SPY", "price_hint": 999.99, "stop_price": 495.0, "target_price": 510.0},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE == "stale_or_invented_number"
    assert "999.99" in msg
    assert "this look" in msg


def test_matching_last_from_this_look_quote_passes():
    snap = _snap_with_quote("SPY", last=500.12, bid=500.10, ask=500.14, mid=500.12)
    ok, code, msg = check_ticket_numbers(
        "market_bracket",
        {
            "symbol": "SPY",
            "direction": "LONG",
            "price_hint": 500.12,
            "stop_price": 495.0,
            "target_price": 510.0,
        },
        snap,
    )
    assert ok is True
    assert code == "ok"
    assert msg == ""


def test_unverifiable_last_is_a_kill_not_a_pass():
    snap: dict = {}
    begin_look(snap)
    ok, code, msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "NVDA", "last": 120.0},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "120.0" in msg


def test_scan_last_is_not_a_this_look_print():
    snap: dict = {"scan_hits": {"rows": [{"symbol": "SPY", "last": 500.12}]}}
    begin_look(snap)
    ok, code, _msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "SPY", "price_hint": 500.12},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE


def test_invented_credit_is_rejected():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "strike": 500.0,
            "right": "C",
            "ibkr": {"last": 1.20, "bid": 1.18, "ask": 1.22, "mid": 1.20, "iv": 0.18},
            "mda": {"iv": 0.99},
        },
    )
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        {
            "symbol": "SPY",
            "limit_price": 4.50,
            "long_strike": 500.0,
            "short_strike": 505.0,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "4.5" in msg


def test_matching_credit_from_option_quote_passes():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "ibkr": {"last": 1.20, "bid": 1.18, "ask": 1.22, "mid": 1.20, "iv": 0.18},
            "mda": {"iv": 0.99},
        },
    )
    ok, code, _msg = check_ticket_numbers(
        "vertical_spread",
        {"symbol": "SPY", "limit_price": 1.20, "credit": 1.20},
        snap,
    )
    assert ok is True
    assert code == "ok"


def test_mda_iv_is_not_send_geometry():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "ibkr": {"last": 1.20, "bid": 1.18, "ask": 1.22},
            "mda": {"iv": 0.99},
        },
    )
    ok, code, _msg = check_ticket_numbers(
        "vertical_spread",
        {"symbol": "SPY", "iv": 0.99, "limit_price": 1.20},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE


def test_wing_width_must_be_in_this_look_strikes():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "quotes": [
                {"symbol": "SPY", "strike": 500.0, "ibkr": {"bid": 2.0, "ask": 2.1, "mid": 2.05}},
                {"symbol": "SPY", "strike": 510.0, "ibkr": {"bid": 0.9, "ask": 1.0, "mid": 0.95}},
            ]
        },
    )
    ok, code, _msg = check_ticket_numbers(
        "iron_butterfly",
        {"symbol": "SPY", "wing_width": 10.0, "limit_price": 2.05},
        snap,
    )
    assert ok is True
    assert code == "ok"
    bad, bcode, _ = check_ticket_numbers(
        "iron_butterfly",
        {"symbol": "SPY", "wing_width": 25.0, "limit_price": 2.05},
        snap,
    )
    assert bad is False
    assert bcode == REASON_CODE


def test_no_claimed_numbers_does_not_fire():
    snap: dict = {}
    begin_look(snap)
    ok, code, msg = check_ticket_numbers(
        "cancel_order",
        {"order_id": 103},
        snap,
    )
    assert ok is True
    assert code == "ok"
    assert msg == ""
    assert ticket_claims("cancel_order", {"order_id": 103}) == []


def test_book_last_binds_this_look():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "book",
        {
            "ibkr_live_quotes": {"AAPL": 178.5},
            "world": {
                "positions": [{"symbol": "AAPL", "sec": "STK", "qty": 20, "mkt": 178.5}]
            },
        },
    )
    ok, code, _msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "AAPL", "price_hint": 178.5},
        snap,
    )
    assert ok is True
    assert code == "ok"


@pytest.mark.asyncio
async def test_execute_ticket_rejects_invented_last(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr(
        "abcxauto.agent_loop.gate_ticket",
        lambda act, _world: (str(act.get("strategy") or ""), None),
    )
    snap = _snap_with_quote("SPY", last=500.12, bid=500.10, ask=500.14, mid=500.12)
    snap["account"] = {"netliquidation": 37000.0}
    snap["positions"] = []
    snap["open_orders"] = []
    world = WorldState(
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
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "direction": "LONG",
                "stop_price": 495.0,
                "target_price": 510.0,
                "quantity": 1,
                "price_hint": 777.77,
            },
            "rationale": "invented last",
        },
        MagicMock(),
        world,
        snap,
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == REASON_CODE
    assert "777.77" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_rejects_invented_credit(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr(
        "abcxauto.agent_loop.gate_ticket",
        lambda act, _world: (str(act.get("strategy") or ""), None),
    )
    snap: dict = {"account": {"netliquidation": 37000.0}, "positions": [], "open_orders": []}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "ibkr": {"last": 1.20, "bid": 1.18, "ask": 1.22, "mid": 1.20},
        },
    )
    world = WorldState(
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
    result = await execute_ticket(
        {
            "action": "vertical_spread",
            "strategy": "vertical_spread",
            "params": {
                "symbol": "SPY",
                "expiration": "20260718",
                "long_strike": 500.0,
                "short_strike": 505.0,
                "right": "C",
                "quantity": 1,
                "limit_price": 9.99,
            },
            "rationale": "invented credit",
        },
        MagicMock(),
        world,
        snap,
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == REASON_CODE
    assert "9.99" in str(result.get("note") or "")
    assert sent == []


def test_option_limit_not_verified_by_stock_print():
    snap = _snap_with_quote("SPY", last=590.0, bid=589.9, ask=590.1, mid=590.0)
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "long_strike": 500.0,
            "short_strike": 505.0,
            "right": "C",
            "limit_price": 590.0,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "590" in msg


def test_invented_stop_rejected_while_derived_stop_passes():
    snap = _snap_with_quote("SPY", last=500.12, bid=500.10, ask=500.14, mid=500.12)
    invented, icode, imsg = check_ticket_numbers(
        "market_bracket",
        {
            "symbol": "SPY",
            "direction": "LONG",
            "price_hint": 500.12,
            "stop_price": 12.34,
            "target_price": 510.0,
        },
        snap,
    )
    assert invented is False
    assert icode == REASON_CODE
    assert "12.34" in imsg

    derived, dcode, dmsg = check_ticket_numbers(
        "market_bracket",
        {
            "symbol": "SPY",
            "direction": "LONG",
            "price_hint": 500.12,
            "stop_price": 495.0,
            "target_price": 510.0,
        },
        snap,
    )
    assert derived is True
    assert dcode == "ok"
    assert dmsg == ""

    ungrounded, ucode, _ = check_ticket_numbers(
        "market_bracket",
        {
            "symbol": "SPY",
            "direction": "LONG",
            "stop_price": 495.0,
            "target_price": 510.0,
        },
        {},
    )
    assert ungrounded is False
    assert ucode == REASON_CODE


def test_modify_stop_does_not_claim_protection_geometry():
    snap: dict = {}
    begin_look(snap)
    assert ticket_claims(
        "modify_stop",
        {"order_id": 101, "new_stop_price": 12.34, "stop_price": 12.34},
    ) == []
    ok, code, msg = check_ticket_numbers(
        "modify_stop",
        {"order_id": 101, "new_stop_price": 12.34, "stop_price": 12.34},
        snap,
    )
    assert ok is True
    assert code == "ok"
    assert msg == ""


def _world() -> "object":
    from abcxauto.world_state import WorldState

    return WorldState(
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


@pytest.mark.asyncio
async def test_scan_mda_last_refused_for_geometry(monkeypatch):
    from abcxauto.agent_loop import _quote_for_action, execute_ticket

    act = {
        "strategy": "market_bracket",
        "params": {
            "symbol": "SNDK",
            "direction": "LONG",
            "stop_price": 88.0,
            "target_price": 93.0,
        },
    }
    snap = {
        "ibkr_live_quotes": {},
        "scan_hits": {"rows": [{"symbol": "SNDK", "last": 91.5}]},
    }
    assert await _quote_for_action(act, snap, connector=None) is None

    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr(
        "abcxauto.agent_loop.gate_ticket",
        lambda ticket, _world: (str(ticket.get("strategy") or ""), None),
    )
    snap_ex = {
        "account": {"netliquidation": 37000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {},
        "scan_hits": {"rows": [{"symbol": "SNDK", "last": 91.5}]},
    }
    begin_look(snap_ex)
    record_look_tool(
        snap_ex, "quote", {"symbol": "SNDK", "last": 91.5, "bid": 91.4, "ask": 91.6}
    )
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "symbol": "SNDK",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 10,
                "price_hint": 91.5,
                "card": "flush bounce",
            },
            "rationale": "mda tape only",
        },
        object(),
        _world(),
        snap_ex,
    )
    # Look numbers can pass from the IBKR quote; send geometry still needs
    # a live IBKR last. Scan row.last is MDA — fail closed.
    assert result.get("status") == "blocked"
    assert "IBKR live last" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_kill_look_exception_fail_closes_consistently(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    def boom(*_a, **_k):
        raise RuntimeError("kill-look exploded")

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.thin_rth_kill_look.kill_look_send_block", boom)

    snap = {"account": {"netliquidation": 37000.0}, "positions": [], "open_orders": []}
    world = _world()

    exit_result = await execute_ticket(
        {
            "action": "cancel_order",
            "strategy": "cancel_order",
            "params": {"order_id": 42},
            "rationale": "exit",
        },
        MagicMock(),
        world,
        snap,
    )
    assert exit_result.get("status") == "blocked"
    assert exit_result.get("reason_code") == "kill_look_failed_closed"
    assert "failed closed" in str(exit_result.get("note") or "")

    risk_result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "direction": "LONG",
                "stop_price": 495.0,
                "target_price": 510.0,
                "quantity": 1,
                "card": "flush bounce",
            },
            "rationale": "new risk",
        },
        MagicMock(),
        world,
        snap,
    )
    assert risk_result.get("status") == "blocked"
    assert risk_result.get("reason_code") == "kill_look_failed_closed"
    assert sent == []
