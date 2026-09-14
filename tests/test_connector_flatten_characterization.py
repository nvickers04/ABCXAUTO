"""Characterization: flatten_all / _wait_for_fill / async_lock.

Uses the test_resilience harness style (IBKRConnector singleton + fake ib,
no TWS socket). Pins current routing: empty book is a no-op, STK goes through
``_place_order`` MKT SELL, OPT goes through the orders-mixin close path.
``flatten_all`` is not a Grok tool.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from abcxauto.broker.connector import IBKRConnector
from abcxauto.broker.orders import IBKROrdersMixin


def _tool_names():
    from abcxauto.brain import AGENT_TOOLS, agent_tools

    catalog = []
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        catalog.append(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    live = []
    for t in agent_tools():
        fn = getattr(t, "function", None)
        live.append(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    return catalog, live


@pytest.fixture
def conn(monkeypatch):
    """Resilience-style connector: real class, fake ib, no heartbeat, no TWS."""
    IBKRConnector._instance = None
    monkeypatch.setattr(IBKRConnector, "_start_heartbeat", lambda self: None)
    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", AsyncMock())
    connector = IBKRConnector()
    connector.ib = SimpleNamespace(
        isConnected=lambda: True,
        positions=lambda: [],
        portfolio=lambda: [],
        openTrades=lambda: [],
        placeOrder=lambda *_a, **_k: None,
    )
    connector._connected = True
    try:
        yield connector
    finally:
        IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_flatten_all_empty_book_is_noop(conn, monkeypatch):
    placed: list = []
    closed: list = []
    cancelled: list = []

    async def _orders():
        return []

    async def _positions():
        return []

    async def _cancel(oid):
        cancelled.append(oid)
        return {"success": True, "order_id": oid}

    async def _place(**kwargs):
        placed.append(kwargs)
        return {"success": True, "order_id": 1}

    async def _close(*_a, **kwargs):
        closed.append(kwargs)
        return {"success": True, "order_id": 2}

    monkeypatch.setattr(conn, "get_open_orders", _orders)
    monkeypatch.setattr(conn, "get_positions", _positions)
    monkeypatch.setattr(conn, "cancel_order", _cancel)
    monkeypatch.setattr(conn, "_place_order", _place)
    monkeypatch.setattr(conn, "close_option_position", _close)

    result = await conn.flatten_all()
    assert result["success"] is True
    assert result["orders_total"] == 0
    assert result["orders_cancelled"] == 0
    assert result["positions_total"] == 0
    assert result["positions_closed"] == 0
    assert result["position_results"] == []
    assert result["errors"] == []
    assert placed == []
    assert closed == []
    assert cancelled == []


@pytest.mark.asyncio
async def test_flatten_all_stk_long_places_market_sell_full_qty(conn, monkeypatch):
    placed: list = []

    async def _orders():
        return []

    async def _positions():
        return [
            {
                "symbol": "AAPL",
                "quantity": 15,
                "sec_type": "STK",
                "conId": 265598,
            }
        ]

    async def _place(**kwargs):
        placed.append(kwargs)
        return {"success": True, "order_id": 55}

    monkeypatch.setattr(conn, "get_open_orders", _orders)
    monkeypatch.setattr(conn, "get_positions", _positions)
    monkeypatch.setattr(conn, "_place_order", _place)

    result = await conn.flatten_all()
    assert result["success"] is True
    assert result["positions_total"] == 1
    assert result["positions_closed"] == 1
    assert placed == [
        {
            "symbol": "AAPL",
            "action": "SELL",
            "quantity": 15,
            "order_type": "MKT",
            "tif": "IOC",
            "order_name": "EMERGENCY_FLATTEN_STK",
        }
    ]
    assert result["position_results"][0]["method"] == "stock_mkt"
    assert result["position_results"][0]["success"] is True


@pytest.mark.asyncio
async def test_flatten_all_option_uses_orders_mixin_close_path(conn, monkeypatch):
    """OPT legs call ``close_option_position`` (orders mixin on the class)."""
    assert conn.close_option_position.__func__ is IBKROrdersMixin.close_option_position
    closed: list = []
    placed: list = []

    async def _orders():
        return []

    async def _positions():
        return [
            {
                "symbol": "SPY",
                "quantity": -2,
                "sec_type": "OPT",
                "expiration": "20260918",
                "strike": 500.0,
                "right": "P",
                "conId": 99,
            }
        ]

    async def _place(**kwargs):
        placed.append(kwargs)
        return {"success": True, "order_id": 1}

    async def _close(symbol, **kwargs):
        closed.append((symbol, kwargs))
        return {"success": True, "order_id": 88}

    monkeypatch.setattr(conn, "get_open_orders", _orders)
    monkeypatch.setattr(conn, "get_positions", _positions)
    monkeypatch.setattr(conn, "_place_order", _place)
    monkeypatch.setattr(conn, "close_option_position", _close)

    result = await conn.flatten_all()
    assert placed == []
    assert closed == [
        (
            "SPY",
            {
                "expiration": "20260918",
                "strike": 500.0,
                "right": "P",
                "quantity": 2,
                "reason": "panic_flatten",
            },
        )
    ]
    assert result["position_results"][0]["method"] == "close_option_position"
    assert result["positions_closed"] == 1


def test_flatten_all_is_not_an_agent_tool():
    catalog, live = _tool_names()
    assert "flatten_all" not in catalog
    assert "flatten_all" not in live
    from abcxauto.brain import agent_tools

    for session in ("", "regular", "premarket", "closed"):
        names = []
        for t in agent_tools(session=session):
            fn = getattr(t, "function", None)
            names.append(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
        assert "flatten_all" not in names


@pytest.mark.asyncio
async def test_wait_for_fill_filled_and_timeout(conn, monkeypatch):
    class _Status:
        def __init__(self, status, avg=None, filled=0):
            self.status = status
            self.avgFillPrice = avg
            self.filled = filled

    class _Trade:
        def __init__(self, status, **kw):
            self.orderStatus = _Status(status, **kw)

    filled = await conn._wait_for_fill(
        _Trade("Filled", avg=12.5, filled=3), timeout=5.0
    )
    assert filled == {
        "filled": True,
        "status": "Filled",
        "avg_fill_price": 12.5,
        "filled_quantity": 3,
    }

    cancelled = await conn._wait_for_fill(_Trade("Cancelled"), timeout=5.0)
    assert cancelled["filled"] is False
    assert cancelled["status"] == "Cancelled"

    timed = await conn._wait_for_fill(_Trade("Submitted"), timeout=0)
    assert timed == {
        "filled": False,
        "status": "Timeout",
        "avg_fill_price": None,
        "filled_quantity": 0,
    }


@pytest.mark.asyncio
async def test_async_lock_rebinds_when_bound_loop_is_closed():
    """A lock created on a loop that is now closed is replaced for this loop."""
    IBKRConnector._instance = None
    connector = IBKRConnector()
    try:
        done = threading.Event()

        def _bind_and_close():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _bind():
                async with connector.async_lock:
                    pass

            loop.run_until_complete(_bind())
            loop.close()
            done.set()

        t = threading.Thread(target=_bind_and_close)
        t.start()
        assert done.wait(timeout=2)
        t.join(timeout=2)
        stale = connector._async_lock
        assert stale is not None
        current = connector.async_lock
        assert current is not stale
        async with current:
            pass
    finally:
        IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_async_lock_fail_closed_when_other_live_loop_owns_it():
    """Another still-live loop owns the lock → RuntimeError, no second lock."""
    IBKRConnector._instance = None
    connector = IBKRConnector()
    held = threading.Event()
    release = threading.Event()
    try:

        def _hold():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _run():
                async with connector.async_lock:
                    held.set()
                    while not release.is_set():
                        await asyncio.sleep(0.01)

            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()

        t = threading.Thread(target=_hold)
        t.start()
        assert held.wait(timeout=2)
        with pytest.raises(RuntimeError, match="different event loop"):
            _ = connector.async_lock
        release.set()
        t.join(timeout=2)
        assert not t.is_alive()
    finally:
        IBKRConnector._instance = None
