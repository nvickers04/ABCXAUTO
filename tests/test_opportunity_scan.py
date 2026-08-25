"""SCAN TAPE metrics."""

import pytest

from abcxauto.config import (
    clear_runtime_overrides,
    get_config,
)
from abcxauto.opportunity_scan import (
    criteria_scan,
    metrics_for_symbol,
    normalize_tickers,
    reset_opportunity_cache,
)


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()
    reset_opportunity_cache()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()
    reset_opportunity_cache()


def _uptrend_candles(n: int = 60, base: float = 100.0) -> list[dict]:
    rows = []
    price = base
    for i in range(n):
        if i < n - 5:
            price += 0.15
        else:
            price -= 0.05
        rows.append({"t": i, "o": price, "h": price + 0.5, "l": price - 0.5, "c": price, "v": 1e6})
    return rows


def test_metrics_insufficient_data():
    assert metrics_for_symbol([{"c": 1.0}] * 5, "SPY") is None


def test_metrics_no_score_no_index_bump():
    idea = metrics_for_symbol(_uptrend_candles(), "SPY")
    assert idea is not None
    assert idea["symbol"] == "SPY"
    assert "score" not in idea
    assert idea["source"] == "mda"
    assert idea["freshness"] == "delayed_daily"
    assert idea["bar"] == "D"
    assert idea["mda_last_is"] == "daily_bar_close"
    assert idea["use"] == "mda_context_not_send_geometry"
    assert "dist20" in idea
    spy = metrics_for_symbol(_uptrend_candles(), "SPY")
    aapl = metrics_for_symbol(_uptrend_candles(), "AAPL")
    # Same candles → same metrics; no SPY-only bump field
    assert spy["dist20"] == aapl["dist20"]


def test_session_range_pins_ticker_open_over_midday_bars():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.opportunity_scan import session_range_from_bars

    et = ZoneInfo("America/New_York")
    bars = [
        {"t": "2026-08-25T10:15:00", "o": 133.47, "h": 135.81, "l": 132.94, "c": 134.0},
        {"t": "2026-08-25T10:20:00", "o": 134.0, "h": 134.4, "l": 133.8, "c": 134.05},
    ]
    rng = session_range_from_bars(
        bars,
        last=134.05,
        open_gap_pct=-3.802,
        rth_open=136.13,
        now=datetime(2026, 8, 25, 12, 46, tzinfo=et),
    )
    assert rng is not None
    assert rng["open"] == 136.13
    assert rng["above_open"] is False
    assert rng["low"] == 132.94
    assert rng["high"] == 136.13
    assert rng["gap_pct"] == -3.802


def test_session_range_uses_last_day_open_and_low():
    from abcxauto.opportunity_scan import session_range_from_bars

    bars = [
        {"t": "2026-08-24T15:00:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5},
        {"t": "2026-08-25T09:35:00", "o": 90.0, "h": 91.0, "l": 88.0, "c": 89.0},
        {"t": "2026-08-25T09:40:00", "o": 89.0, "h": 92.0, "l": 88.5, "c": 91.5},
    ]
    rng = session_range_from_bars(bars, last=91.2)
    assert rng is not None
    assert rng["date"] == "2026-08-25"
    assert rng["open"] == 90.0
    assert rng["low"] == 88.0
    assert rng["high"] == 92.0
    assert rng["last"] == 91.2
    assert rng["n"] == 2
    assert rng["above_open"] is True
    assert rng["above_low"] is True
    assert rng["vs_open"] == 1.2
    assert rng["vs_low"] == 3.2
    through = session_range_from_bars(bars, last=87.5)
    assert through["above_open"] is False
    assert through["above_low"] is False
    gapped = session_range_from_bars(bars, last=91.2, open_gap_pct=-10.0)
    assert gapped["prior_close"] == 100.0
    assert gapped["gap_pts"] == -10.0
    assert gapped["retrace_30"] == 93.0
    assert gapped["retrace_50"] == 95.0
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    assert session_range_from_bars(
        bars, last=91.2, now=datetime(2026, 8, 25, 16, 0, tzinfo=et)
    )["today"] is True
    assert session_range_from_bars(
        bars, last=91.2, now=datetime(2026, 8, 26, 6, 0, tzinfo=et)
    )["today"] is False


def test_session_range_derives_gap_from_prior_rth_close():
    from abcxauto.opportunity_scan import session_range_from_bars

    bars = [
        {"t": "2026-08-24T15:55:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0},
        {"t": "2026-08-25T09:35:00", "o": 90.0, "h": 91.0, "l": 88.0, "c": 89.0},
        {"t": "2026-08-25T09:40:00", "o": 89.0, "h": 92.0, "l": 88.5, "c": 91.5},
    ]
    rng = session_range_from_bars(bars, last=91.2)
    assert rng is not None
    assert rng["gap_pct"] == -10.0
    assert rng["prior_close"] == 100.0
    assert rng["retrace_30"] == 93.0
    assert rng["retrace_50"] == 95.0
    live = session_range_from_bars(bars, last=91.2, open_gap_pct=-8.0)
    assert live["gap_pct"] == -8.0


def test_session_range_does_not_derive_gap_from_premarket_open():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.opportunity_scan import session_range_from_bars

    et = ZoneInfo("America/New_York")
    bars = [
        {"t": "2026-08-24T15:55:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0},
        {"t": "2026-08-25T04:05:00", "o": 85.0, "h": 86.0, "l": 80.0, "c": 84.0},
        {"t": "2026-08-25T08:50:00", "o": 84.0, "h": 87.0, "l": 83.0, "c": 86.0},
    ]
    prem = session_range_from_bars(
        bars, last=86.0, now=datetime(2026, 8, 25, 8, 55, tzinfo=et)
    )
    assert prem is not None
    assert prem.get("rth") is False
    assert "gap_pct" not in prem


def test_session_range_does_not_use_premarket_low_as_opening_low():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.opportunity_scan import session_range_from_bars

    et = ZoneInfo("America/New_York")
    bars = [
        {"t": "2026-08-25T04:05:00", "o": 85.0, "h": 86.0, "l": 80.0, "c": 84.0},
        {"t": "2026-08-25T08:50:00", "o": 84.0, "h": 87.0, "l": 83.0, "c": 86.0},
        {"t": "2026-08-25T09:35:00", "o": 90.0, "h": 91.0, "l": 88.0, "c": 89.0},
        {"t": "2026-08-25T09:40:00", "o": 89.0, "h": 92.0, "l": 88.5, "c": 91.5},
    ]
    rng = session_range_from_bars(
        bars, last=91.2, now=datetime(2026, 8, 25, 10, 0, tzinfo=et)
    )
    assert rng["low"] == 88.0
    assert rng["open"] == 90.0
    assert rng["today"] is True
    assert rng["rth"] is True
    prem = session_range_from_bars(
        bars[:2], last=86.0, now=datetime(2026, 8, 25, 8, 55, tzinfo=et)
    )
    assert prem["today"] is False
    assert prem["rth"] is False
    assert prem["low"] == 80.0


def test_session_range_ibkr_compact_ignores_utc_wrong_t_iso():
    """formatDate=1 is ET wall. A UTC t_iso on that stamp is not the opening print."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.opportunity_scan import session_range_from_bars

    et = ZoneInfo("America/New_York")
    bars = [
        {
            "t": "20260825 08:50:00",
            "t_iso": "2026-08-25T08:50:00Z",
            "o": 84.0,
            "h": 87.0,
            "l": 80.0,
            "c": 86.0,
        },
        {
            "t": "20260825 09:35:00",
            "t_iso": "2026-08-25T09:35:00Z",
            "o": 90.0,
            "h": 91.0,
            "l": 88.0,
            "c": 89.0,
        },
        {
            "t": "20260825 13:35:00",
            "t_iso": "2026-08-25T13:35:00Z",
            "o": 91.0,
            "h": 92.0,
            "l": 90.5,
            "c": 91.5,
        },
    ]
    rng = session_range_from_bars(
        bars, last=91.2, now=datetime(2026, 8, 25, 14, 0, tzinfo=et)
    )
    assert rng is not None
    assert rng["open"] == 90.0
    assert rng["low"] == 88.0
    assert rng["rth"] is True
    assert rng["today"] is True


def test_structure_from_ibkr_bars_shares_sma_keys_not_mda_last():
    from abcxauto.opportunity_scan import structure_from_bars

    candles = _uptrend_candles()
    out = structure_from_bars(candles, "SPY", resolution="D", source="ibkr", freshness="ibkr_rth")
    assert out is not None
    assert out["source"] == "ibkr"
    assert out["freshness"] == "ibkr_rth"
    assert "sma20" in out and "dist20" in out and "ret5" in out
    assert "mda_last" not in out
    assert out["bar_last"] == pytest.approx(candles[-1]["c"])
    assert out["use"] == "ibkr_rth_structure"


def test_normalize_tickers_cap_and_regex():
    out = normalize_tickers(
        ["nvda", "bad symbol", "XLE", "nvda", "TOOLONGTICKER12", "BRK.B"],
        cap=3,
    )
    assert out == ["NVDA", "XLE", "BRK.B"]


def test_tape_seed_cap_matches_prompt():
    from abcxauto.opportunity_scan import TAPE_SEED_CAP

    assert TAPE_SEED_CAP == 12


def test_mda_bar_freshness_daily_vs_intraday():
    from abcxauto.opportunity_scan import mda_bar_freshness, mda_last_kind

    assert mda_bar_freshness("D") == "delayed_daily"
    assert mda_last_kind("D") == "daily_bar_close"
    assert mda_bar_freshness("15") == "delayed_15m"
    assert mda_last_kind("15") == "intrabar_close"


def test_metrics_intraday_not_daily_close():
    idea = metrics_for_symbol(_uptrend_candles(), "QQQ", resolution="15")
    assert idea is not None
    assert idea["freshness"] == "delayed_15m"
    assert idea["bar"] == "15"
    assert idea["mda_last_is"] == "intrabar_close"
    assert idea["mda_last_t"] == 59


def test_metrics_intraday_last_is_labeled():
    idea = metrics_for_symbol(_uptrend_candles(), "AAPL", resolution="15")
    assert idea is not None
    assert idea["mda_last_is"] == "intrabar_close"
    assert idea["source"] == "mda"


@pytest.mark.asyncio
async def test_criteria_scan_index_etfs_is_not_catalog_dump():
    """index_etfs has no IBKR spec — must not dump SPY/QQQ/IWM as a screen."""
    out = await criteria_scan(arena="index_etfs", connector=None)
    assert out.get("ok") is False
    assert out.get("symbols") in (None, [])
    assert out.get("hits") in (None, [])
    for name in ("SPY", "QQQ", "IWM", "DIA"):
        assert name not in (out.get("symbols") or [])
        assert name not in str(out.get("hits") or [])
    assert "catalog" in str(out.get("error") or "").lower() or "ibkr" in str(
        out.get("error") or ""
    ).lower()


@pytest.mark.asyncio
async def test_criteria_scan_industry_arena_not_catalog_even_with_connector():
    class Conn:
        connected = True

    out = await criteria_scan(arena="technology", connector=Conn())
    assert out.get("ok") is False
    assert "AAPL" not in (out.get("symbols") or [])
    assert "MSFT" not in (out.get("symbols") or [])
    assert out.get("hits") in (None, [])


@pytest.mark.asyncio
async def test_criteria_scan_mega_cap_without_ibkr_does_not_dump_catalog():
    out = await criteria_scan(arena="mega_cap", connector=None)
    assert out.get("ok") is False
    for name in ("AAPL", "MSFT", "NVDA", "AMZN"):
        assert name not in (out.get("symbols") or [])


@pytest.mark.asyncio
async def test_criteria_scan_symbols_still_returns_asked_names():
    out = await criteria_scan(symbols=["NVDA", "XLE"], connector=None)
    assert out["ok"] is True
    assert out["symbols"] == ["NVDA", "XLE"]
    assert out["source"] == "symbols"


@pytest.mark.asyncio
async def test_criteria_scan_catalog_seed_does_not_quote(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("catalog seed must not start a quote sweep")

    monkeypatch.setattr("abcxauto.opportunity_scan.attach_live_quotes", boom)
    out = await criteria_scan(arena="index_etfs", connector=object())
    assert out.get("ok") is False
    assert "SPY" not in (out.get("symbols") or [])


def test_session_range_from_live_open_at_the_bell():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.opportunity_scan import session_range_from_live_open
    from abcxauto.structure_grade import session_usable

    et = ZoneInfo("America/New_York")
    bell = datetime(2026, 8, 25, 9, 30, 20, tzinfo=et)
    rng = session_range_from_live_open(
        last=91.2,
        rth_open=90.0,
        open_gap_pct=-10.0,
        now=bell,
        regular=True,
    )
    assert rng is not None
    assert session_usable(rng) is True
    assert rng["open"] == 90.0
    assert rng["low"] == 90.0
    assert rng["last"] == 91.2
    assert rng["today"] is True
    assert rng["rth"] is True
    assert rng["print"] == "live_open"
    assert rng["above_low"] is True
    assert rng["gap_pct"] == -10.0
    prem = session_range_from_live_open(
        last=91.2,
        rth_open=90.0,
        open_gap_pct=-10.0,
        now=datetime(2026, 8, 25, 8, 55, tzinfo=et),
        regular=False,
    )
    assert prem is None
    on_lows = session_range_from_live_open(
        last=90.0,
        rth_open=90.0,
        now=bell,
        regular=True,
    )
    assert on_lows is not None
    assert on_lows["above_low"] is False
    stale = session_range_from_live_open(
        last=91.2,
        rth_open=110.0,
        open_gap_pct=-2.0,
        now=bell,
        regular=True,
    )
    assert stale is None
