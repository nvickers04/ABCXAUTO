"""IBKR 5s stream buffer — no live TWS."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.queries import IBKRQueriesMixin


class _FakeBars(list):
    def __init__(self, rows=None):
        super().__init__(rows or [])
        self.updateEvent = None


class _Conn(IBKRQueriesMixin):
    def __init__(self, bars):
        self.ib = SimpleNamespace(reqRealTimeBars=lambda *a, **k: bars)
        self._connected = True

    async def _ensure_connected(self):
        return True

    async def _prepare_contract(self, symbol):
        return SimpleNamespace(symbol=symbol)


def test_start_realtime_bars_ingests_existing():
    raw = SimpleNamespace(
        time="2026-08-18T17:45:05",
        open_=310.4,
        high=310.8,
        low=310.3,
        close=310.6,
        volume=4,
    )
    bars = _FakeBars([raw])
    conn = _Conn(bars)
    conn.start_realtime_bars("AAPL", SimpleNamespace(symbol="AAPL"))
    assert conn._rt_buf["AAPL"][0]["c"] == 310.6
    assert conn.realtime_bar_buffer("AAPL")[0]["c"] == 310.6


@pytest.mark.asyncio
async def test_get_realtime_bars_returns_buffer_without_wait():
    raw = SimpleNamespace(
        time="2026-08-18T17:45:10",
        open_=311.0,
        high=311.2,
        low=310.9,
        close=311.1,
        volume=2,
    )
    bars = _FakeBars([raw])
    conn = _Conn(bars)
    out = await conn.get_realtime_bars("AAPL", resolution="15", wait_s=0)
    assert out["source"] == "ibkr"
    assert out["freshness"] == "ibkr_rt_5s"
    assert out["resolution"] == "5s"
    assert out["requested_resolution"] == "15"
    assert out["bars"][0]["c"] == 311.1
