"""Broker resilience: mode/port guards, disconnect halt, bracket emergency flatten."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from abcxauto.broker.connection import (
    LIVE_CONFIRM_PHRASE,
    TradingModePortError,
    reconnect_backoff_seconds,
    validate_trading_mode_port,
)
from abcxauto.broker.orders import IBKROrdersMixin
from abcxauto.config import Config, get_config
from abcxauto.risk_gates import get_risk_gate, reset_risk_gate


# ---------------------------------------------------------------------------
# Task 2 — paper/live port + live-confirm guards
# ---------------------------------------------------------------------------


class TestTradingModePortGuard:
    def test_paper_rejects_live_port(self):
        with pytest.raises(TradingModePortError, match="paper"):
            validate_trading_mode_port("paper", 7496)

    def test_live_rejects_paper_port(self):
        with pytest.raises(TradingModePortError, match="live"):
            validate_trading_mode_port("live", 7497, LIVE_CONFIRM_PHRASE)

    def test_live_without_confirm_rejected(self):
        with pytest.raises(TradingModePortError, match="LIVE_CONFIRM"):
            validate_trading_mode_port("live", 7496, "")

    def test_live_wrong_confirm_rejected(self):
        with pytest.raises(TradingModePortError, match="LIVE_CONFIRM"):
            validate_trading_mode_port("live", 4001, "yes")

    def test_paper_ok_ports(self):
        validate_trading_mode_port("paper", 7497)
        validate_trading_mode_port("paper", 4002)

    def test_live_with_confirm_ok(self):
        validate_trading_mode_port("live", 7496, LIVE_CONFIRM_PHRASE)
        validate_trading_mode_port("live", 4001, LIVE_CONFIRM_PHRASE)


@pytest.mark.asyncio
async def test_connect_refuses_paper_mode_live_port(monkeypatch):
    """Guard runs before any IB.connect attempt."""
    from abcxauto.broker import connector as connector_mod
    from abcxauto.broker.connector import IBKRConnector

    base = get_config()
    monkeypatch.setattr(
        "abcxauto.broker.connection.get_config",
        lambda: Config(**{**base.__dict__, "trading_mode": "paper", "ibkr_port": 7496}),
    )
    monkeypatch.setattr(
        "abcxauto.broker.connector.get_config",
        lambda: Config(**{**base.__dict__, "trading_mode": "paper", "ibkr_port": 7496}),
    )

    # Reset singleton so __init__ re-resolves endpoint
    IBKRConnector._instance = None
    conn = IBKRConnector()
    connect_calls = []

    async def _fake_connect_async(*a, **k):
        connect_calls.append((a, k))
        raise AssertionError("IB.connectAsync must not be called")

    conn.ib = MagicMock()
    conn.ib.connectAsync = _fake_connect_async
    conn.ib.isConnected = lambda: False

    with pytest.raises(TradingModePortError):
        await conn.connect(max_retries=1)
    assert connect_calls == []

    IBKRConnector._instance = None  # leave clean for other tests


def test_port_is_closed_detects_refused():
    from abcxauto.broker.connector import port_is_closed

    assert port_is_closed(ConnectionRefusedError(10061, "refused")) is True
    assert port_is_closed(OSError(10061, "Connect call failed")) is True
    assert port_is_closed(OSError(1225, "The remote computer refused the network connection")) is True
    assert port_is_closed(TimeoutError("timed out")) is False


@pytest.mark.asyncio
async def test_connect_stops_on_port_closed(monkeypatch):
    from abcxauto.broker import connector as connector_mod
    from abcxauto.broker.connector import IBKRConnector

    calls: list[int] = []

    class FakeIB:
        def __init__(self):
            pass

        async def connectAsync(self, *_a, **_k):
            calls.append(1)
            raise ConnectionRefusedError(10061, "Connect call failed")

        def isConnected(self):
            return False

        def __getattr__(self, name):
            return MagicMock()

    monkeypatch.setattr(connector_mod, "IB", FakeIB)
    IBKRConnector._instance = None
    conn = IBKRConnector()
    ok = await conn.connect(max_retries=12)
    assert ok is False
    assert calls == [1]
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_connect_stops_on_paper_disclaimer(monkeypatch):
    from abcxauto.broker import connector as connector_mod
    from abcxauto.broker.connector import IBKRConnector

    calls: list[int] = []

    class FakeIB:
        def __init__(self):
            pass

        async def connectAsync(self, *_a, **_k):
            calls.append(1)
            IBKRConnector._instance._connect_block = "paper_disclaimer"
            raise TimeoutError()

        def isConnected(self):
            return False

        def __getattr__(self, name):
            return MagicMock()

    monkeypatch.setattr(connector_mod, "IB", FakeIB)
    IBKRConnector._instance = None
    conn = IBKRConnector()
    ok = await conn.connect(max_retries=12)
    assert ok is False
    assert calls == [1]
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_connect_refuses_live_without_confirm(monkeypatch):
    from abcxauto.broker.connector import IBKRConnector

    base = get_config()
    cfg = Config(
        **{
            **base.__dict__,
            "trading_mode": "live",
            "ibkr_port": 7496,
            "live_confirm": "",
        }
    )
    monkeypatch.setattr("abcxauto.broker.connection.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.broker.connector.get_config", lambda: cfg)

    IBKRConnector._instance = None
    conn = IBKRConnector()
    connect_calls = []

    async def _fake_connect_async(*a, **k):
        connect_calls.append(1)
        raise AssertionError("must not connect")

    conn.ib.connectAsync = _fake_connect_async
    with pytest.raises(TradingModePortError, match="LIVE_CONFIRM"):
        await conn.connect(max_retries=1)
    assert connect_calls == []
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_connect_live_with_confirm_passes_guard(monkeypatch):
    """Live + confirm + correct port reaches IB.connectAsync (mocked)."""
    from abcxauto.broker.connector import IBKRConnector

    base = get_config()
    cfg = Config(
        **{
            **base.__dict__,
            "trading_mode": "live",
            "ibkr_port": 7496,
            "live_confirm": LIVE_CONFIRM_PHRASE,
        }
    )
    monkeypatch.setattr("abcxauto.broker.connection.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.broker.connector.get_config", lambda: cfg)

    IBKRConnector._instance = None
    conn = IBKRConnector()
    connect_calls = []

    class _FakeIB:
        def __init__(self):
            self.disconnectedEvent = _FakeEvent()
            self.execDetailsEvent = _FakeEvent()
            self.orderStatusEvent = _FakeEvent()
            self.errorEvent = _FakeEvent()

        async def connectAsync(self, **kwargs):
            connect_calls.append(kwargs)

        def isConnected(self):
            return True

        def reqMarketDataType(self, *_a):
            pass

        def managedAccounts(self):
            return ["DU123"]

        def accountValues(self, *_a):
            return []

    class _FakeEvent:
        def __iadd__(self, other):
            return self

        def __isub__(self, other):
            return self

    monkeypatch.setattr("abcxauto.broker.connector.IB", _FakeIB)
    monkeypatch.setattr(
        "abcxauto.broker.connector._safe_sleep",
        AsyncMock(),
    )
    # Avoid starting a real heartbeat task that outlives the test
    monkeypatch.setattr(
        IBKRConnector,
        "_start_heartbeat",
        lambda self: None,
    )
    monkeypatch.setattr(
        IBKRConnector,
        "_update_account_values",
        AsyncMock(),
    )

    ok = await conn.connect(max_retries=1)
    assert ok is True
    assert len(connect_calls) == 1
    assert connect_calls[0]["port"] == 7496
    IBKRConnector._instance = None


# ---------------------------------------------------------------------------
# Task 3 — backoff math + disconnect → halt
# ---------------------------------------------------------------------------


class TestReconnectBackoff:
    def test_starts_near_two_seconds(self):
        assert reconnect_backoff_seconds(0, base=2.0, cap=60.0) == 2.0

    def test_doubles_then_caps(self):
        assert reconnect_backoff_seconds(1, base=2.0, cap=60.0) == 4.0
        assert reconnect_backoff_seconds(2, base=2.0, cap=60.0) == 8.0
        assert reconnect_backoff_seconds(3, base=2.0, cap=60.0) == 16.0
        assert reconnect_backoff_seconds(4, base=2.0, cap=60.0) == 32.0
        assert reconnect_backoff_seconds(5, base=2.0, cap=60.0) == 60.0
        assert reconnect_backoff_seconds(10, base=2.0, cap=60.0) == 60.0


class _SimpleEvent:
    """Minimal ib_insync-style event: += handler, .emit() fires all."""

    def __init__(self):
        self._handlers: List[Callable] = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def emit(self):
        for h in list(self._handlers):
            h()


class _FakeIBForDisconnect:
    def __init__(self):
        self.disconnectedEvent = _SimpleEvent()
        self.execDetailsEvent = _SimpleEvent()
        self.orderStatusEvent = _SimpleEvent()
        self.errorEvent = _SimpleEvent()
        self._connected = True

    def isConnected(self):
        return self._connected


@pytest.mark.asyncio
async def test_disconnect_halts_after_threshold(monkeypatch):
    """Simulate disconnectedEvent; short threshold; no real 120s sleep."""
    from abcxauto.broker.connector import IBKRConnector

    base = get_config()
    cfg = Config(
        **{
            **base.__dict__,
            "trading_mode": "paper",
            "ibkr_port": 7497,
            "disconnect_halt_s": 0.05,
        }
    )
    monkeypatch.setattr("abcxauto.broker.connection.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.broker.connector.get_config", lambda: cfg)

    reset_risk_gate()
    IBKRConnector._instance = None

    # Build connector without running full __init__ side effects twice
    conn = IBKRConnector()
    fake_ib = _FakeIBForDisconnect()
    conn.ib = fake_ib
    conn._handlers_on_ib = None
    conn._register_handlers()
    conn._connected = True
    conn._loop = asyncio.get_running_loop()
    conn._tickers = {}

    # Reconnect always fails quickly; sleep is near-instant
    async def _fail_connect(max_retries=None):
        return False

    sleeps: List[float] = []

    async def _fast_sleep(s):
        sleeps.append(s)
        await asyncio.sleep(0)  # yield only

    monkeypatch.setattr(conn, "connect", _fail_connect)
    monkeypatch.setattr("abcxauto.broker.connector._safe_sleep", _fast_sleep)

    # Pretend we have already been disconnected long enough
    conn._disconnect_since = time.monotonic() - 1.0
    conn._disconnect_halt_fired = False
    conn._disconnect_cause = "tws_restart"

    # Fire disconnect event (also schedules reconnect)
    fake_ib._connected = False
    # _on_disconnect would reset _disconnect_since if None — keep our past stamp
    # by calling schedule path directly after marking disconnected
    conn._connected = False
    conn._schedule_reconnect("tws_restart")

    # Drive the reconnect loop briefly
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if get_risk_gate().is_halted:
            break
        await asyncio.sleep(0.02)

    assert get_risk_gate().is_halted
    assert "broker disconnected" in get_risk_gate().halt_reason
    assert get_risk_gate().halt_kind == "disconnect"

    # Cancel background task
    if conn._reconnect_task and not conn._reconnect_task.done():
        conn._reconnect_task.cancel()
        try:
            await conn._reconnect_task
        except (asyncio.CancelledError, Exception):
            pass

    IBKRConnector._instance = None
    reset_risk_gate()


def test_connector_client_id_from_config(monkeypatch):
    """IBKRConnector must use get_config().ibkr_client_id (not env default 1)."""
    from abcxauto.broker.connector import IBKRConnector

    base = get_config()
    cfg = Config(**{**base.__dict__, "ibkr_client_id": 77, "ibkr_port": 7497, "trading_mode": "paper"})
    monkeypatch.setattr("abcxauto.broker.connector.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.broker.connection.get_config", lambda: cfg)

    IBKRConnector._instance = None
    conn = IBKRConnector()
    assert conn.client_id == 77
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_handlers_not_double_wired(monkeypatch):
    from abcxauto.broker.connector import IBKRConnector

    IBKRConnector._instance = None
    conn = IBKRConnector()
    fake = _FakeIBForDisconnect()
    conn.ib = fake
    conn._handlers_on_ib = None
    conn._register_handlers()
    assert len(fake.disconnectedEvent._handlers) == 1
    conn._register_handlers()  # idempotent
    assert len(fake.disconnectedEvent._handlers) == 1
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_resolve_loop_prefers_captured_running_loop():
    """Reconnect must stay on the connect loop, not a stray running loop."""
    from abcxauto.broker.connector import IBKRConnector

    IBKRConnector._instance = None
    conn = IBKRConnector()

    class _Other:
        def is_closed(self):
            return False

        def is_running(self):
            return True

    other = _Other()
    conn._loop = other
    assert conn._resolve_loop() is other
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_async_lock_rebinds_after_bound_loop_closes():
    """Closed-loop lock is the paper 'Lock bound to a different event loop' hole."""
    from abcxauto.broker.connector import IBKRConnector

    IBKRConnector._instance = None
    conn = IBKRConnector()
    done = threading.Event()

    def _bind_and_close():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _bind():
            async with conn.async_lock:
                pass

        loop.run_until_complete(_bind())
        loop.close()
        done.set()

    t = threading.Thread(target=_bind_and_close)
    t.start()
    assert done.wait(timeout=2)
    t.join(timeout=2)
    stale = conn._async_lock
    assert stale is not None
    current = conn.async_lock
    assert current is not stale
    async with current:
        pass
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_async_lock_fail_closed_when_held_on_other_live_loop():
    from abcxauto.broker.connector import IBKRConnector

    IBKRConnector._instance = None
    conn = IBKRConnector()
    held = threading.Event()
    release = threading.Event()

    def _hold():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            async with conn.async_lock:
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
        _ = conn.async_lock
    release.set()
    t.join(timeout=2)
    assert not t.is_alive()
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_connect_adopts_live_api_socket_no_new_ib(monkeypatch):
    """1100 keeps the socket; connect must not replace IB()."""
    from abcxauto.broker.connector import IBKRConnector

    IBKRConnector._instance = None
    conn = IBKRConnector()
    created = {"n": 0}

    def _boom():
        created["n"] += 1
        raise AssertionError("new_ib must not run while API socket is up")

    monkeypatch.setattr("abcxauto.broker.connector.new_ib", _boom)
    monkeypatch.setattr(IBKRConnector, "_start_heartbeat", lambda self: None)

    class _Live:
        def isConnected(self):
            return True

        loop = asyncio.get_running_loop()

    conn.ib = _Live()
    conn._connected = False
    conn._loop = asyncio.get_running_loop()
    ok = await conn.connect(max_retries=3)
    assert ok is True
    assert conn._connected is True
    assert created["n"] == 0
    IBKRConnector._instance = None


# ---------------------------------------------------------------------------
# Task 1 — bracket exception after fill → emergency flatten
# ---------------------------------------------------------------------------


class _OrderStatus:
    def __init__(self, status="Submitted", filled=0, avgFillPrice=0.0):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avgFillPrice
        self.remaining = 0


class _Order:
    def __init__(self, order_id: int):
        self.orderId = order_id
        self.orderType = "LMT"
        self.ocaGroup = ""


class _Trade:
    def __init__(self, order_id: int, status="Submitted"):
        self.order = _Order(order_id)
        self.orderStatus = _OrderStatus(status=status)
        self.log = []
        self.contract = SimpleNamespace(symbol="SPY")


class _BracketHarness(IBKROrdersMixin):
    """Minimal mixin host with fake IB for place_bracket_order tests."""

    def __init__(self):
        self.ib = MagicMock()
        self.net_liquidation = 100_000.0
        self.day_trades_remaining = -1
        self._order_state_lock = __import__("threading").Lock()
        self._bracket_groups = {}
        self._placed: List[Any] = []
        self._next_id = 1000

    async def _ensure_connected(self):
        return True

    async def _prepare_contract(self, symbol: str):
        return SimpleNamespace(symbol=symbol)

    async def _update_account_values(self):
        return None

    async def _wait_for_fill(self, trade, timeout: float = 5.0):
        return {
            "filled": True,
            "status": "Filled",
            "avg_fill_price": 100.0,
            "filled_quantity": 10,
        }

    def _place(self, contract, order):
        self._next_id += 1
        trade = _Trade(self._next_id)
        self._placed.append(order)
        # After entry fill path, first protection placeOrder raises
        if len(self._placed) == 2:
            raise RuntimeError("socket died mid-bracket")
        return trade


@pytest.mark.asyncio
async def test_bracket_exception_after_fill_emergency_flatten(monkeypatch):
    harness = _BracketHarness()
    harness.ib.placeOrder = harness._place

    async def _instant_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", _instant_sleep)

    result = await harness.place_bracket_order(
        symbol="SPY",
        quantity=10,
        direction="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
    )

    assert result.get("filled") is True
    assert result.get("success") is False
    assert "emergency_exit" in result
    assert result["emergency_exit"].get("attempted") is True
    # Entry + failed stop attempt + emergency MKT
    assert any(getattr(o, "orderType", None) == "MKT" for o in harness._placed)
    mkt = next(o for o in harness._placed if getattr(o, "orderType", None) == "MKT")
    assert mkt.action == "SELL"
    assert mkt.totalQuantity == 10
