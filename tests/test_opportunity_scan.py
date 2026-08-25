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
    assert "dist20" in idea
    spy = metrics_for_symbol(_uptrend_candles(), "SPY")
    aapl = metrics_for_symbol(_uptrend_candles(), "AAPL")
    # Same candles → same metrics; no SPY-only bump field
    assert spy["dist20"] == aapl["dist20"]


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
