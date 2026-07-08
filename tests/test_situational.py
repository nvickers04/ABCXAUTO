"""Situational awareness — inventory prompt + pre-execution validation."""

from abcxauto.rocket import (
    format_position_inventory,
    position_key,
    validate_action_against_inventory,
)

MIXED = [
    {"symbol": "SPY", "quantity": 100, "sec_type": "STK", "unrealized_pnl": 50.0, "con_id": 1},
    {
        "symbol": "SPY", "quantity": 2, "sec_type": "OPT", "unrealized_pnl": -20.0,
        "expiration": "20260718", "strike": 450.0, "right": "C", "con_id": 99,
    },
]


def test_position_key_distinguishes_stk_and_opt():
    assert position_key(MIXED[0]) != position_key(MIXED[1])
    # conId preferred (protocol: single source of truth)
    assert "conId=99" in position_key(MIXED[1])
    assert "conId=1" in position_key(MIXED[0])


def test_format_position_inventory_lists_all_fields():
    text = format_position_inventory(MIXED)
    assert "LIVE POSITION LEDGER" in text
    assert "SPY" in text
    assert "expiry=20260718" in text
    assert "conId=99" in text


def test_validate_rejects_stock_close_when_only_option_held():
    ok, msg = validate_action_against_inventory(
        {"strategy": "market_order", "params": {"symbol": "SPY", "action": "SELL", "quantity": 100}, "target_conId": "99"},  # wrong conId type
        [MIXED[1]],
    )
    assert not ok
    assert "target_conId" in msg or "not found" in msg or "symbol-only" in msg.lower()


def test_validate_accepts_close_option_for_opt_leg():
    ok, msg = validate_action_against_inventory(
        {
            "strategy": "close_option",
            "params": {
                "symbol": "SPY", "expiration": "20260718", "strike": 450.0,
                "right": "C", "quantity": 2,
            },
            "target_conId": "99",
        },
        MIXED,
    )
    assert ok
    assert "validated" in msg or ok is True


def test_validate_accepts_stock_sell_for_stk_only():
    ok, _ = validate_action_against_inventory(
        {"strategy": "market_order", "params": {"symbol": "SPY", "action": "SELL", "quantity": 50}, "target_conId": "1"},
        [MIXED[0]],
    )
    assert ok