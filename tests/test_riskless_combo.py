"""One working riskless BAG is allowed. A second must not reach TWS."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.broker.connector import IBKRConnector, combo_legs_from_contract
from abcxauto.broker.options import IBKROptionsMixin
from abcxauto.config import Config, get_config
from abcxauto.executor import execute_proposal
from abcxauto.proposals import validate_proposal
from abcxauto.risk_gates import get_risk_gate, reset_risk_gate
from abcxauto.riskless_combo import (
    REASON_CODE,
    is_riskless_combo_202,
    is_riskless_combo_strategy,
    order_is_working_riskless_combo,
    riskless_combo_block_reason,
)
from abcxauto.send import send_action
from tests.test_proposals import RATIONALE, VALID_PAYLOADS

_RISKLESS_202 = (
    "Riskless combination orders are not allowed. You have reached the "
    "maximum limit of active riskless/guaranteed-loss combination orders."
)


def _cfg(**overrides) -> Config:
    base = get_config()
    return Config(**{**base.__dict__, "risk_gates_enabled": False, "max_arena_concentration_pct": 0, **overrides})


@pytest.fixture(autouse=True)
def _isolate_gates(monkeypatch):
    reset_risk_gate()
    monkeypatch.setattr("abcxauto.executor.get_config", lambda: _cfg())
    monkeypatch.setattr("abcxauto.send.get_config", lambda: _cfg(ibkr_port=7497))
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    yield
    reset_risk_gate()


def _iron(symbol="IWM", **over):
    payload = dict(VALID_PAYLOADS["iron_condor"])
    payload["symbol"] = symbol
    payload.update(over)
    return validate_proposal("iron_condor", payload, RATIONALE)


def _fly(**over):
    payload = dict(VALID_PAYLOADS["butterfly"])
    payload.update(over)
    return validate_proposal("butterfly", payload, RATIONALE)


def _iron_fly(**over):
    payload = dict(VALID_PAYLOADS["iron_butterfly"])
    payload.update(over)
    return validate_proposal("iron_butterfly", payload, RATIONALE)


def _vertical(**over):
    payload = dict(VALID_PAYLOADS["vertical_spread"])
    payload.update(over)
    return validate_proposal("vertical_spread", payload, RATIONALE)


def _calendar(**over):
    payload = dict(VALID_PAYLOADS["calendar_spread"])
    payload.update(over)
    return validate_proposal("calendar_spread", payload, RATIONALE)


def _working_iron(symbol="IWM", oid=6384, status="Submitted"):
    return {
        "order_id": oid,
        "symbol": symbol,
        "sec_type": "BAG",
        "status": status,
        "strategy": "iron_condor",
        "combo_legs": [{}, {}, {}, {}],
        "action": "SELL",
        "quantity": 1,
        "order_type": "LMT",
    }


class FakeGW:
    def __init__(self, open_orders=None):
        self.connected = True
        self.open_orders = list(open_orders or [])
        self.calls = []

    async def get_open_orders(self):
        return self.open_orders

    async def get_positions(self):
        return []

    async def get_account_summary(self):
        return {"netliquidation": 100_000.0, "dailypnl": 0.0}

    def __getattr__(self, name):
        if not name.startswith(("place_", "buy_", "sell_", "close_", "roll_")):
            raise AttributeError(name)

        async def _method(**kwargs):
            self.calls.append((name, kwargs))
            return {"success": True, "order_id": 8000, "method": name}

        return _method


def test_classifies_iron_fly_not_vertical():
    assert is_riskless_combo_strategy("iron_condor")
    assert is_riskless_combo_strategy("Iron Butterfly")
    assert is_riskless_combo_strategy("Call Butterfly")
    assert not is_riskless_combo_strategy("vertical_spread")
    assert not is_riskless_combo_strategy("calendar_spread")
    assert order_is_working_riskless_combo(_working_iron())
    assert order_is_working_riskless_combo(
        {
            "sec_type": "BAG",
            "status": "PendingSubmit",
            "combo_legs": [
                {"ratio": 1},
                {"ratio": 2},
                {"ratio": 1},
            ],
        }
    )
    assert not order_is_working_riskless_combo(
        {
            "sec_type": "BAG",
            "status": "Submitted",
            "strategy": "vertical_spread",
            "combo_legs": [{}, {}],
        }
    )
    assert not order_is_working_riskless_combo(
        {**_working_iron(), "status": "Cancelled"}
    )


def test_block_reason_empty_book_allows_first():
    assert riskless_combo_block_reason("iron_condor", []) is None
    assert riskless_combo_block_reason("iron_condor", [], cancel_202=True) is None


def test_block_reason_second_while_working():
    reason = riskless_combo_block_reason("butterfly", [_working_iron()])
    assert reason
    assert REASON_CODE in reason
    assert "6384" in reason


def test_block_reason_after_202_until_working_bag_gone():
    unknown_bag = {
        "order_id": 7211,
        "symbol": "TLT",
        "sec_type": "BAG",
        "status": "Submitted",
    }
    reason = riskless_combo_block_reason(
        "iron_condor", [unknown_bag], cancel_202=True
    )
    assert reason
    assert "[202]" in reason
    assert riskless_combo_block_reason("iron_condor", [], cancel_202=True) is None
    assert riskless_combo_block_reason(
        "vertical_spread", [_working_iron()], cancel_202=True
    ) is None


def test_is_riskless_combo_202():
    assert is_riskless_combo_202(202, _RISKLESS_202)
    assert is_riskless_combo_202(202, "guaranteed-loss combination orders")
    assert not is_riskless_combo_202(202, "Order Canceled - reason:Requested by customer")
    assert not is_riskless_combo_202(201, _RISKLESS_202)


@pytest.mark.asyncio
async def test_empty_book_allows_first_iron():
    gw = FakeGW([])
    result = await execute_proposal(_iron(), gw)
    assert result.get("success") is True
    assert gw.calls == [("place_iron_condor", gw.calls[0][1])]


@pytest.mark.asyncio
async def test_second_iron_blocked_while_one_working():
    gw = FakeGW([_working_iron(status="Submitted")])
    result = await execute_proposal(_iron("TLT"), gw)
    assert result.get("success") is not True
    assert result.get("reason_code") == REASON_CODE
    assert REASON_CODE in str(result.get("error") or "")
    assert gw.calls == []


@pytest.mark.asyncio
async def test_second_fly_blocked_while_iron_pending_submit():
    gw = FakeGW([_working_iron(oid=6834, status="PendingSubmit")])
    result = await execute_proposal(_fly(), gw)
    assert result.get("reason_code") == REASON_CODE
    assert gw.calls == []
    result = await execute_proposal(_iron_fly(), gw)
    assert result.get("reason_code") == REASON_CODE
    assert gw.calls == []


@pytest.mark.asyncio
async def test_after_202_blocked_until_working_bag_gone():
    gate = reset_risk_gate()
    gate.note_riskless_combo_202()
    gw = FakeGW([_working_iron(oid=7028)])
    blocked = await execute_proposal(_iron("TLT"), gw)
    assert blocked.get("reason_code") == REASON_CODE
    assert gw.calls == []
    gw.open_orders = []
    allowed = await execute_proposal(_iron("TLT"), gw)
    assert allowed.get("success") is True
    assert gw.calls[0][0] == "place_iron_condor"
    assert gate.riskless_combo_202 is False


@pytest.mark.asyncio
async def test_working_vertical_does_not_block_first_iron():
    gw = FakeGW(
        [
            {
                "order_id": 11,
                "symbol": "SPY",
                "sec_type": "BAG",
                "status": "Submitted",
                "strategy": "vertical_spread",
                "combo_legs": [{}, {}],
            }
        ]
    )
    result = await execute_proposal(_iron(), gw)
    assert result.get("success") is True
    assert gw.calls[0][0] == "place_iron_condor"


@pytest.mark.asyncio
async def test_vertical_and_calendar_still_send():
    gw = FakeGW([_working_iron()])
    vert = await execute_proposal(_vertical(), gw)
    assert vert.get("success") is True
    assert gw.calls[-1][0] == "place_vertical_spread"
    cal = await execute_proposal(_calendar(), gw)
    assert cal.get("success") is True
    assert gw.calls[-1][0] == "place_calendar_spread"


@pytest.mark.asyncio
async def test_filled_debit_today_still_sends_vertical():
    """A filled defined-risk debit is a position, not a working riskless BAG."""
    gw = FakeGW([])
    gw.positions = [
        {
            "symbol": "IWM",
            "sec_type": "OPT",
            "quantity": 1,
            "strike": 220.0,
            "right": "C",
            "expiration": "20260828",
        },
        {
            "symbol": "IWM",
            "sec_type": "OPT",
            "quantity": -1,
            "strike": 222.0,
            "right": "C",
            "expiration": "20260828",
        },
    ]
    result = await execute_proposal(_vertical(symbol="SPY"), gw)
    assert result.get("success") is True
    assert gw.calls[0][0] == "place_vertical_spread"


@pytest.mark.asyncio
async def test_closing_iron_also_blocked_while_one_working():
    gw = FakeGW([_working_iron()])
    result = await execute_proposal(
        _iron(closing_position=True, limit_price=1.10), gw
    )
    assert result.get("reason_code") == REASON_CODE
    assert gw.calls == []


@pytest.mark.asyncio
async def test_send_action_does_not_place_second():
    gw = FakeGW([_working_iron(oid=7395)])
    ticket = {
        "strategy": "iron_condor",
        "params": dict(VALID_PAYLOADS["iron_condor"]),
        "rationale": "paper resend",
    }
    result = await send_action(ticket, gw)
    assert result.get("reason_code") == REASON_CODE
    assert result.get("status") == "rejected"
    assert gw.calls == []


@pytest.mark.asyncio
async def test_place_iron_condor_refuses_before_bag():
    placed = []

    class Mix(IBKROptionsMixin):
        async def get_open_orders(self):
            return [_working_iron()]

        async def _place_combo_order(self, *args, **kwargs):
            placed.append((args, kwargs))
            return {"success": True, "order_id": 1}

    mix = Mix()
    out = await mix.place_iron_condor(
        "IWM", "20260828", 200.0, 205.0, 215.0, 220.0, 1, 1.25, False
    )
    assert out.get("reason_code") == REASON_CODE
    assert placed == []


@pytest.mark.asyncio
async def test_place_iron_condor_empty_book_still_places():
    placed = []

    class Mix(IBKROptionsMixin):
        async def get_open_orders(self):
            return []

        async def _place_combo_order(self, *args, **kwargs):
            placed.append(True)
            return {"success": True, "order_id": 1}

    mix = Mix()
    out = await mix.place_iron_condor(
        "IWM", "20260828", 200.0, 205.0, 215.0, 220.0, 1, 1.25, False
    )
    assert out.get("success") is True
    assert placed == [True]


def test_on_error_riskless_202_sets_latch():
    reset_risk_gate()
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._riskless_combo_202 = False
    conn._ibkr_data_stale = False
    conn._connect_block = ""
    conn._on_error(6384, 202, _RISKLESS_202, "")
    assert conn._riskless_combo_202 is True
    assert get_risk_gate().riskless_combo_202 is True


def test_on_error_plain_202_does_not_latch():
    reset_risk_gate()
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._riskless_combo_202 = False
    conn._ibkr_data_stale = False
    conn._connect_block = ""
    conn.get_cancel_attribution = lambda *_a, **_k: {"kind": "broker_cancel"}
    conn._on_error(1, 202, "Order Canceled - reason:Requested by customer", "")
    assert conn._riskless_combo_202 is False
    assert get_risk_gate().riskless_combo_202 is False


def test_combo_legs_from_contract():
    contract = SimpleNamespace(
        comboLegs=[
            SimpleNamespace(conId=11, ratio=1, action="BUY", exchange="SMART"),
            SimpleNamespace(conId=12, ratio=2, action="SELL", exchange="SMART"),
            SimpleNamespace(conId=13, ratio=1, action="BUY", exchange="SMART"),
        ]
    )
    legs = combo_legs_from_contract(contract)
    assert [row["ratio"] for row in legs] == [1, 2, 1]
    assert legs[0]["conId"] == 11
