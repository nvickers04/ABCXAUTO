"""IBKR hist spec + bar normalize."""

from types import SimpleNamespace

from abcxauto.broker.bars import bars_from_ibkr, hist_spec, ibkr_bar_freshness


def test_hist_spec_maps_resolutions():
    assert hist_spec("D") == ("1 day", "6 M")
    assert hist_spec("15") == ("15 mins", "5 D")
    assert hist_spec("5") == ("5 mins", "3 D")
    assert hist_spec("60") == ("1 hour", "10 D")


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
