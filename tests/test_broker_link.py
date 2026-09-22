"""Broker-link gates: stale book, fixed client id, cache flush, ticks, lock, port."""

from __future__ import annotations

import asyncio
import threading
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


def _quote_bag(**kwargs):
    """Minimal host for IBKRQueriesMixin.get_live_quote tests."""
    base = {
        "_QUOTE_CACHE_S": 2.5,
        "_ibkr_data_stale": False,
        "_quote_cache": {},
        "_tickers": {},
        "_quote_mkt_syms": set(),
        "_book_subs": {},
    }
    base.update(kwargs)
    bag = SimpleNamespace(**base)
    bag._live_quote_cached = lambda sym: IBKRQueriesMixin._live_quote_cached(bag, sym)
    bag._live_quote_remember = lambda sym, payload: IBKRQueriesMixin._live_quote_remember(
        bag, sym, payload
    )
    return bag


@pytest.mark.asyncio
async def test_get_live_quote_subscribes_once_no_cancel_churn(monkeypatch):
    """Streaming quote: one reqMktData, no cancelMktData, reuse on next call."""
    from abcxauto.broker import connector as connector_mod

    contract = SimpleNamespace(symbol="AVGO", conId=12345)
    ticker = SimpleNamespace(
        contract=contract, last=180.5, bid=180.4, ask=180.6, time=None
    )
    ib = MagicMock()
    ib.reqTickersAsync = AsyncMock(return_value=[ticker])
    ib.reqMktData = MagicMock(return_value=ticker)
    ib.cancelMktData = MagicMock()
    ib.ticker = MagicMock(return_value=ticker)

    bag = _quote_bag(
        ib=ib,
        _ensure_connected=AsyncMock(return_value=True),
        _prepare_contract=AsyncMock(return_value=contract),
    )
    monkeypatch.setattr(connector_mod, "_safe_sleep", AsyncMock())

    first = await IBKRQueriesMixin.get_live_quote(bag, "AVGO", fresh=True)
    second = await IBKRQueriesMixin.get_live_quote(bag, "AVGO", fresh=True)

    assert first.get("last") == 180.5
    assert second.get("last") == 180.5
    assert ib.reqMktData.call_count == 1
    assert ib.reqMktData.call_args.args[2] is False  # streaming, not snapshot
    ib.reqTickersAsync.assert_not_called()
    ib.cancelMktData.assert_not_called()
    assert "AVGO" in bag._tickers
    assert "AVGO" in bag._quote_mkt_syms


@pytest.mark.asyncio
async def test_get_live_quote_skips_cancel_when_only_tickers_async(monkeypatch):
    """Reuse path: subscribed stays False so cancelMktData must not run."""
    from abcxauto.broker import connector as connector_mod

    contract = SimpleNamespace(symbol="NVDA", conId=99)
    ticker = SimpleNamespace(
        contract=contract, last=500.0, bid=499.9, ask=500.1, time=None
    )
    ib = MagicMock()
    ib.reqMktData = MagicMock(return_value=ticker)
    ib.cancelMktData = MagicMock()
    ib.ticker = MagicMock(return_value=None)

    bag = _quote_bag(
        _tickers={"NVDA": ticker},
        _quote_mkt_syms={"NVDA"},
        ib=ib,
        _ensure_connected=AsyncMock(return_value=True),
        _prepare_contract=AsyncMock(return_value=contract),
    )
    monkeypatch.setattr(connector_mod, "_safe_sleep", AsyncMock())

    out = await IBKRQueriesMixin.get_live_quote(bag, "NVDA", fresh=True)
    assert out.get("last") == 500.0
    ib.reqMktData.assert_not_called()
    ib.cancelMktData.assert_not_called()


@pytest.mark.asyncio
async def test_get_live_quote_reuses_book_sub_without_req(monkeypatch):
    from abcxauto.broker import connector as connector_mod

    contract = SimpleNamespace(symbol="AMZN", conId=777)
    ticker = SimpleNamespace(
        contract=contract, last=200.0, bid=199.9, ask=200.1, time=None
    )
    ib = MagicMock()
    ib.reqMktData = MagicMock(return_value=ticker)
    ib.cancelMktData = MagicMock()
    ib.ticker = MagicMock(return_value=ticker)

    bag = _quote_bag(
        _book_subs={777: contract},
        ib=ib,
        _ensure_connected=AsyncMock(return_value=True),
        _prepare_contract=AsyncMock(return_value=contract),
    )
    monkeypatch.setattr(connector_mod, "_safe_sleep", AsyncMock())

    out = await IBKRQueriesMixin.get_live_quote(bag, "AMZN", fresh=True)
    assert out.get("last") == 200.0
    ib.reqMktData.assert_not_called()
    ib.cancelMktData.assert_not_called()
    ib.ticker.assert_called_once_with(contract)
    # Book mirror must not poison _tickers — lot exit would cancel the line.
    assert "AMZN" not in bag._tickers
    assert "AMZN" not in bag._quote_mkt_syms


@pytest.mark.asyncio
async def test_get_live_quote_rereqs_after_book_unsub(monkeypatch):
    """After book cancels the lot stream, quote must open its own line."""
    from abcxauto.broker import connector as connector_mod

    contract = SimpleNamespace(symbol="AMZN", conId=777)
    book_ticker = SimpleNamespace(
        contract=contract, last=200.0, bid=199.9, ask=200.1, time=None
    )
    fresh_ticker = SimpleNamespace(
        contract=contract, last=201.0, bid=200.9, ask=201.1, time=None
    )
    ib = MagicMock()
    ib.reqMktData = MagicMock(return_value=fresh_ticker)
    ib.cancelMktData = MagicMock()
    ib.ticker = MagicMock(return_value=book_ticker)

    bag = _quote_bag(
        _book_subs={777: contract},
        ib=ib,
        _ensure_connected=AsyncMock(return_value=True),
        _prepare_contract=AsyncMock(return_value=contract),
    )
    monkeypatch.setattr(connector_mod, "_safe_sleep", AsyncMock())

    first = await IBKRQueriesMixin.get_live_quote(bag, "AMZN", fresh=True)
    assert first.get("last") == 200.0
    ib.reqMktData.assert_not_called()

    bag._book_subs.clear()
    second = await IBKRQueriesMixin.get_live_quote(bag, "AMZN", fresh=True)
    assert second.get("last") == 201.0
    assert ib.reqMktData.call_count == 1
    assert ib.reqMktData.call_args.args[2] is False
    ib.cancelMktData.assert_not_called()
    assert "AMZN" in bag._tickers
    assert "AMZN" in bag._quote_mkt_syms


@pytest.mark.asyncio
async def test_get_live_quote_drops_stale_book_mirror_in_tickers(monkeypatch):
    """Poisoned _tickers entry from a prior book mirror must not block re-req."""
    from abcxauto.broker import connector as connector_mod

    contract = SimpleNamespace(symbol="META", conId=42)
    dead = SimpleNamespace(
        contract=contract, last=100.0, bid=99.9, ask=100.1, time=None
    )
    live = SimpleNamespace(
        contract=contract, last=105.0, bid=104.9, ask=105.1, time=None
    )
    ib = MagicMock()
    ib.reqMktData = MagicMock(return_value=live)
    ib.cancelMktData = MagicMock()
    ib.ticker = MagicMock(return_value=None)

    bag = _quote_bag(
        _tickers={"META": dead},  # not in _quote_mkt_syms → treated as foreign
        _book_subs={},
        ib=ib,
        _ensure_connected=AsyncMock(return_value=True),
        _prepare_contract=AsyncMock(return_value=contract),
    )
    monkeypatch.setattr(connector_mod, "_safe_sleep", AsyncMock())

    out = await IBKRQueriesMixin.get_live_quote(bag, "META", fresh=True)
    assert out.get("last") == 105.0
    assert ib.reqMktData.call_count == 1
    ib.cancelMktData.assert_not_called()
    assert "META" in bag._quote_mkt_syms


def test_farm_ok_codes_stay_quiet():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = False
    conn._connected = True
    conn._reconnect_requested = False
    for code in (2104, 2106, 2108, 2158):
        conn._on_error(-1, code, "farm ok", "")
    assert conn._ibkr_data_stale is False


def test_account_summary_exposes_stale_fact():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = True
    assert conn.ibkr_data_stale is True


def test_quote_cache_flushed_on_disconnect():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._quote_cache = {"SPY": (time.monotonic(), {"last": 501.0})}
    conn._tickers = {}
    conn._quote_mkt_syms = {"SPY"}
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
    assert conn._quote_mkt_syms == set()


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


async def _await_book_refresh(conn, *, timeout: float = 1.0) -> None:
    """Wait for a 1101/1102 book-refresh task (Task or threadsafe Future)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = getattr(conn, "_book_refresh_task", None)
        if task is not None:
            if asyncio.isfuture(task):
                await asyncio.wait_for(task, timeout=timeout)
                return
            result = getattr(task, "result", None)
            if callable(result):
                await asyncio.wait_for(asyncio.wrap_future(task), timeout=timeout)
                return
        await asyncio.sleep(0)


def _bind_owner_loop(conn) -> asyncio.AbstractEventLoop:
    owner = asyncio.get_running_loop()
    conn._loop = owner
    conn._async_lock = asyncio.Lock()
    conn._async_lock_loop = owner
    conn.ib = SimpleNamespace(loop=owner, isConnected=lambda: True)
    return owner


@pytest.mark.asyncio
async def test_1102_from_foreign_loop_refreshes_on_owner_loop():
    """1102 on a stray running loop must refresh on the connector owner loop."""
    IBKRConnector._instance = None
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = True
    conn._quote_cache = {"SPY": (time.monotonic(), {"last": 1.0})}
    conn._connected = True
    conn._book_refresh_task = None
    conn._maybe_resume_disconnect_halt = lambda **_k: None
    owner = _bind_owner_loop(conn)
    owner_id = id(owner)

    loops: list[int] = []

    async def _refresh():
        loops.append(id(asyncio.get_running_loop()))
        return True

    conn._refresh_book_after_data_loss = _refresh

    fired = threading.Event()
    seen: dict[str, object] = {}

    def _foreign() -> None:
        async def _fire() -> None:
            conn._on_error(-1, 1102, "Connectivity has been restored.", "")
            seen["stale"] = conn._ibkr_data_stale
            seen["loops"] = list(loops)
            seen["foreign"] = id(asyncio.get_running_loop())

        try:
            asyncio.run(_fire())
        finally:
            fired.set()

    t = threading.Thread(target=_foreign)
    t.start()
    assert fired.wait(timeout=2)
    t.join(timeout=2)
    assert not t.is_alive()
    assert seen["stale"] is True
    assert seen["loops"] == []

    await _await_book_refresh(conn)
    deadline = time.monotonic() + 1.0
    while not loops and time.monotonic() < deadline:
        await asyncio.sleep(0)
    assert loops == [owner_id]
    assert loops[0] != seen["foreign"]
    assert conn._ibkr_data_stale is False
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_failed_book_refresh_after_1102_stays_stale():
    IBKRConnector._instance = None
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = True
    conn._quote_cache = {"SPY": (time.monotonic(), {"last": 1.0})}
    conn._connected = True
    conn._book_refresh_task = None
    conn._maybe_resume_disconnect_halt = lambda **_k: None
    _bind_owner_loop(conn)

    async def _refresh():
        return False

    conn._refresh_book_after_data_loss = _refresh
    conn._on_error(-1, 1102, "Connectivity has been restored.", "")
    assert conn._ibkr_data_stale is True
    try:
        await _await_book_refresh(conn)
    except Exception:
        pass
    assert conn._ibkr_data_stale is True
    IBKRConnector._instance = None


@pytest.mark.asyncio
async def test_book_refresh_raise_after_1102_stays_stale():
    IBKRConnector._instance = None
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = True
    conn._quote_cache = {}
    conn._connected = True
    conn._book_refresh_task = None
    conn._maybe_resume_disconnect_halt = lambda **_k: None
    _bind_owner_loop(conn)

    async def _refresh():
        raise RuntimeError("book refresh failed")

    conn._refresh_book_after_data_loss = _refresh
    conn._on_error(-1, 1102, "Connectivity has been restored.", "")
    assert conn._ibkr_data_stale is True
    try:
        await _await_book_refresh(conn)
    except Exception:
        pass
    assert conn._ibkr_data_stale is True
    IBKRConnector._instance = None


def _after_connect_conn(*, refresh_ok: bool = True, refresh_raise: bool = False):
    """Minimal connector for _after_connect_restore (fresh process starts not-stale)."""
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = False
    conn._disconnect_cause = "unknown"
    conn._reconnect_requested = False
    conn._heartbeat_failures = 0
    conn._reconnect_attempt = 0
    conn._disconnect_since = None
    conn._last_heartbeat_ok = 0.0
    conn._pending_resubscribe = set()
    conn._quote_cache = {}
    conn._maybe_resume_disconnect_halt = lambda **_k: None

    async def _refresh() -> bool:
        if refresh_raise:
            raise RuntimeError("book refresh failed")
        return bool(refresh_ok)

    conn._refresh_book_after_data_loss = _refresh
    return conn


@pytest.mark.asyncio
async def test_after_connect_refresh_fail_marks_stale():
    """New process starts False; failed post-connect refresh must set stale."""
    conn = _after_connect_conn(refresh_ok=False)
    await conn._after_connect_restore()
    assert conn._ibkr_data_stale is True


@pytest.mark.asyncio
async def test_after_connect_refresh_raise_marks_stale():
    conn = _after_connect_conn(refresh_raise=True)
    await conn._after_connect_restore()
    assert conn._ibkr_data_stale is True


@pytest.mark.asyncio
async def test_after_connect_refresh_ok_clears_stale():
    conn = _after_connect_conn(refresh_ok=True)
    conn._ibkr_data_stale = True
    await conn._after_connect_restore()
    assert conn._ibkr_data_stale is False


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
