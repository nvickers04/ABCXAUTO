"""Book tick resubscribe after a dead socket — no live TWS."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.connector import IBKRConnector


class _IB:
    def __init__(self):
        self.cancelled = []
        self.requested = []

    def cancelMktData(self, contract):
        self.cancelled.append(contract)

    def reqMktData(self, contract, *_a):
        self.requested.append(contract)

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
