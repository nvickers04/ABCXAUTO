"""Send gate: ticket last / IV / credit / width must be in this look's cache."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.look_snapshot import (
    REASON_CODE,
    begin_look,
    check_ticket_numbers,
    record_look_tool,
    snapshot_bags,
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


def _world(**kwargs) -> "object":
    from abcxauto.world_state import WorldState

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


def _nok_legs() -> list[dict]:
    return [
        {
            "symbol": "NOK",
            "secType": "OPT",
            "sec_type": "OPT",
            "quantity": 100,
            "expiration": "20260925",
            "right": "C",
            "strike": 10.0,
            "conId": 11001,
        },
        {
            "symbol": "NOK",
            "secType": "OPT",
            "sec_type": "OPT",
            "quantity": -100,
            "expiration": "20260925",
            "right": "C",
            "strike": 10.5,
            "conId": 11002,
        },
    ]


def _nok_ticket(*, closing: bool) -> dict:
    params = {
        "long_strike": 10.0,
        "short_strike": 10.5,
        "symbol": "NOK",
        "quantity": 100.0,
        "limit_price": 0.14,
        "expiration": "20260925",
        "right": "C",
        "card": "defined-risk exit",
        "closing_position": True if closing else False,
    }
    return {
        "action": "vertical_spread",
        "strategy": "vertical_spread",
        "params": params,
        "rationale": "Defined-risk exit, not new risk.",
    }


def _empty_look_snap(positions: list[dict], *, book_unreliable: bool = False) -> dict:
    snap = {
        "account": {"netliquidation": 37000.0},
        "positions": positions,
        "open_orders": [],
        "book_unreliable": book_unreliable,
    }
    begin_look(snap)
    return snap


def _capture_send(monkeypatch) -> list:
    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    return sent


@pytest.mark.asyncio
async def test_nok_closing_vertical_empty_cache_is_not_number_gated(monkeypatch):
    """Journal 356: NOK 10/10.5C close refused by look_numbers with an empty cache."""
    from abcxauto.agent_loop import execute_ticket

    legs = _nok_legs()
    sent = _capture_send(monkeypatch)
    result = await execute_ticket(
        _nok_ticket(closing=True),
        MagicMock(),
        _world(flat=False, positions=legs),
        _empty_look_snap(legs),
    )
    assert result.get("reason_code") != REASON_CODE
    assert "stale_or_invented_number" not in str(result.get("note") or "")
    assert result.get("status") == "ok"
    assert sent and sent[0]["strategy"] == "vertical_spread"
    assert sent[0]["params"]["closing_position"] is True
    assert sent[0]["params"]["limit_price"] == 0.14


@pytest.mark.asyncio
async def test_nok_opening_vertical_empty_cache_still_number_gated(monkeypatch):
    """Anti-regression: the same ticket as new risk still dies on look_numbers."""
    from abcxauto.agent_loop import execute_ticket

    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    legs = _nok_legs()
    sent = _capture_send(monkeypatch)
    ticket = _nok_ticket(closing=False)
    result = await execute_ticket(
        ticket,
        MagicMock(),
        _world(flat=False, positions=legs),
        _empty_look_snap(legs),
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == REASON_CODE == "stale_or_invented_number"
    note = str(result.get("note") or "")
    assert "limit_price=0.14" in note
    assert "closing_position=false" in note
    assert "legs missing from this look's cache" in note
    assert sent == []


def test_look_numbers_refusal_names_flag_and_missing_legs():
    snap = _empty_look_snap([])
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        _nok_ticket(closing=True)["params"],
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "limit_price=0.14" in msg
    assert "closing_position=true" in msg
    assert "legs missing from this look's cache" in msg


def test_look_numbers_refusal_names_prints_when_cache_has_them():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "NOK",
            "expiration": "20260925",
            "right": "C",
            "strike": 10.0,
            "ibkr": {"last": 0.20, "bid": 0.19, "ask": 0.21, "mid": 0.20},
        },
    )
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        {
            "symbol": "NOK",
            "expiration": "20260925",
            "right": "C",
            "long_strike": 10.0,
            "short_strike": 10.5,
            "limit_price": 0.14,
            "closing_position": False,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "limit_price=0.14" in msg
    assert "closing_position=false" in msg
    assert "prints=[" in msg
    assert "0.2" in msg or "0.19" in msg
    assert "legs missing" not in msg


@pytest.mark.asyncio
async def test_exit_refused_when_book_unreliable(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    sent = _capture_send(monkeypatch)
    legs = _nok_legs()
    result = await execute_ticket(
        _nok_ticket(closing=True),
        MagicMock(),
        _world(flat=False, positions=legs, gates={"book_unreliable": True}),
        _empty_look_snap(legs, book_unreliable=True),
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == "book_unreliable"
    assert "unreliable" in str(result.get("note") or "").lower()
    assert result.get("reason_code") != REASON_CODE
    assert sent == []


@pytest.mark.asyncio
async def test_close_option_empty_cache_is_not_number_gated(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    pos = [{
        "symbol": "IBIT",
        "secType": "OPT",
        "sec_type": "OPT",
        "quantity": 2,
        "expiration": "20260925",
        "right": "C",
        "strike": 50.0,
        "conId": 22001,
    }]
    sent = _capture_send(monkeypatch)
    result = await execute_ticket(
        {
            "action": "close_option",
            "strategy": "close_option",
            "params": {
                "symbol": "IBIT",
                "conId": 22001,
                "quantity": 2,
                "limit_price": 0.55,
                "expiration": "20260925",
                "right": "C",
                "strike": 50.0,
            },
            "rationale": "close the call",
        },
        MagicMock(),
        _world(flat=False, positions=pos),
        _empty_look_snap(pos),
    )
    assert result.get("reason_code") != REASON_CODE
    assert "stale_or_invented_number" not in str(result.get("note") or "")
    assert result.get("status") == "ok"
    assert sent and sent[0]["strategy"] == "close_option"


@pytest.mark.asyncio
async def test_closing_market_order_empty_cache_is_not_number_gated(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    pos = [{
        "symbol": "NKE",
        "secType": "STK",
        "sec_type": "STK",
        "quantity": 70,
        "conId": 9,
    }]
    sent = _capture_send(monkeypatch)
    result = await execute_ticket(
        {
            "action": "market_order",
            "strategy": "market_order",
            "params": {
                "symbol": "NKE",
                "action": "SELL",
                "quantity": 70,
                "closing_position": True,
                "conId": 9,
            },
            "rationale": "flatten",
        },
        MagicMock(),
        _world(flat=False, positions=pos),
        _empty_look_snap(pos),
    )
    assert result.get("reason_code") != REASON_CODE
    assert result.get("status") == "ok"
    assert sent and sent[0]["strategy"] == "market_order"


def test_bracket_oca_close_still_skips_stop_target_claims():
    close = {"closing_position": True, "stop_price": 12.34, "target_price": 15.0, "symbol": "SPY"}
    assert ticket_claims("bracket", close) == []
    assert ticket_claims("oca", close) == []
    assert ticket_claims("market_bracket", close) == []
    opening = {"stop_price": 12.34, "target_price": 15.0, "symbol": "SPY"}
    claimed = {field for _kind, field, _val in ticket_claims("bracket", opening)}
    assert claimed == {"stop_price", "target_price"}


@pytest.mark.asyncio
async def test_bracket_close_empty_cache_is_not_number_gated(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    pos = [{
        "symbol": "SPY",
        "secType": "STK",
        "sec_type": "STK",
        "quantity": 10,
        "conId": 7,
    }]
    sent = _capture_send(monkeypatch)
    result = await execute_ticket(
        {
            "action": "bracket",
            "strategy": "bracket",
            "params": {
                "symbol": "SPY",
                "quantity": 10,
                "direction": "LONG",
                "stop_price": 12.34,
                "target_price": 15.0,
                "closing_position": True,
                "conId": 7,
            },
            "rationale": "close the bracket",
        },
        MagicMock(),
        _world(flat=False, positions=pos),
        _empty_look_snap(pos),
    )
    assert result.get("reason_code") != REASON_CODE
    assert "stale_or_invented_number" not in str(result.get("note") or "")
    assert "limit_price" not in str(result.get("note") or "")
    # Later geometry may still refuse; this gate must not.
    assert sent == [] or result.get("status") == "ok"

# extra tests appended by combo-net fix — keep helpers local to this block

def _record_ibkr_opt_leg(
    snap: dict,
    *,
    symbol: str,
    expiration: str,
    strike: float,
    right: str,
    bid: float,
    ask: float,
    mid: float | None = None,
    last: float | None = None,
) -> None:
    if mid is None:
        mid = round((bid + ask) / 2.0, 4)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": symbol,
            "expiration": expiration,
            "strike": strike,
            "right": right,
            "ibkr": {
                "last": last if last is not None else mid,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "source": "ibkr",
                "freshness": "live",
            },
        },
    )


def _spy_750_745_ticket(*, limit_price: float, closing: bool = False) -> dict:
    return {
        "symbol": "SPY",
        "expiration": "20260918",
        "right": "P",
        "long_strike": 750.0,
        "short_strike": 745.0,
        "quantity": 1,
        "limit_price": limit_price,
        "closing_position": closing,
    }


def _record_spy_750_745_legs(snap: dict) -> None:
    # Live refusal 2026-09-16: 750P 2.27/2.28, 745P 1.47/1.48, sent 0.78.
    _record_ibkr_opt_leg(
        snap,
        symbol="SPY",
        expiration="20260918",
        strike=750.0,
        right="P",
        bid=2.27,
        ask=2.28,
        mid=2.275,
    )
    _record_ibkr_opt_leg(
        snap,
        symbol="SPY",
        expiration="20260918",
        strike=745.0,
        right="P",
        bid=1.47,
        ask=1.48,
        mid=1.475,
    )


def test_spy_750_745p_net_078_is_allowed_from_leg_prints():
    """Exact live case: natural credit ~0.80, sent 0.78 as new risk."""
    snap: dict = {}
    begin_look(snap)
    _record_spy_750_745_legs(snap)
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        _spy_750_745_ticket(limit_price=0.78),
        snap,
    )
    assert ok is True, msg
    assert code == "ok"
    assert msg == ""


def test_live_bag_quote_is_found_and_verifies_combo_net():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "expiration": "20260918",
            "long_strike": 750.0,
            "short_strike": 745.0,
            "right": "P",
            "sec": "BAG",
            "bid": 0.79,
            "ask": 0.81,
            "last": 0.80,
            "mid": 0.80,
            "source": "ibkr",
            "freshness": "live",
        },
    )
    by_inst = snapshot_bags(snap)
    bag_keys = [k for k in by_inst if k[0] == "BAG"]
    assert bag_keys, f"BAG row stored but not keyed as BAG: {list(by_inst)}"
    want_pair = tuple(sorted((int(round(745.0 * 10000.0)), int(round(750.0 * 10000.0)))))
    assert any(
        k[1] == "SPY" and k[2] == "20260918" and k[3] == "P" and k[4] == want_pair
        for k in bag_keys
    ), bag_keys
    # Wildcard OPT-without-strike would also verify a *different* combo.
    other = dict(_spy_750_745_ticket(limit_price=0.80))
    other["long_strike"] = 740.0
    other["short_strike"] = 735.0
    other_ok, other_code, other_msg = check_ticket_numbers(
        "vertical_spread", other, snap
    )
    assert other_ok is False
    assert other_code == REASON_CODE
    assert "0.8" in other_msg
    for px in (0.79, 0.80, 0.81):
        ok, code, msg = check_ticket_numbers(
            "vertical_spread",
            _spy_750_745_ticket(limit_price=px),
            snap,
        )
        assert ok is True, (px, msg)
        assert code == "ok"


def test_nok_014_inside_derived_combo_range_is_allowed():
    snap: dict = {}
    begin_look(snap)
    _record_ibkr_opt_leg(
        snap, symbol="NOK", expiration="20260925", strike=10.0, right="C",
        bid=0.31, ask=0.35, mid=0.33,
    )
    _record_ibkr_opt_leg(
        snap, symbol="NOK", expiration="20260925", strike=10.5, right="C",
        bid=0.16, ask=0.18, mid=0.17,
    )
    ticket = {
        "symbol": "NOK",
        "expiration": "20260925",
        "right": "C",
        "long_strike": 10.0,
        "short_strike": 10.5,
        "limit_price": 0.14,
        "closing_position": False,
    }
    ok, code, msg = check_ticket_numbers("vertical_spread", ticket, snap)
    assert ok is True, msg
    assert code == "ok"
    far = dict(ticket)
    far["limit_price"] = 0.60
    bad, bcode, bmsg = check_ticket_numbers("vertical_spread", far, snap)
    assert bad is False
    assert bcode == REASON_CODE
    assert "0.6" in bmsg
    assert "option_quote" in bmsg
    assert "long_strike" in bmsg
    assert "short_strike" in bmsg
    assert "derived_combo" in bmsg
    assert "0.13" in bmsg
    assert "0.19" in bmsg


def test_mda_price_cannot_verify_combo_net():
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "expiration": "20260918",
            "strike": 750.0,
            "right": "P",
            "ibkr": {"error": "no IBKR tick yet", "source": "ibkr"},
            "mda": {
                "last": 0.78,
                "bid": 2.27,
                "ask": 2.28,
                "mid": 2.275,
                "source": "marketdata",
                "freshness": "delayed",
            },
        },
    )
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "expiration": "20260918",
            "strike": 745.0,
            "right": "P",
            "ibkr": {"error": "no IBKR tick yet", "source": "ibkr"},
            "mda": {
                "last": 0.78,
                "bid": 1.47,
                "ask": 1.48,
                "mid": 1.475,
                "source": "marketdata",
                "freshness": "delayed",
            },
        },
    )
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        _spy_750_745_ticket(limit_price=0.78),
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "0.78" in msg


def test_prior_look_number_cannot_verify_combo_net():
    snap: dict = {}
    begin_look(snap)
    _record_spy_750_745_legs(snap)
    begin_look(snap)
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        _spy_750_745_ticket(limit_price=0.78),
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "0.78" in msg
    assert "legs missing from this look's cache" in msg


def test_single_leg_print_does_not_verify_combo_net():
    snap: dict = {}
    begin_look(snap)
    _record_ibkr_opt_leg(
        snap,
        symbol="SPY",
        expiration="20260918",
        strike=750.0,
        right="P",
        bid=2.27,
        ask=2.28,
        mid=2.275,
    )
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        _spy_750_745_ticket(limit_price=2.27),
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "2.27" in msg
    assert "option_quote" in msg
    assert "long_strike" in msg


def test_combo_refusal_names_tool_and_derived_market():
    snap: dict = {}
    begin_look(snap)
    _record_ibkr_opt_leg(
        snap, symbol="NOK", expiration="20260925", strike=10.0, right="C",
        bid=0.31, ask=0.35, mid=0.33,
    )
    _record_ibkr_opt_leg(
        snap, symbol="NOK", expiration="20260925", strike=10.5, right="C",
        bid=0.16, ask=0.18, mid=0.17,
    )
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        {
            "symbol": "NOK",
            "expiration": "20260925",
            "right": "C",
            "long_strike": 10.0,
            "short_strike": 10.5,
            "limit_price": 0.60,
            "closing_position": False,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "option_quote" in msg
    assert "long_strike" in msg
    assert "short_strike" in msg
    assert "derived_combo bid=0.13" in msg
    assert "ask=0.19" in msg


def test_omitted_limit_price_on_new_risk_combo_is_refused():
    snap: dict = {}
    begin_look(snap)
    _record_spy_750_745_legs(snap)
    params = _spy_750_745_ticket(limit_price=0.78)
    del params["limit_price"]
    ok, code, msg = check_ticket_numbers("vertical_spread", params, snap)
    assert ok is False
    assert code == REASON_CODE
    assert "limit_price" in msg
    mkt = dict(params)
    mkt["order_type"] = "MKT"
    mok, mcode, mmsg = check_ticket_numbers("vertical_spread", mkt, snap)
    assert mok is False
    assert mcode == REASON_CODE
    assert "limit_price" in mmsg


@pytest.mark.asyncio
async def test_closing_vertical_still_not_number_gated(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    legs = _nok_legs()
    sent = _capture_send(monkeypatch)
    ticket = _nok_ticket(closing=True)
    ticket["params"]["limit_price"] = 4.50
    result = await execute_ticket(
        ticket,
        MagicMock(),
        _world(flat=False, positions=legs),
        _empty_look_snap(legs),
    )
    assert result.get("reason_code") != REASON_CODE
    assert "stale_or_invented_number" not in str(result.get("note") or "")
    assert result.get("status") == "ok"
    assert sent and sent[0]["params"]["limit_price"] == 4.50
