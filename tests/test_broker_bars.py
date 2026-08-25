"""IBKR hist spec + bar normalize."""

from types import SimpleNamespace

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
