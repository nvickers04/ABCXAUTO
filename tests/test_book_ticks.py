"""Book tick resubscribe after a dead socket — no live TWS."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.connector import IBKRConnector


class _Wrapper:
    def __init__(self):
        self.tickers = {}
        self.ticker2ReqId = {"mktData": {}}


class _Ticker:
    def __init__(self, contract):
        self.contract = contract


class _IB:
    def __init__(self):
        self.cancelled = []
        self.requested = []
        self.wrapper = _Wrapper()
        self._next_req = 1

    def ticker(self, contract):
        return self.wrapper.tickers.get(id(contract))

    def cancelMktData(self, contract):
        self.cancelled.append(contract)
        ticker = self.wrapper.tickers.get(id(contract))
        if ticker is not None:
            self.wrapper.ticker2ReqId["mktData"].pop(ticker, None)

    def reqMktData(self, contract, *_a):
        self.requested.append(contract)
        req_id = self._next_req
        self._next_req += 1
        ticker = _Ticker(contract)
        self.wrapper.tickers[id(contract)] = ticker
        self.wrapper.ticker2ReqId["mktData"][ticker] = req_id
        return ticker

    async def qualifyContractsAsync(self, contract):
        if not getattr(contract, "conId", 0):
            contract.conId = 1


class _Host:
    def __init__(self):
        self.connected = True
        self.ib = _IB()
        self._book_subs = {}
        self._book_sub_live = set()
        self.rt = []

    _clear_book_subs = IBKRConnector._clear_book_subs
    ensure_book_ticks = IBKRConnector.ensure_book_ticks
    _mkt_data_has_live_sub = IBKRConnector._mkt_data_has_live_sub
    _mkt_data_cancel_if_live = IBKRConnector._mkt_data_cancel_if_live

    async def _prepare_contract(self, symbol):
        return SimpleNamespace(symbol=symbol, conId=99)

    def start_realtime_bars(self, symbol, contract, **_k):
        self.rt.append((symbol, getattr(contract, "conId", None)))


def test_clear_book_subs_skips_cancel_when_not_live():
    host = _Host()
    dead = SimpleNamespace(conId=1)
    host._book_subs = {1: dead}
    host._clear_book_subs(cancel=True)
    assert host._book_subs == {}
    assert host.ib.cancelled == []


@pytest.mark.asyncio
async def test_ensure_book_ticks_resubscribes_after_clear():
    host = _Host()
    await host.ensure_book_ticks(
        [{"conId": 1, "symbol": "AAPL", "secType": "STK"}]
    )
    assert host.ib.requested
    assert 1 in host._book_sub_live
    assert host.rt == [("AAPL", 99)]


@pytest.mark.asyncio
async def test_ensure_book_ticks_does_not_cancel_stale_gone():
    host = _Host()
    stale = SimpleNamespace(conId=7)
    host._book_subs = {7: stale}
    await host.ensure_book_ticks([])
    assert host.ib.cancelled == []
    assert host._book_subs == {}


def test_mkt_data_cancel_skips_never_subscribed():
    host = _Host()
    contract = SimpleNamespace(conId=42)
    host._mkt_data_cancel_if_live(contract)
    assert host.ib.cancelled == []


def test_mkt_data_cancel_subscribed_once():
    host = _Host()
    contract = SimpleNamespace(conId=42)
    host.ib.reqMktData(contract, "", True, False)
    host._mkt_data_cancel_if_live(contract)
    assert host.ib.cancelled == [contract]
    assert not host._mkt_data_has_live_sub(contract)


def test_mkt_data_second_cancel_is_noop():
    host = _Host()
    contract = SimpleNamespace(conId=42)
    host.ib.reqMktData(contract, "", True, False)
    host._mkt_data_cancel_if_live(contract)
    host._mkt_data_cancel_if_live(contract)
    assert host.ib.cancelled == [contract]


def test_mkt_data_two_contracts_independent():
    host = _Host()
    a = SimpleNamespace(conId=1)
    b = SimpleNamespace(conId=2)
    host.ib.reqMktData(a, "", True, False)
    host.ib.reqMktData(b, "", True, False)
    host._mkt_data_cancel_if_live(a)
    assert host.ib.cancelled == [a]
    assert host._mkt_data_has_live_sub(b)
    host._mkt_data_cancel_if_live(b)
    assert host.ib.cancelled == [a, b]


def test_mkt_data_teardown_clears_book_subs():
    host = _Host()
    contract = SimpleNamespace(conId=5)
    host.ib.reqMktData(contract, "", False, False)
    host._book_subs = {5: contract}
    host._book_sub_live = {5}
    host._clear_book_subs(cancel=True)
    assert host.ib.cancelled == [contract]
    assert host._book_subs == {}
    assert host._book_sub_live == set()
    assert not host._mkt_data_has_live_sub(contract)


def test_mkt_data_skip_book_preserves_stream():
    host = _Host()
    contract = SimpleNamespace(conId=9)
    host.ib.reqMktData(contract, "", False, False)
    host._book_sub_live = {9}
    host._mkt_data_cancel_if_live(contract, skip_book=True)
    assert host.ib.cancelled == []
    assert host._mkt_data_has_live_sub(contract)
