"""RTH kill-look send enum + defined-risk stock brackets.

Offline. Does not start looking, TWS, or 7496.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from abcxauto.order_examples import NOT_TICKETS, ORDER_EXAMPLES
from abcxauto.proposals import OrderProposal, STRATEGIES, validate_proposal
from abcxauto.risk_gates import check_defined_risk_only, reset_risk_gate
from abcxauto.thin_rth_kill_look import (
    REASON_STRUCTURE,
    pcs_send_ok,
    send_strategy_names,
)
from tests.test_proposals import RATIONALE, VALID_PAYLOADS
from tests.test_risk_gates import (
    FakeConnector,
    _bracket,
    _cfg,
    _siri_market_bracket,
)
from tests.test_thin_rth_kill_look import MODE_OPEN, _kill_on


PRE_FIX_ENUM = ["vertical_spread"]

# Pre-fix send_strategy_names returned exactly this. The fix must widen it.
EXPECTED_RTH_KILL_LOOK = [
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "butterfly",
    "calendar_spread",
    "diagonal_spread",
    "cash_secured_put",
    "covered_call",
    "protective_put",
    "collar",
    "roll_option",
    "buy_option",
    "straddle",
    "strangle",
    "bracket",
    "market_bracket",
    "oca",
    "cancel_order",
    "modify_stop",
    "modify_target",
]

LEFT_OUT_UNLIMITED = ("ratio_spread", "jade_lizard")
LEFT_OUT_EXIT = ("close_option", "market_order", "limit_order")

_UNDEFINED_STK_REASON = "undefined STK risk"


class _NakedStkParams(BaseModel):
    symbol: str
    quantity: int
    direction: str


def _naked_stk(strategy: str = "market_bracket", symbol: str = "SIRI") -> OrderProposal:
    """A stock ticket with no stop. Schema-valid brackets cannot do this."""
    return OrderProposal(
        id=1,
        strategy=strategy,
        params=_NakedStkParams(symbol=symbol, quantity=10, direction="LONG"),
        rationale="naked",
    )


def test_pre_fix_send_enum_was_vertical_only(monkeypatch):
    """Records the bug: kill-look send enum was vertical_spread only."""
    _kill_on(monkeypatch)
    names = send_strategy_names(session="regular")
    # After the fix this must be wider than the pre-fix list.
    assert names != PRE_FIX_ENUM
    assert names is not None
    for name in EXPECTED_RTH_KILL_LOOK:
        assert name in names, name


def test_send_strategy_names_offers_each_defined_risk_structure(monkeypatch):
    _kill_on(monkeypatch)
    names = send_strategy_names(session="regular")
    assert names == EXPECTED_RTH_KILL_LOOK
    assert names[0] == "vertical_spread"


def test_every_enum_name_has_schema_example_and_place_path(monkeypatch):
    _kill_on(monkeypatch)
    from abcxauto.broker.connector import IBKRConnector

    names = send_strategy_names(session="regular")
    assert names
    for name in names:
        assert name in ORDER_EXAMPLES, name
        assert name not in NOT_TICKETS, name
        assert name in STRATEGIES, name
        method = STRATEGIES[name][1]
        assert method, name
        assert callable(getattr(IBKRConnector, method, None)), (name, method)


def test_option_without_place_path_is_not_offered(monkeypatch):
    _kill_on(monkeypatch)
    from abcxauto.proposals import STRATEGIES as strat_map
    from abcxauto.strategy_params import VerticalSpreadParams

    monkeypatch.setitem(strat_map, "vertical_spread", (VerticalSpreadParams, ""))
    names = send_strategy_names(session="regular")
    assert "vertical_spread" not in names
    assert "iron_condor" in names


def test_unlimited_and_bare_stock_names_are_not_offered(monkeypatch):
    _kill_on(monkeypatch)
    names = set(send_strategy_names(session="regular") or [])
    for name in LEFT_OUT_UNLIMITED + LEFT_OUT_EXIT:
        assert name not in names, name


def test_bracket_with_stop_passes_defined_risk_only(monkeypatch):
    cfg = _cfg(defined_risk_only=True, risk_gates_enabled=True)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)

    ok, why = check_defined_risk_only(_bracket(symbol="NVDA"))
    assert ok is True, why
    ok_mb, why_mb = check_defined_risk_only(_siri_market_bracket())
    assert ok_mb is True, why_mb
    oca = validate_proposal(
        "oca",
        {
            "symbol": "NVDA",
            "quantity": 10,
            "direction": "LONG",
            "stop_price": 97.0,
            "target_price": 106.0,
        },
        RATIONALE,
        quote_last=100.0,
    )
    ok_oca, why_oca = check_defined_risk_only(oca)
    assert ok_oca is True, why_oca


def test_naked_stock_still_refused_with_same_reason(monkeypatch):
    cfg = _cfg(defined_risk_only=True)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)

    for strat in ("market_bracket", "bracket"):
        ok, why = check_defined_risk_only(_naked_stk(strat))
        assert ok is False
        assert "defined_risk_only" in why
        assert _UNDEFINED_STK_REASON in why

    with pytest.raises((ValidationError, Exception)):
        validate_proposal(
            "market_order",
            {"symbol": "AAPL", "action": "BUY", "quantity": 5},
            RATIONALE,
        )


def test_naked_short_call_still_refused(monkeypatch):
    cfg = _cfg(defined_risk_only=True)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)

    ratio = validate_proposal("ratio_spread", VALID_PAYLOADS["ratio_spread"], RATIONALE)
    ok, why = check_defined_risk_only(ratio)
    assert ok is False
    assert "defined_risk_only" in why

    short = validate_proposal(
        "straddle",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "strike": 500.0,
            "quantity": 1,
            "action": "SELL",
        },
        RATIONALE,
    )
    ok2, why2 = check_defined_risk_only(short)
    assert ok2 is False
    assert "short" in why2.lower()


@pytest.mark.asyncio
async def test_cash_only_still_refuses_short_and_bounds_notional(monkeypatch):
    cfg = _cfg(
        defined_risk_only=True,
        cash_only=True,
        max_position_pct=0,
        daily_loss_limit_pct=0,
        max_open_positions=0,
    )
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    gate = reset_risk_gate()
    short_conn = FakeConnector(
        account={
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 50_000.0,
        }
    )
    ok, reason = await gate.pre_trade_check(
        _bracket(direction="SHORT", entry=100.0, stop=105.0, target=90.0),
        short_conn,
    )
    assert ok is False
    assert "cash-only" in reason.lower() or "short" in reason.lower()

    tight = FakeConnector(
        account={
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 5_000.0,
        }
    )
    ok2, reason2 = await gate.pre_trade_check(_bracket(qty=200, entry=100.0), tight)
    assert ok2 is False
    assert "cash" in reason2.lower()


def test_last_stop_gate_still_blocks_unprotected_hold():
    """A stock lot whose stop is not resting at IBKR is still unholdable."""
    from abcxauto.agent_loop import gate_ticket
    from abcxauto.monitor import build_protection_report
    from abcxauto.protect import last_stop_block_reason
    from abcxauto.protect_reconciler import last_stop_covers_lot
    from tests.test_agent_loop import _world

    lot = {"symbol": "AAPL", "quantity": 10, "sec_type": "STK", "secType": "STK"}
    assert last_stop_covers_lot(lot, []) is False
    report = build_protection_report([lot], [])
    assert report["unprotected_symbols"]
    world = _world(
        needs_protection=True,
        unprotected=list(report["unprotected_symbols"]),
        flat=False,
        positions=[lot],
    )
    strat, forced = gate_ticket({"action": "hold", "strategy": "hold"}, world)
    assert strat == "blocked"
    assert "hold_forbidden" in str((forced or {}).get("note") or "")

    covering = [
        {
            "order_id": 9,
            "symbol": "AAPL",
            "sec_type": "STK",
            "action": "SELL",
            "quantity": 10,
            "order_type": "STP",
        }
    ]
    block = last_stop_block_reason(9, covering, [lot])
    assert block is not None
    assert "only working stop" in block


@pytest.mark.asyncio
async def test_protected_bracket_still_cannot_cancel_only_last_stop(monkeypatch):
    """Allowing a bracket through defined_risk_only must not weaken last-stop."""
    from abcxauto.executor import execute_proposal
    from abcxauto.protect import last_stop_block_reason

    cfg = _cfg(defined_risk_only=True, risk_gates_enabled=True)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.executor.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)

    covering = [
        {
            "order_id": 9,
            "symbol": "AAPL",
            "sec_type": "STK",
            "action": "SELL",
            "quantity": 10,
            "order_type": "STP",
        }
    ]
    positions = [
        {"symbol": "AAPL", "quantity": 10, "sec_type": "STK", "secType": "STK"}
    ]

    class GW(FakeConnector):
        def __init__(self):
            super().__init__(positions=positions)
            self.cancelled = []

        async def get_open_orders(self):
            return covering

        async def cancel_order(self, order_id):
            self.cancelled.append(order_id)
            raise AssertionError("last-stop must not cancel")

    cancel = validate_proposal("cancel_order", {"order_id": 9}, RATIONALE)
    result = await execute_proposal(cancel, GW())
    assert result.get("success") is not True
    assert "only working stop" in str(result.get("error") or result)
    assert last_stop_block_reason(9, covering, positions) is not None


def test_pcs_send_ok_accepts_named_bracket_and_condor(monkeypatch):
    _kill_on(monkeypatch)
    ok, why = pcs_send_ok(
        "market_bracket",
        {
            "symbol": "NVDA",
            "quantity": 10,
            "direction": "LONG",
            "stop_price": 97.0,
            "target_price": 106.0,
            "card": "nvda-long",
        },
        "nvda-long",
        mode=MODE_OPEN,
    )
    assert ok is True, why

    ok2, why2 = pcs_send_ok(
        "iron_condor",
        {**dict(ORDER_EXAMPLES["iron_condor"]), "card": "spy-ic"},
        "spy-ic",
        mode=MODE_OPEN,
    )
    assert ok2 is True, why2

    ok3, why3 = pcs_send_ok(
        "ratio_spread",
        {**dict(ORDER_EXAMPLES["ratio_spread"]), "card": "bad"},
        "bad",
        mode=MODE_OPEN,
    )
    assert ok3 is False
    assert why3 == REASON_STRUCTURE
