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
async def test_criteria_scan_deleted_screen_is_rejected_not_a_seed_dump():
    out = await criteria_scan(arena="index_etfs", connector=None)
    assert out.get("ok") is False
    assert out.get("symbols") in (None, [])
    assert out.get("hits") in (None, [])
    err = str(out.get("error") or "")
    assert "unknown screen" in err
    assert "valid=" in err
    assert "most_active" in err
    for name in ("SPY", "QQQ", "IWM", "DIA"):
        assert name not in (out.get("symbols") or [])
        assert name not in str(out.get("hits") or [])


@pytest.mark.asyncio
async def test_criteria_scan_deleted_industry_screen_not_catalog_even_with_connector():
    class Conn:
        connected = True

    out = await criteria_scan(arena="technology", connector=Conn())
    assert out.get("ok") is False
    assert "unknown screen" in str(out.get("error") or "")
    assert "AAPL" not in (out.get("symbols") or [])
    assert "MSFT" not in (out.get("symbols") or [])
    assert out.get("hits") in (None, [])


@pytest.mark.asyncio
async def test_criteria_scan_mega_cap_without_ibkr_does_not_dump_catalog():
    out = await criteria_scan(arena="mega_cap", connector=None)
    assert out.get("ok") is False
    assert "IBKR" in str(out.get("error") or "")
    for name in ("AAPL", "MSFT", "NVDA", "AMZN"):
        assert name not in (out.get("symbols") or [])
        assert name not in str(out)


@pytest.mark.asyncio
async def test_criteria_scan_symbols_still_returns_asked_names():
    out = await criteria_scan(symbols=["NVDA", "XLE"], connector=None)
    assert out["ok"] is True
    assert out["symbols"] == ["NVDA", "XLE"]
    assert out["source"] == "symbols"
    assert out.get("thin") is False
    assert out.get("empty") is False
    assert "on_book" in out["hits"][0]
    assert "watch" not in out


@pytest.mark.asyncio
async def test_criteria_scan_deleted_screen_does_not_quote(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("deleted screen must not start a quote sweep")

    monkeypatch.setattr("abcxauto.opportunity_scan.attach_live_quotes", boom)
    out = await criteria_scan(arena="index_etfs", connector=object())
    assert out.get("ok") is False
    assert "SPY" not in (out.get("symbols") or [])


def test_row_gap_pct_maps_distance_change_open_gap():
    from abcxauto.opportunity_scan import row_gap_pct

    assert row_gap_pct(
        {
            "distance": "135.5",
            "metric_name": "option_volume",
            "scan_code": "HOT_BY_OPT_VOLUME",
        }
    ) is None
    assert row_gap_pct(
        {
            "metric_name": "option_volume",
            "metric_value": 135.5,
            "gap_pct": 135.5,
            "scan_code": "HOT_BY_OPT_VOLUME",
        }
    ) is None
    assert row_gap_pct({"distance": "8.2%"}) is None
    assert row_gap_pct({"change_pct": -3.5}) is None
    assert row_gap_pct({"change": 4.25}) is None
    assert row_gap_pct({"open_gap_pct": -6.5}) == pytest.approx(-6.5)
    assert row_gap_pct({"gap%": 1.0, "distance": "9"}) == pytest.approx(1.0)
    assert row_gap_pct(
        {
            "metric_name": "option_volume",
            "metric_value": 135.5,
            "open_gap_pct": 31.06,
            "scan_code": "HOT_BY_OPT_VOLUME",
        }
    ) == pytest.approx(31.06)
    assert row_gap_pct({"ibkr": {"open_gap_pct": -7.1}}) == pytest.approx(-7.1)
    assert row_gap_pct({"quote": {"gap%": 2.5}}) == pytest.approx(2.5)
    assert row_gap_pct(
        {"distance": "8.2%", "scan_code": "HIGH_OPEN_GAP"}
    ) == pytest.approx(8.2)
    assert row_gap_pct(
        {"distance": "n/a", "scan_code": "HIGH_OPEN_GAP"}
    ) is None
    assert row_gap_pct(
        {"distance": "-4.1", "scan_code": "TOP_OPEN_PERC_LOSE"}
    ) == pytest.approx(-4.1)


def test_thin_ranked_row_carries_skip_class_and_named_metric():
    from abcxauto.opportunity_scan import RANKED_ROW_KEYS, thin_ranked_row

    row = thin_ranked_row(
        {
            "symbol": "nvda",
            "rank": 0,
            "distance": "12.4",
            "on_book": False,
            "last": 181.5,
            "bid": 181.4,
            "open_gap_pct": -5.0,
            "stock_type": "CORP",
        },
        screen="top_gainers",
        scan_code="TOP_PERC_GAIN",
    )
    assert row["symbol"] == "NVDA"
    assert row["rank"] == 0
    assert row["screen"] == "top_gainers"
    assert row["scan_code"] == "TOP_PERC_GAIN"
    assert row["metric_name"] == "percent_change"
    assert row["metric_value"] == pytest.approx(12.4)
    assert row["gap_pct"] == pytest.approx(-5.0)
    assert row["last"] == pytest.approx(181.5)
    assert row["stock_type"] == "CORP"
    assert row["skip_class"] == ""
    assert row["source"] == "ibkr"
    assert "bid" not in row
    assert "gap%" not in row
    assert set(row) <= RANKED_ROW_KEYS


def test_hot_by_opt_volume_distance_is_not_gap_pct():
    from abcxauto.opportunity_scan import row_gap_pct, thin_ranked_row

    noisy = {
        "symbol": "INIO",
        "rank": 0,
        "distance": "135.5",
        "metric_name": "option_volume",
        "metric_value": 135.5,
        "scan_code": "HOT_BY_OPT_VOLUME",
    }
    real = {
        "symbol": "ABCD",
        "rank": 1,
        "distance": "80.0",
        "open_gap_pct": 31.06,
        "metric_name": "option_volume",
        "metric_value": 80.0,
        "scan_code": "HOT_BY_OPT_VOLUME",
    }
    assert row_gap_pct(noisy) is None
    assert row_gap_pct(real) == pytest.approx(31.06)

    thin_noisy = thin_ranked_row(noisy, scan_code="HOT_BY_OPT_VOLUME")
    thin_real = thin_ranked_row(real, scan_code="HOT_BY_OPT_VOLUME")
    assert thin_noisy["metric_name"] == "option_volume"
    assert thin_noisy["metric_value"] == pytest.approx(135.5)
    assert "gap_pct" not in thin_noisy
    assert thin_real["metric_name"] == "option_volume"
    assert thin_real["metric_value"] == pytest.approx(80.0)
    assert thin_real["gap_pct"] == pytest.approx(31.06)
    assert row_gap_pct(thin_noisy) is None
    assert row_gap_pct(thin_real) == pytest.approx(31.06)


def test_thin_ranked_row_labels_levered_etf():
    from abcxauto.opportunity_scan import thin_ranked_row

    row = thin_ranked_row(
        {"symbol": "TQQQ", "rank": 1, "distance": "12.0"},
        screen="top_gainers",
        scan_code="TOP_PERC_GAIN",
    )
    assert row["skip_class"] == "levered"
    assert row["source"] == "ibkr"


def test_thin_ranked_row_omits_metric_when_ibkr_did_not_return_one():
    from abcxauto.opportunity_scan import thin_ranked_row

    row = thin_ranked_row(
        {"symbol": "AAPL", "rank": 2},
        screen="most_active",
        scan_code="MOST_ACTIVE",
    )
    assert "metric_name" not in row
    assert "metric_value" not in row
    assert row["skip_class"] == ""
    assert "last" not in row


def test_ranked_page_character_cost_is_measured():
    """Document the billed 30-row page size. Not a budget gate."""
    from abcxauto.opportunity_scan import thin_ranked_row

    old = [
        {"symbol": f"S{i:02d}", "gap%": 1.2 + i / 10, "rank": i, "arena": "top_gainers"}
        for i in range(30)
    ]
    new = [
        thin_ranked_row(
            {
                "symbol": f"S{i:02d}",
                "rank": i,
                "distance": str(1.2 + i / 10),
                "stock_type": "CORP",
            },
            screen="top_gainers",
            scan_code="TOP_PERC_GAIN",
        )
        for i in range(30)
    ]
    import json

    before = len(json.dumps(old, separators=(",", ":")))
    after = len(json.dumps(new, separators=(",", ":")))
    # New contract is richer (skip_class + named metric) and must stay compact.
    assert before > 0
    assert after > before
    assert after < 12_000
    # Stash for the worker report.
    test_ranked_page_character_cost_is_measured.before = before
    test_ranked_page_character_cost_is_measured.after = after


@pytest.mark.asyncio
async def test_criteria_scan_arena_emits_thin_gap_rows(monkeypatch):
    async def fake_pull(_connector=None, **_k):
        return {
            "ok": True,
            "arena_id": "top_gainers",
            "scan_code": "TOP_PERC_GAIN",
            "source": "ibkr",
            "symbols": ["NVDA", "AMD"],
            "rows": [
                {"symbol": "NVDA", "rank": 0, "distance": "12.4"},
                {"symbol": "AMD", "rank": 1, "distance": "-3.1"},
            ],
            "applied": {},
        }

    async def boom(*_a, **_k):
        raise AssertionError("ranked screen must not quote")

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)
    monkeypatch.setattr("abcxauto.opportunity_scan.attach_live_quotes", boom)
    out = await criteria_scan(scan_code="TOP_PERC_GAIN", connector=object())
    assert out["ok"] is True
    assert out["thin"] is True
    assert out["sort"] == "TOP_PERC_GAIN"
    assert out["criteria"]["scan_code"] == "TOP_PERC_GAIN"
    assert out["quoted"] == 0
    assert out["ranked"] is True
    assert out["empty"] is False
    assert "watch" not in out
    assert out["provenance"]["screen"] == "top_gainers"
    assert out["provenance"]["scan_code"] == "TOP_PERC_GAIN"
    assert out["provenance"]["empty"] is False
    assert "metric" in str(out.get("rank_meaning") or "")
    from abcxauto.opportunity_scan import RANKED_ROW_KEYS

    for row in out["hits"]:
        assert set(row) <= RANKED_ROW_KEYS
        assert row["source"] == "ibkr"
        assert "skip_class" in row
        assert "bid" not in row
        assert "distance" not in row
        assert "open_gap_pct" not in row
    assert out["hits"][0]["symbol"] == "NVDA"
    assert "gap_pct" not in out["hits"][0]
    assert out["hits"][0]["metric_name"] == "percent_change"
    assert out["hits"][0]["metric_value"] == pytest.approx(12.4)
    assert out["hits"][0]["rank"] == 0
    assert out["hits"][0]["skip_class"] == ""


@pytest.mark.asyncio
async def test_criteria_scan_empty_ibkr_is_labeled_empty(monkeypatch):
    async def fake_pull(_connector=None, **_k):
        return {
            "ok": True,
            "arena_id": "mega_cap",
            "scan_code": "HOT_BY_VOLUME",
            "source": "empty",
            "empty": True,
            "symbols": [],
            "rows": [],
            "applied": {"market_cap_above": 200_000_000_000},
            "ibkr_rows": 0,
            "kept": 0,
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)
    out = await criteria_scan(arena="mega_cap", connector=object())
    assert out["ok"] is True
    assert out["empty"] is True
    assert out["source"] == "empty"
    assert out["symbols"] == []
    assert out["hits"] == []
    assert out["provenance"]["empty"] is True
    assert out["provenance"]["ibkr_rows"] == 0
    assert out["provenance"]["kept"] == 0
    assert out["provenance"]["filters"]["market_cap_above"] == 200_000_000_000
    assert out["rank_meaning"] == "empty screen"
    assert "watch" not in out
    for name in ("SPY", "QQQ", "IWM", "AAPL"):
        assert name not in str(out)


@pytest.mark.asyncio
async def test_criteria_scan_symbols_stays_quote_fat(monkeypatch):
    async def quotes(rows, **_k):
        for row in rows:
            if row.get("symbol") == "NVDA":
                row["last"] = 181.5
                row["open_gap_pct"] = -5.0
                row["bid"] = 181.4
                row["ask"] = 181.6
        return 1

    monkeypatch.setattr("abcxauto.opportunity_scan.attach_live_quotes", quotes)
    out = await criteria_scan(symbols=["NVDA"], connector=object())
    assert out["ok"] is True
    assert out["thin"] is False
    assert out["hits"][0]["last"] == 181.5
    assert out["hits"][0]["open_gap_pct"] == -5.0
    assert out["hits"][0]["bid"] == 181.4
    assert "on_book" in out["hits"][0]
    assert "fat" in str(out.get("note") or "").lower()


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
