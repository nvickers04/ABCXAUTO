"""Broker-link gates: stale book, fixed client id, cache flush, ticks, lock, port."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from abcxauto.broker.connection import DisconnectCause, stale_new_risk_block
from abcxauto.broker.connector import IBKRConnector, IBKRQueriesMixin
from abcxauto.broker.orders import IBKROrdersMixin
from abcxauto.broker.ticks import round_to_min_tick
import abcxauto.supervisor as sup


def test_round_to_min_tick_nearest_increment():
    assert round_to_min_tick(100.123, 0.01) == 100.12
    assert round_to_min_tick(1.234, 0.05) == 1.25
    assert round_to_min_tick(0.49, 0.01) == 0.49
    assert round_to_min_tick(98.004, 0.01) == 98.00


def test_stale_new_risk_block_reads_connector_flag():
    assert stale_new_risk_block(SimpleNamespace(_ibkr_data_stale=False)) is None
    out = stale_new_risk_block(SimpleNamespace(_ibkr_data_stale=True))
    assert out["error"] == "ibkr_data_stale"
    assert out["status"] == "rejected"


@pytest.mark.asyncio
async def test_stale_book_blocks_new_risk_but_not_exits():
    mixin = IBKROrdersMixin()
    mixin._ibkr_data_stale = True
    mixin.ib = MagicMock()
    mixin.net_liquidation = 100_000.0
    mixin.day_trades_remaining = -1
    mixin._order_state_lock = Lock()
    mixin._bracket_groups = {}

    async def _ok():
        return True

    mixin._ensure_connected = _ok
    out = await mixin.place_bracket_order("SPY", 10, "LONG", 100.0, 98.0, 103.0)
    assert out["error"] == "ibkr_data_stale"
    mixin.ib.placeOrder.assert_not_called()

    mkt = await mixin.place_market_bracket("SPY", 10, "LONG", 98.0, 103.0)
    assert mkt["error"] == "ibkr_data_stale"
    mixin.ib.placeOrder.assert_not_called()

    mixin._prepare_contract = AsyncMock(return_value=SimpleNamespace(symbol="SPY"))
    mixin._contract_min_tick = AsyncMock(return_value=0.01)
    mixin.get_open_orders = AsyncMock(return_value=[])
    mixin._check_order_rejection = AsyncMock(return_value=None)
    trade = SimpleNamespace(order=SimpleNamespace(orderId=9), orderStatus=SimpleNamespace(status="Submitted"))
    mixin.ib.placeOrder = MagicMock(return_value=trade)
    exit_out = await mixin.place_market_order("SPY", "SELL", 10, wait_for_fill=False)
    assert exit_out.get("success") is True
    mixin.ib.placeOrder.assert_called()


@pytest.mark.asyncio
async def test_get_live_quote_errors_when_stale():
    bag = SimpleNamespace(
        _QUOTE_CACHE_S=2.5,
        _ibkr_data_stale=True,
        _quote_cache={"SPY": (time.monotonic(), {"last": 501.0, "source": "ibkr"})},
    )
    out = await IBKRQueriesMixin.get_live_quote(bag, "SPY")
    assert out["error"] == "ibkr_data_stale"
    assert out["ibkr_data_stale"] is True


def test_account_summary_exposes_stale_fact():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = True
    assert conn.ibkr_data_stale is True


def test_quote_cache_flushed_on_disconnect():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._quote_cache = {"SPY": (time.monotonic(), {"last": 501.0})}
    conn._tickers = {}
    conn._ibkr_data_stale = False
    conn._disconnect_cause = DisconnectCause.USER_DISCONNECT.value
    conn.client_id = 42
    conn.host = "127.0.0.1"
    conn.port = 7497
    conn._pending_resubscribe = set()
    conn._clear_book_subs = lambda **_k: None
    conn._on_disconnect()
    assert conn._quote_cache == {}
    assert conn._ibkr_data_stale is True


def test_1100_marks_stale_and_drops_quote_cache():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = False
    conn._quote_cache = {"SPY": (time.monotonic(), {"last": 1.0})}
    conn._connected = True
    conn._reconnect_requested = False
    conn._disconnect_cause = "unknown"
    conn._schedule_reconnect = lambda reason: None
    conn._on_error(-1, 1100, "Connectivity between IBKR and TWS has been lost.", "")
    assert conn._ibkr_data_stale is True
    assert conn._quote_cache == {}
    assert conn._connected is True


@pytest.mark.asyncio
async def test_1102_forces_refresh_before_clearing_stale():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = True
    conn._quote_cache = {"SPY": (time.monotonic(), {"last": 1.0})}
    conn._loop = asyncio.get_running_loop()
    conn._connected = True
    refreshed = []

    async def _refresh():
        refreshed.append(1)
        return True

    conn._refresh_book_after_data_loss = _refresh
    conn._on_error(-1, 1102, "Connectivity has been restored.", "")
    assert conn._ibkr_data_stale is True
    await asyncio.sleep(0)
    if conn._book_refresh_task is not None:
        await asyncio.sleep(0)
    deadline = time.monotonic() + 1.0
    while not refreshed and time.monotonic() < deadline:
        await asyncio.sleep(0)
    assert refreshed == [1]
    assert conn._ibkr_data_stale is False
    assert conn._quote_cache == {}


@pytest.mark.asyncio
async def test_connect_retry_keeps_configured_client_id(monkeypatch):
    from abcxauto.broker import connector as connector_mod

    calls: list[int] = []

    class FakeIB:
        def __init__(self):
            self.disconnectedEvent = _Evt()
            self.execDetailsEvent = _Evt()
            self.orderStatusEvent = _Evt()
            self.errorEvent = _Evt()
            self._ok = False

        async def connectAsync(self, **kwargs):
            calls.append(int(kwargs["clientId"]))
            if len(calls) == 1:
                raise TimeoutError("stale session")
            self._ok = True

        def isConnected(self):
            return self._ok

        def disconnect(self):
            self._ok = False

        def reqMarketDataType(self, *_a):
            pass

        def managedAccounts(self):
            return ["DU123"]

        def accountValues(self, *_a):
            return []

        def __getattr__(self, name):
            return MagicMock()

    class _Evt:
        def __iadd__(self, other):
            return self

        def __isub__(self, other):
            return self

    monkeypatch.setattr(connector_mod, "IB", FakeIB)
    monkeypatch.setattr(connector_mod, "_safe_sleep", AsyncMock())
    monkeypatch.setattr(IBKRConnector, "_start_heartbeat", lambda self: None)
    monkeypatch.setattr(IBKRConnector, "_update_account_values", AsyncMock())
    IBKRConnector._instance = None
    conn = IBKRConnector()
    configured = int(conn.client_id)
    ok = await conn.connect(max_retries=3)
    assert ok is True
    assert calls == [configured, configured]
    assert conn.client_id == configured
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_place_order_rounds_to_contract_tick(monkeypatch):
    mixin = IBKROrdersMixin()
    contract = SimpleNamespace(symbol="PENNY")
    mixin.ib = MagicMock()
    mixin._order_state_lock = Lock()

    async def _ok():
        return True

    mixin._ensure_connected = _ok
    mixin._prepare_contract = AsyncMock(return_value=contract)
    mixin._contract_min_tick = AsyncMock(return_value=0.05)
    mixin.get_open_orders = AsyncMock(return_value=[])
    mixin._check_order_rejection = AsyncMock(return_value=None)
    captured = []

    def _place(_c, order):
        captured.append(order)
        return SimpleNamespace(order=SimpleNamespace(orderId=7), orderStatus=SimpleNamespace(status="Submitted"))

    mixin.ib.placeOrder = _place
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())
    out = await mixin.place_limit_order("PENNY", "BUY", 1, 1.23)
    assert out.get("success") is True
    assert captured[0].lmtPrice == 1.25


@pytest.mark.asyncio
async def test_bracket_stop_rests_before_entry_can_fill(monkeypatch):
    mixin = IBKROrdersMixin()
    mixin.ib = MagicMock()
    mixin.net_liquidation = 100_000.0
    mixin.day_trades_remaining = -1
    mixin._order_state_lock = Lock()
    mixin._bracket_groups = {}
    mixin._ibkr_data_stale = False
    placed = []
    nid = {"n": 10}

    class _T:
        def __init__(self, order):
            nid["n"] += 1
            self.order = order
            self.order.orderId = nid["n"]
            self.orderStatus = SimpleNamespace(status="Submitted")
            self.log = []

    def _place(_c, order):
        placed.append(order)
        return _T(order)

    mixin.ib.placeOrder = _place

    async def _ok():
        return True

    mixin._ensure_connected = _ok
    mixin._prepare_contract = AsyncMock(return_value=SimpleNamespace(symbol="SPY"))
    mixin._contract_min_tick = AsyncMock(return_value=0.01)
    mixin._update_account_values = AsyncMock()
    mixin.modify_stop_price = AsyncMock(return_value={"success": True})

    async def _fill(trade, timeout=30.0):
        return {
            "filled": True,
            "status": "Filled",
            "avg_fill_price": 100.0,
            "filled_quantity": 10,
        }

    mixin._wait_for_fill = _fill
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())

    out = await mixin.place_bracket_order("SPY", 10, "LONG", 100.0, 98.0, 103.0)
    assert out.get("success") is True
    assert out.get("protection") == "protected"
    assert placed[0].transmit is False
    assert placed[0].orderType == "LMT"
    assert placed[1].transmit is True
    assert placed[1].orderType == "STP"
    assert placed[1].parentId == placed[0].orderId


def test_on_execution_stamps_via_fill_ts_iso(monkeypatch):
    monkeypatch.delenv("ABCXAUTO_TWS_TIMEZONE", raising=False)
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._executions = {}
    conn._execution_lock = Lock()
    trade = SimpleNamespace(
        contract=SimpleNamespace(symbol="WMT"),
        order=SimpleNamespace(orderType="LMT", ocaGroup=None),
    )
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            time=datetime(2026, 8, 20, 15, 42, 6),
            shares=70,
            price=103.07,
            avgPrice=103.07,
            side="BOT",
            orderId=4443,
            execId="e1",
        ),
        commissionReport=None,
    )
    conn._on_execution(trade, fill)
    assert conn._executions["WMT"][0]["time"] == "2026-08-20T20:42:06.000Z"


def test_claim_desk_lock_write_failure_blocks_start(tmp_path, monkeypatch):
    blocker = tmp_path / "notdir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(blocker / "desk.lock"))
    assert sup.claim_desk_lock() is False


def test_claim_desk_lock_unreadable_blocks_start(tmp_path, monkeypatch):
    lock = tmp_path / "desk.lock"
    lock.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))
    assert sup.claim_desk_lock() is False


def test_tws_listen_endpoint_uses_configured_port(monkeypatch):
    monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
    monkeypatch.setenv("IBKR_PORT", "7496")
    host, port = sup.tws_listen_endpoint()
    assert host == "127.0.0.1"
    assert port == 7496


def test_crash_relaunch_probes_configured_port(monkeypatch):
    codes = [1, 0]
    launches = []
    probed = []

    class _Proc:
        def __init__(self, code: int) -> None:
            self._code = code
            self.pid = 501

        def wait(self) -> int:
            return self._code

    def _popen(*_a, **_kw):
        launches.append(1)
        return _Proc(codes.pop(0))

    def _listen(host="127.0.0.1", port=7497, timeout=2.0):
        probed.append((host, port))
        return True

    monkeypatch.setenv("IBKR_PORT", "7496")
    monkeypatch.setenv("IBKR_HOST", "10.0.0.8")
    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "useful_hours", lambda **_kw: True)
    monkeypatch.setattr(sup, "tws_listening", _listen)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    monkeypatch.setattr(sup, "live_pro_pids", lambda **_k: [])
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    assert sup.supervise() == 0
    assert len(launches) == 2
    assert probed
    assert probed[-1] == ("10.0.0.8", 7496)
