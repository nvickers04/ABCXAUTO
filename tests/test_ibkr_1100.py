import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from abcxauto.broker.connection import classify_error_code
from abcxauto.broker.connector import IBKRConnector


def test_apply_req_pnl_overwrites_daily_tag():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._pnl = SimpleNamespace(dailyPnL=-41.25)
    out = conn._apply_req_pnl({"dailypnl": 0.0, "unrealizedpnl": -800.0})
    assert out["dailypnl"] == -41.25
    assert out["unrealizedpnl"] == -800.0
    conn._pnl = SimpleNamespace(dailyPnL=float("nan"))
    out = conn._apply_req_pnl({"dailypnl": -1.0})
    assert out["dailypnl"] == -1.0
    assert classify_error_code(1100) == "tws_lost"
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = False
    conn._connected = True
    conn._reconnect_requested = False
    conn._disconnect_cause = "unknown"
    scheduled = []
    conn._schedule_reconnect = lambda reason: scheduled.append(reason)
    conn._on_error(-1, 1100, "Connectivity between IBKR and TWS has been lost.", "")
    assert conn._ibkr_data_stale is True
    assert conn._connected is True
    assert scheduled == []


@pytest.mark.asyncio
async def test_heartbeat_keeps_live_socket_after_1100(monkeypatch):
    """Ping/flag stale must not schedule reconnect or mint a new IB()."""
    IBKRConnector._instance = None
    conn = IBKRConnector()
    conn._connected = False
    conn._ibkr_data_stale = True
    conn._disconnect_cause = "unknown"
    conn._heartbeat_failures = 0
    ib = MagicMock()
    ib.isConnected = lambda: True

    async def _ping():
        raise TimeoutError("farm down")

    ib.reqCurrentTimeAsync = _ping
    conn.ib = ib
    scheduled = []
    conn._schedule_reconnect = lambda reason: scheduled.append(reason)
    ticks = {"n": 0}

    async def _sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", _sleep)
    await conn._heartbeat_loop()
    assert scheduled == []
    assert conn._connected is True
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_heartbeat_ping_fail_keeps_live_socket(monkeypatch):
    IBKRConnector._instance = None
    conn = IBKRConnector()
    conn._connected = True
    conn._ibkr_data_stale = True
    ib = MagicMock()
    ib.isConnected = lambda: True

    async def _ping():
        raise TimeoutError("reqCurrentTime failed")

    ib.reqCurrentTimeAsync = _ping
    conn.ib = ib
    scheduled = []
    conn._schedule_reconnect = lambda reason: scheduled.append(reason)
    ticks = {"n": 0}

    async def _sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", _sleep)
    await conn._heartbeat_loop()
    assert scheduled == []
    assert conn._connected is True
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_heartbeat_retries_book_refresh_while_stale(monkeypatch):
    """While stale with a live socket, heartbeat retries book refresh, not reconnect."""
    IBKRConnector._instance = None
    conn = IBKRConnector()
    conn._connected = True
    conn._ibkr_data_stale = True
    ib = MagicMock()
    ib.isConnected = lambda: True

    async def _ping():
        raise TimeoutError("reqCurrentTime failed")

    ib.reqCurrentTimeAsync = _ping
    conn.ib = ib
    scheduled = []
    refresh_calls = []
    conn._schedule_reconnect = lambda reason: scheduled.append(reason)
    conn._schedule_book_refresh_after_restore = lambda: refresh_calls.append(1)
    ticks = {"n": 0}

    async def _sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", _sleep)
    await conn._heartbeat_loop()
    assert len(refresh_calls) >= 1
    assert scheduled == []
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_heartbeat_disconnected_but_socket_live_retries_refresh(monkeypatch):
    """Adopt a live socket, schedule book refresh, and do not reconnect."""
    IBKRConnector._instance = None
    conn = IBKRConnector()
    conn._connected = False
    conn._ibkr_data_stale = True
    conn._disconnect_cause = "unknown"
    conn._heartbeat_failures = 0
    ib = MagicMock()
    ib.isConnected = lambda: True

    async def _ping():
        raise TimeoutError("farm down")

    ib.reqCurrentTimeAsync = _ping
    conn.ib = ib
    scheduled = []
    refresh_calls = []
    conn._schedule_reconnect = lambda reason: scheduled.append(reason)
    conn._schedule_book_refresh_after_restore = lambda: refresh_calls.append(1)
    ticks = {"n": 0}

    async def _sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", _sleep)
    await conn._heartbeat_loop()
    assert conn._connected is True
    assert len(refresh_calls) >= 1
    assert scheduled == []
    IBKRConnector._instance = None


def test_1102_marks_stale_before_refresh():
    """1102 keeps the book stale until a successful refresh, not on restore."""
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = False
    conn._connected = True
    refresh_calls = []
    conn._schedule_book_refresh_after_restore = lambda: refresh_calls.append(1)
    conn._on_error(-1, 1102, "Connectivity has been restored.", "")
    assert conn._ibkr_data_stale is True
    assert refresh_calls == [1]
    IBKRConnector._instance = None
