"""get_open_orders scrubs IBKR unset doubles on the raw order dict."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from abcxauto.broker.connector import IBKRConnector


def _open_trade(
    *,
    order_type: str = "LMT",
    lmt: float = 5.24,
    trail_pct: float | None = None,
    aux: float = 0.0,
    status: str = "Submitted",
):
    order = SimpleNamespace(
        orderId=20034,
        action="SELL",
        totalQuantity=6,
        orderType=order_type,
        lmtPrice=lmt,
        auxPrice=aux,
        trailingPercent=trail_pct,
        ocaGroup="",
        parentId=0,
    )
    contract = SimpleNamespace(
        symbol="SPY",
        secType="BAG",
        conId=123,
        comboLegs=[{}, {}],
    )
    return SimpleNamespace(
        order=order,
        orderStatus=SimpleNamespace(status=status),
        contract=contract,
    )


@pytest.fixture
def conn(monkeypatch):
    IBKRConnector._instance = None
    monkeypatch.setattr(IBKRConnector, "_start_heartbeat", lambda self: None)
    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", AsyncMock())
    connector = IBKRConnector()
    connector.ib = SimpleNamespace(
        isConnected=lambda: True,
        openTrades=lambda: [],
        reqAllOpenOrdersAsync=AsyncMock(),
    )
    connector._connected = True
    try:
        yield connector
    finally:
        IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_get_open_orders_scrubs_unset_trailing_percent(conn):
    conn.ib.openTrades = lambda: [_open_trade(trail_pct=sys.float_info.max)]
    orders = await conn.get_open_orders()
    assert len(orders) == 1
    assert "trail_percent" not in orders[0]
    assert orders[0]["lmt_price"] == 5.24


@pytest.mark.asyncio
async def test_get_open_orders_keeps_real_trailing_percent(conn):
    conn.ib.openTrades = lambda: [_open_trade(order_type="TRAIL", trail_pct=2.5, lmt=0.0)]
    orders = await conn.get_open_orders()
    assert orders[0]["trail_percent"] == 2.5


@pytest.mark.asyncio
async def test_get_open_orders_scrubs_unset_aux_price(conn):
    conn.ib.openTrades = lambda: [
        _open_trade(order_type="STP", lmt=0.0, aux=sys.float_info.max)
    ]
    orders = await conn.get_open_orders()
    assert orders[0]["aux_price"] is None
