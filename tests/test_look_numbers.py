"""Send gate: ticket last / IV / credit / width must be in this look's cache."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.look_snapshot import (
    REASON_CODE,
    _combo_marketable_slack,
    _in_pool,
    _vertical_combo_context,
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


def _vertical_snap(
    symbol: str,
    expiration: str,
    long_strike: float,
    short_strike: float,
    right: str,
    long_bid: float,
    long_ask: float,
    short_bid: float,
    short_ask: float,
) -> dict:
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "quotes": [
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "strike": long_strike,
                    "right": right,
                    "ibkr": {
                        "bid": long_bid,
                        "ask": long_ask,
                        "mid": (long_bid + long_ask) / 2,
                    },
                },
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "strike": short_strike,
                    "right": right,
                    "ibkr": {
                        "bid": short_bid,
                        "ask": short_ask,
                        "mid": (short_bid + short_ask) / 2,
                    },
                },
            ]
        },
    )
    return snap


def _aapl_vertical_snap(long_bid, long_ask, short_bid, short_ask) -> dict:
    return _vertical_snap("AAPL", "20260925", 330.0, 335.0, "C", long_bid, long_ask, short_bid, short_ask)


def _vertical_params(limit_price: float, **extra) -> dict:
    return {
        "symbol": "AAPL",
        "expiration": "20260925",
        "long_strike": 330.0,
        "short_strike": 335.0,
        "right": "C",
        "quantity": 12,
        "limit_price": limit_price,
        "closing_position": False,
        **extra,
    }


def _verbatim_would_accept(limit: float, snap: dict, symbol: str = "AAPL") -> bool:
    bag = snapshot_bags(snap).get(symbol)
    pool = bag.prints if bag is not None else set()
    return bool(pool) and _in_pool(pool, limit)


@pytest.mark.parametrize(
    "limit_price,expect_ok",
    [
        (3.40, False),
        (2.35, True),
        (2.40, True),
        (5.50, False),
    ],
)
def test_unified_combo_limit_table_aapl_vertical(limit_price, expect_ok):
    # Natural debit ask = long ask - short bid = 4.61 - 2.26 = 2.35 (not a leg print)
    snap = _aapl_vertical_snap(4.50, 4.61, 2.26, 2.37)
    params = _vertical_params(limit_price)
    assert _verbatim_would_accept(limit_price, snap) is False
    ok, code, msg = check_ticket_numbers("vertical_spread", params, snap)
    assert ok is expect_ok
    if expect_ok:
        assert code == "ok"
        assert msg == ""
    else:
        assert code == REASON_CODE
        assert "combo_debit" in msg
        assert f"limit_price={limit_price}" in msg
        assert "bid=" in msg and "ask=" in msg and "mid=" in msg
        assert "this look's quote/option_quote/book" in msg


def test_derived_combo_natural_passes_even_when_not_verbatim():
    snap = _aapl_vertical_snap(4.50, 4.61, 2.26, 2.37)
    for limit in (2.35, 2.40):
        assert _verbatim_would_accept(limit, snap) is False
        ok, code, msg = check_ticket_numbers(
            "vertical_spread", _vertical_params(limit), snap
        )
        assert ok is True
        assert code == "ok"
        assert msg == ""


def test_leg_price_used_as_combo_limit_is_refused():
    snap = _aapl_vertical_snap(3.40, 4.61, 2.26, 2.37)
    params = _vertical_params(3.40)
    assert _verbatim_would_accept(3.40, snap) is True
    ok, code, msg = check_ticket_numbers("vertical_spread", params, snap)
    assert ok is False
    assert code == REASON_CODE
    assert "combo_debit" in msg
    assert "ask=2.35" in msg or "ask=2.3500" in msg


def test_combo_limit_fails_closed_without_both_legs():
    """A long-leg last must not pass as a combo debit when the short is missing."""
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "XOM",
            "expiration": "20260925",
            "strike": 170.0,
            "right": "C",
            "ibkr": {"last": 3.0, "bid": 2.95, "ask": 3.05},
        },
    )
    params = {
        "symbol": "XOM",
        "expiration": "20260925",
        "long_strike": 170.0,
        "short_strike": 175.0,
        "right": "C",
        "quantity": 13,
        "limit_price": 3.0,
        "closing_position": False,
    }
    assert _verbatim_would_accept(3.0, snap, "XOM") is True
    ok, code, msg = check_ticket_numbers("vertical_spread", params, snap)
    assert ok is False
    assert code == REASON_CODE
    assert "option_quote both legs" in msg
    assert "170" in msg and "175" in msg


def test_xom_long_leg_last_refused_as_combo_debit():
    """Live 2026-09-15: BAG LMT 3.00 vs IBKR natural 1.84 (order 21107)."""
    snap = _vertical_snap("XOM", "20260925", 170.0, 175.0, "C", 2.95, 3.05, 1.16, 1.21)
    params = {
        "symbol": "XOM",
        "expiration": "20260925",
        "long_strike": 170.0,
        "short_strike": 175.0,
        "right": "C",
        "quantity": 13,
        "limit_price": 3.0,
        "closing_position": False,
    }
    # combo ask = 3.05 - 1.16 = 1.89; IBKR named 1.84
    assert _verbatim_would_accept(3.0, snap, "XOM") is True
    ok, code, msg = check_ticket_numbers("vertical_spread", params, snap)
    assert ok is False
    assert code == REASON_CODE
    assert "combo_debit" in msg
    ok_near, _, _ = check_ticket_numbers(
        "vertical_spread", {**params, "limit_price": 1.85}, snap
    )
    assert ok_near is True


def test_combo_limit_last_only_quotes_fail_closed():
    """Bid/ask missing cannot invent geometry, and last=3.00 is not a debit."""
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "quotes": [
                {
                    "symbol": "XOM",
                    "expiration": "20260925",
                    "strike": 170.0,
                    "right": "C",
                    "ibkr": {"last": 3.0},
                },
                {
                    "symbol": "XOM",
                    "expiration": "20260925",
                    "strike": 175.0,
                    "right": "C",
                    "ibkr": {"last": 1.16},
                },
            ]
        },
    )
    params = {
        "symbol": "XOM",
        "expiration": "20260925",
        "long_strike": 170.0,
        "short_strike": 175.0,
        "right": "C",
        "limit_price": 3.0,
        "closing_position": False,
    }
    assert _verbatim_would_accept(3.0, snap, "XOM") is True
    ok, code, msg = check_ticket_numbers("vertical_spread", params, snap)
    assert ok is False
    assert "option_quote bid/ask" in msg
    assert "170" in msg and "175" in msg


def test_non_vertical_option_limit_still_verbatim():
    snap = _aapl_vertical_snap(4.50, 4.61, 2.26, 2.37)
    ok, code, msg = check_ticket_numbers(
        "iron_condor",
        {
            "symbol": "AAPL",
            "limit_price": 99.0,
            "put_long_strike": 300.0,
            "put_short_strike": 310.0,
            "call_short_strike": 350.0,
            "call_long_strike": 360.0,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE


@pytest.mark.parametrize(
    "symbol,limit,long_ask,short_bid",
    [
        ("XLF", 0.56, 1.06, 0.50),
        ("IBIT", 0.43, 0.93, 0.50),
        ("SPY", 2.40, 3.00, 0.60),
        ("IBIT", 0.66, 1.16, 0.50),
        ("XLF", 0.32, 0.82, 0.50),
        ("QQQ", 2.70, 3.20, 0.50),
        ("QQQ", 2.63, 3.13, 0.50),
        ("SPY", 1.79, 2.29, 0.50),
        ("QQQ", 3.10, 3.60, 0.50),
        ("QQQ", 3.12, 3.62, 0.50),
        ("SPY", 2.96, 3.46, 0.50),
    ],
)
def test_historical_derived_limits_now_pass(symbol, limit, long_ask, short_bid):
    """Past stale_or_invented blocks on correct derived combo prices."""
    snap = _vertical_snap(symbol, "20260718", 50.0, 55.0, "C", 0.10, long_ask, short_bid, 0.55)
    assert _verbatim_would_accept(limit, snap, symbol) is False
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        {
            "symbol": symbol,
            "expiration": "20260718",
            "long_strike": 50.0,
            "short_strike": 55.0,
            "right": "C",
            "limit_price": limit,
        },
        snap,
    )
    assert ok is True
    assert code == "ok"
    assert msg == ""


def _nok_narrow_snap(long_bid, long_ask, short_bid, short_ask) -> dict:
    return _vertical_snap("NOK", "20260718", 10.0, 10.5, "C", long_bid, long_ask, short_bid, short_ask)


def _nok_params(limit_price: float, **extra) -> dict:
    return {
        "symbol": "NOK",
        "expiration": "20260718",
        "long_strike": 10.0,
        "short_strike": 10.5,
        "right": "C",
        "quantity": 100,
        "limit_price": limit_price,
        "closing_position": False,
        **extra,
    }


@pytest.mark.parametrize(
    "limit_price,expect_ok",
    [
        (0.20, True),
        (0.19, True),
        (0.27, False),
        (0.24, True),
        (0.25, False),
    ],
)
def test_nok_narrow_spread_proportional_slack(limit_price, expect_ok):
    # Natural debit ask = 0.69 - 0.50 = 0.19; width 0.50 -> slack max(tick, 4% width) = 0.05
    snap = _nok_narrow_snap(0.10, 0.69, 0.50, 0.55)
    ctx = _vertical_combo_context(_nok_params(0.19), snap)
    assert ctx is not None
    assert abs(float(ctx["ask"]) - 0.19) < 1e-9
    assert _combo_marketable_slack(ctx) == 0.05
    ok, code, msg = check_ticket_numbers(
        "vertical_spread", _nok_params(limit_price), snap
    )
    assert ok is expect_ok
    if not expect_ok:
        assert code == REASON_CODE
        assert "combo_debit" in msg


def test_combo_slack_ignores_quantity():
    snap = _nok_narrow_snap(0.10, 0.69, 0.50, 0.55)
    one = check_ticket_numbers(
        "vertical_spread", _nok_params(0.27, quantity=1), snap
    )
    hundred = check_ticket_numbers(
        "vertical_spread", _nok_params(0.27, quantity=100), snap
    )
    assert one == hundred
    assert one[0] is False


def test_aapl_wide_spread_slack_scales_to_ceiling():
    snap = _aapl_vertical_snap(4.50, 4.61, 2.26, 2.37)
    ctx = _vertical_combo_context(_vertical_params(2.35), snap)
    assert ctx is not None
    assert _combo_marketable_slack(ctx) == 0.20


def test_broker_cancel_case_too_aggressive_derived_limit_refused():
    # 09-09: market ~1.6, IBKR refused >= 1.925
    snap = _vertical_snap("SPY", "20260718", 500.0, 505.0, "C", 1.0, 2.0, 0.4, 0.5)
    ok, code, msg = check_ticket_numbers(
        "vertical_spread",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "long_strike": 500.0,
            "short_strike": 505.0,
            "right": "C",
            "limit_price": 1.925,
        },
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
    assert "combo_debit" in msg


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
        {"symbol": "SPY", "price_hint": 500.12, "stop_price": 495.0, "target_price": 510.0},
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


@pytest.mark.asyncio
async def test_execute_ticket_rejects_implausible_combo_debit(monkeypatch):
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
    snap = _aapl_vertical_snap(3.40, 4.61, 2.26, 2.37)
    snap["account"] = {"netliquidation": 37000.0}
    snap["positions"] = []
    snap["open_orders"] = []
    result = await execute_ticket(
        {
            "action": "vertical_spread",
            "strategy": "vertical_spread",
            "params": _vertical_params(3.40),
            "rationale": "invented combo debit",
        },
        MagicMock(),
        world,
        snap,
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == REASON_CODE
    assert "combo_debit" in str(result.get("note") or "")
    assert "3.4" in str(result.get("note") or "")
    assert sent == []
