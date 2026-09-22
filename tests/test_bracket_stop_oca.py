"""Bracket fill-adjust: OCA modify error must not claim a dead stop as protected."""

from __future__ import annotations

from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from abcxauto.broker.orders import IBKROrdersMixin


@pytest.mark.asyncio
async def test_bracket_oca_modify_error_replaces_with_bare_stop(monkeypatch):
    """modify_stop_price error + cancelled stop → bare place_stop_order, not protected unless working."""
    mixin = IBKROrdersMixin()
    mixin.ib = MagicMock()
    mixin.net_liquidation = 100_000.0
    mixin.day_trades_remaining = -1
    mixin._order_state_lock = Lock()
    mixin._bracket_groups = {}
    mixin._ibkr_data_stale = False
    placed = []
    nid = {"n": 10}
    stop_trade_holder = {"t": None}

    class _T:
        def __init__(self, order):
            nid["n"] += 1
            self.order = order
            self.order.orderId = nid["n"]
            self.orderStatus = SimpleNamespace(status="Submitted")
            self.log = []
            self.contract = SimpleNamespace(symbol="SPY")

    def _place(_c, order):
        trade = _T(order)
        placed.append(order)
        if getattr(order, "orderType", None) == "STP" and getattr(order, "parentId", None):
            stop_trade_holder["t"] = trade
        return trade

    mixin.ib.placeOrder = _place
    mixin.ib.openTrades = MagicMock(return_value=[])

    async def _ok():
        return True

    mixin._ensure_connected = _ok
    mixin._prepare_contract = AsyncMock(return_value=SimpleNamespace(symbol="SPY"))
    mixin._contract_min_tick = AsyncMock(return_value=0.01)
    mixin._update_account_values = AsyncMock()

    async def _modify_cancels(order_id, new_stop_price):
        t = stop_trade_holder["t"]
        if t is not None:
            t.orderStatus.status = "Cancelled"
        return {"error": "Error 10326: Cannot modify order with ocaGroup set"}

    mixin.modify_stop_price = AsyncMock(side_effect=_modify_cancels)

    # Replacement does not rest → must not report protected.
    place_stop = AsyncMock(return_value={"error": "replace failed"})
    mixin.place_stop_order = place_stop

    async def _fill(trade, timeout=30.0):
        return {
            "filled": True,
            "status": "Filled",
            "avg_fill_price": 101.0,  # != entry so fill-adjust runs
            "filled_quantity": 10,
        }

    mixin._wait_for_fill = _fill
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())

    out = await mixin.place_bracket_order("SPY", 10, "LONG", 100.0, 98.0, 103.0)

    place_stop.assert_called_once()
    args, kwargs = place_stop.call_args
    # bare place_stop_order(symbol, action, qty, stop_price) — no ocaGroup/parentId
    assert args[0] == "SPY"
    assert args[1] == "SELL"
    assert args[2] == 10
    assert abs(float(args[3]) - 98.0) < 1e-9  # planned_stop (adjust failed)
    assert not kwargs

    assert out.get("filled") is True
    assert out.get("protection") != "protected"
    assert out.get("protection") == "unprotected"
    assert out.get("success") is False
    # Must not market-sell the shares on this path.
    assert not any(getattr(o, "orderType", None) == "MKT" for o in placed)

    # When replacement is working, protection may be protected.
    stop_trade_holder["t"] = None
    placed.clear()
    nid["n"] = 10

    async def _modify_cancels_again(order_id, new_stop_price):
        t = stop_trade_holder["t"]
        if t is not None:
            t.orderStatus.status = "Cancelled"
        return {"error": "Error 10326: Cannot modify order with ocaGroup set"}

    mixin.modify_stop_price = AsyncMock(side_effect=_modify_cancels_again)

    replacement = SimpleNamespace(
        order=SimpleNamespace(orderId=999, orderType="STP", parentId=None, ocaGroup=""),
        orderStatus=SimpleNamespace(status="Submitted"),
        log=[],
        contract=SimpleNamespace(symbol="SPY"),
    )

    async def _place_stop_ok(symbol, action, quantity, stop_price):
        mixin.ib.openTrades = MagicMock(return_value=[replacement])
        return {"success": True, "order_id": 999}

    mixin.place_stop_order = AsyncMock(side_effect=_place_stop_ok)

    out2 = await mixin.place_bracket_order("SPY", 10, "LONG", 100.0, 98.0, 103.0)
    mixin.place_stop_order.assert_called_once()
    assert out2.get("protection") == "protected"
    assert out2.get("success") is True
    assert out2.get("stop_order_id") == 999
    assert not any(getattr(o, "orderType", None) == "MKT" for o in placed)
