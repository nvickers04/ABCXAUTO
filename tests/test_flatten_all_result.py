"""flatten_all tells the truth: success only when the book is actually flat."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from abcxauto.broker.connector import IBKRConnector


def _lot(symbol, qty, *, con_id, price=100.0, sec="STK"):
    return {
        "symbol": symbol,
        "quantity": qty,
        "conId": con_id,
        "con_id": con_id,
        "sec_type": sec,
        "market_price": price,
        "avg_cost": price,
    }


def _stop(symbol, oid, *, con_id, aux=99.0):
    return {
        "order_id": oid,
        "symbol": symbol,
        "conId": con_id,
        "con_id": con_id,
        "sec_type": "STK",
        "order_type": "STP",
        "action": "SELL",
        "quantity": 10,
        "aux_price": aux,
        "status": "Submitted",
    }


@pytest.fixture
def conn(monkeypatch):
    bag = IBKRConnector.__new__(IBKRConnector)
    bag.connected = True

    async def _ok():
        return True

    bag._ensure_connected = _ok
    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", AsyncMock())
    return bag


def _wire(bag, *, book, orders, flatten_one, cancel=None, place_stop=None):
    async def _positions():
        return list(book)

    async def _orders():
        return list(orders)

    async def _cancel(oid):
        if cancel is not None:
            return await cancel(oid)
        orders[:] = [row for row in orders if row.get("order_id") != oid]
        return {"success": True, "order_id": oid}

    bag.get_positions = _positions
    bag.get_open_orders = _orders
    bag.cancel_order = _cancel
    bag._flatten_one_position = flatten_one
    bag.place_stop_order = place_stop or AsyncMock(
        return_value={"success": True, "order_id": 900}
    )


@pytest.mark.asyncio
async def test_flatten_all_full_success(conn):
    book = [_lot("SPY", 10, con_id=1), _lot("QQQ", 5, con_id=2)]
    orders = [_stop("SPY", 11, con_id=1)]

    async def _close(pos):
        book[:] = [row for row in book if row["conId"] != pos["conId"]]
        return {
            "success": True,
            "method": "stock_mkt",
            "symbol": pos["symbol"],
            "conId": pos["conId"],
        }

    _wire(conn, book=book, orders=orders, flatten_one=_close)
    out = await conn.flatten_all()
    assert out["success"] is True
    assert out["status"] == "flat"
    assert out["positions_closed"] == 2
    assert out["positions_total"] == 2
    assert out["remaining"] == []
    assert out["failed"] == []
    assert out["orders_cancelled"] == 1
    assert {row["status"] for row in out["position_results"]} == {"closed"}


@pytest.mark.asyncio
async def test_flatten_all_partial_reports_remaining(conn):
    book = [_lot("SPY", 10, con_id=1), _lot("QQQ", 5, con_id=2)]
    orders = []

    async def _close(pos):
        if pos["symbol"] == "QQQ":
            book[:] = [row for row in book if row["symbol"] != "QQQ"]
            return {"success": True, "method": "stock_mkt", "symbol": "QQQ", "conId": 2}
        return {
            "success": False,
            "method": "stock_mkt",
            "symbol": "SPY",
            "conId": 1,
            "error": "close rejected",
        }

    placed = []

    async def _stop_order(symbol, action, qty, px):
        placed.append((symbol, action, qty, px))
        return {"success": True, "order_id": 77}

    _wire(conn, book=book, orders=orders, flatten_one=_close, place_stop=_stop_order)
    out = await conn.flatten_all()
    assert out["success"] is False
    assert out["status"] == "partial"
    assert out["positions_closed"] == 1
    assert out["positions_total"] == 2
    assert len(out["remaining"]) == 1
    assert out["remaining"][0]["symbol"] == "SPY"
    assert len(out["failed"]) == 1
    assert out["failed"][0]["symbol"] == "SPY"
    assert "close rejected" in str(out["failed"][0]["reason"])
    assert out["failed"][0]["protection"] == "last_stop"
    assert placed == [("SPY", "SELL", 10, 100.0)]


@pytest.mark.asyncio
async def test_flatten_all_close_rejected_leaves_last_stop(conn):
    book = [_lot("SPY", 10, con_id=1, price=104.0)]
    orders = [_stop("SPY", 11, con_id=1, aux=97.5)]

    async def _close(pos):
        return {
            "success": False,
            "method": "stock_mkt",
            "symbol": "SPY",
            "conId": 1,
            "error": "201 Overfill",
        }

    placed = []

    async def _stop_order(symbol, action, qty, px):
        placed.append((symbol, action, qty, px))
        return {"success": True, "order_id": 88}

    _wire(conn, book=book, orders=orders, flatten_one=_close, place_stop=_stop_order)
    out = await conn.flatten_all()
    assert out["success"] is False
    assert out["status"] == "failed"
    assert out["failed"][0]["protection"] == "last_stop"
    assert out["failed"][0]["protection_order_id"] == 88
    # Restore the cancelled stop price, not a new hunt level.
    assert placed == [("SPY", "SELL", 10, 97.5)]
    assert out["orders_cancelled"] == 1


@pytest.mark.asyncio
async def test_flatten_all_cancel_rejected_still_closes(conn):
    book = [_lot("SPY", 10, con_id=1)]
    orders = [_stop("SPY", 11, con_id=1)]

    async def _cancel(_oid):
        return {"error": "Order rejected: cannot cancel"}

    async def _close(pos):
        book[:] = []
        return {
            "success": True,
            "method": "stock_mkt",
            "symbol": pos["symbol"],
            "conId": pos["conId"],
        }

    stop = conn.place_stop_order = AsyncMock()
    _wire(conn, book=book, orders=orders, flatten_one=_close, cancel=_cancel)
    conn.place_stop_order = stop
    out = await conn.flatten_all()
    assert out["success"] is True
    assert out["status"] == "flat"
    assert out["orders_cancelled"] == 0
    assert out["orders_failed"][0]["order_id"] == 11
    assert "cannot cancel" in out["orders_failed"][0]["reason"]
    assert out["remaining"] == []
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_flatten_all_cancel_rejected_keeps_working_stop(conn):
    book = [_lot("SPY", 10, con_id=1)]
    orders = [_stop("SPY", 11, con_id=1, aux=96.0)]

    async def _cancel(_oid):
        return {"error": "Order rejected: cannot cancel"}

    async def _close(_pos):
        return {"success": False, "symbol": "SPY", "conId": 1, "error": "close rejected"}

    placed = AsyncMock()
    _wire(
        conn,
        book=book,
        orders=orders,
        flatten_one=_close,
        cancel=_cancel,
        place_stop=placed,
    )
    out = await conn.flatten_all()
    assert out["success"] is False
    assert out["failed"][0]["protection"] == "still_working"
    assert out["failed"][0]["protection_order_id"] == 11
    placed.assert_not_called()


@pytest.mark.asyncio
async def test_flatten_all_not_connected(conn):
    async def _no():
        return False

    conn._ensure_connected = _no
    out = await conn.flatten_all()
    assert out["success"] is False
    assert out["status"] == "disconnected"
    assert out["error"] == "Not connected"


def test_stk_flatten_uses_gtc_not_ioc():
    """Panic close must not expire unfilled; leftover last-stop is the fallback."""
    import inspect

    src = inspect.getsource(IBKRConnector._flatten_one_position)
    assert 'tif="GTC"' in src
    assert 'tif="IOC"' not in src
