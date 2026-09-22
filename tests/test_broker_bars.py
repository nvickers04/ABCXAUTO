"""IBKR hist spec + bar normalize."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.bars import bars_from_ibkr, hist_spec, ibkr_bar_freshness


def test_session_countback_keeps_the_930_print():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.broker.bars import session_countback

    et = ZoneInfo("America/New_York")
    midday = datetime(2026, 8, 25, 12, 46, tzinfo=et)
    late = datetime(2026, 8, 25, 15, 0, tzinfo=et)
    assert session_countback("5", n_symbols=2, now=midday) >= 40
    assert session_countback("5", n_symbols=2, now=late) >= 66
    assert session_countback("D", n_symbols=2, now=late) == 40


def test_hist_spec_maps_resolutions():
    from abcxauto.broker.bars import normalize_resolution

    assert hist_spec("D") == ("1 day", "6 M")
    assert hist_spec("15") == ("15 mins", "5 D")
    assert hist_spec("5") == ("5 mins", "3 D")
    assert hist_spec("60") == ("1 hour", "10 D")
    assert normalize_resolution("5min") == "5"
    assert normalize_resolution("5-min") == "5"
    assert hist_spec("5min") == ("5 mins", "3 D")
    assert hist_spec("15 minutes") == ("15 mins", "5 D")


def test_hist_spec_rejects_unknown_instead_of_daily():
    """Unknown must error — never silently serve 1-day bars."""
    from abcxauto.broker.bars import HIST_RESOLUTIONS, normalize_resolution

    assert "15" in HIST_RESOLUTIONS
    assert normalize_resolution("15") == "15"
    assert normalize_resolution("bogus") == "BOGUS"
    with pytest.raises(ValueError, match="unsupported resolution"):
        hist_spec("bogus")
    with pytest.raises(ValueError, match="unsupported resolution"):
        hist_spec("15D")


@pytest.mark.asyncio
async def test_get_historical_bars_keeps_15_and_rejects_unknown():
    """A 15 ask must request 15-mins hist; garbage resolution must not become D."""
    import asyncio
    from types import SimpleNamespace

    from abcxauto.broker.bars import IBKRBarsMixin

    class Conn(IBKRBarsMixin):
        def __init__(self):
            self.ib = SimpleNamespace()
            self.async_lock = asyncio.Lock()
            self._seen = None

        async def _ensure_connected(self):
            return True

        async def _prepare_contract(self, sym):
            return SimpleNamespace(symbol=sym, conId=1)

    conn = Conn()

    async def hist_req(contract, **kw):
        conn._seen = kw
        bar = SimpleNamespace(
            date="2026-09-21 10:00:00",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10,
        )
        return [bar]

    conn.ib.reqHistoricalDataAsync = hist_req
    out = await conn.get_historical_bars("AVGO", resolution="15", countback=40)
    assert out.get("error") is None
    assert out["resolution"] == "15"
    assert conn._seen["barSizeSetting"] == "15 mins"
    assert conn._seen["durationStr"] == "5 D"

    bad = await conn.get_historical_bars("AVGO", resolution="bogus", countback=40)
    assert "unsupported resolution" in str(bad.get("error") or "")
    assert not bad.get("bars")
    assert bad.get("resolution") != "D"


def test_bars_from_ibkr_skips_bad_close():
    good = SimpleNamespace(date="2026-08-18", open=1, high=2, low=0.5, close=1.5, volume=9)
    bad = SimpleNamespace(date="x", open=1, high=2, low=0.5, close=None, volume=0)
    rows = bars_from_ibkr([good, bad])
    assert len(rows) == 1
    assert rows[0]["c"] == 1.5
    assert rows[0]["v"] == 9
    assert ibkr_bar_freshness("15") == "ibkr_rth"


def test_bars_from_ibkr_realtime_open_underscore():
    bar = SimpleNamespace(
        time="2026-08-18T18:00:05",
        open_=310.4,
        high=310.8,
        low=310.3,
        close=310.6,
        volume=12,
    )
    rows = bars_from_ibkr([bar])
    assert rows[0]["o"] == 310.4
    assert rows[0]["c"] == 310.6
    assert ibkr_bar_freshness("5s") == "ibkr_rt_5s"
