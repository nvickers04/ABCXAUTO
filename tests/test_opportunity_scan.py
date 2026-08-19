"""SCAN TAPE metrics + prompt formatting."""

from abcxauto.config import (
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
)
from abcxauto.opportunity_scan import (
    dismiss_cites_tape,
    format_opportunities,
    format_scan_tape,
    metrics_for_symbol,
    normalize_tickers,
    reset_opportunity_cache,
    score_symbol,
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


def test_score_symbol_compat_alias():
    assert score_symbol([{"c": 1.0}] * 5, "SPY") is None
    idea = score_symbol(_uptrend_candles(), "QQQ")
    assert idea is not None
    assert "score" not in idea


def test_normalize_tickers_cap_and_regex():
    out = normalize_tickers(
        ["nvda", "bad symbol", "XLE", "nvda", "TOOLONGTICKER12", "BRK.B"],
        cap=3,
    )
    assert out == ["NVDA", "XLE", "BRK.B"]


def test_tape_seed_cap_matches_prompt():
    from abcxauto.opportunity_scan import TAPE_SEED_CAP

    assert TAPE_SEED_CAP == 12


def test_format_scan_tape():
    text = format_scan_tape(
        [
            {
                "symbol": "QQQ",
                "source": "mda",
                "freshness": "delayed",
                "mda_last": 100.0,
                "dist20": 0.01,
                "ret5": 0.02,
                "sma20": 99.0,
                "sma50": 98.0,
                "above_sma20": True,
            }
        ]
    )
    assert "SCAN TAPE" in text
    assert "QQQ" in text
    assert "daily close" in text.lower()
    assert "not 15m" in text.lower() or "not a 15m" in text.lower()
    assert "not live" in text.lower() or "IBKR" in text
    assert "heuristic_rank" not in text
    assert "MARKET FEATURES" not in text


def test_format_opportunities_alias():
    assert "none" in format_opportunities([]).lower()


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


def test_dismiss_cites_tape():
    ideas = [{"symbol": "NVDA"}, {"symbol": "XLE"}]
    assert dismiss_cites_tape("NVDA too extended", ideas)
    assert not dismiss_cites_tape("no edge today", ideas)


def test_prompt_includes_scan_tape_and_quote_sources(tmp_path, monkeypatch):
    from abcxauto.world_state import WorldState

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100000,
        daily_pnl=0,
        positions=[],
        open_orders=[],
        opportunities=[
            {
                "symbol": "AAPL",
                "source": "mda",
                "freshness": "delayed",
                "mda_last": 200,
                "dist20": 0.0,
                "ret5": 0.0,
                "sma20": 200,
                "sma50": 198,
                "above_sma20": True,
            }
        ],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    prompt = world.prompt_block()
    assert "SCAN TAPE" in prompt
    assert "QUOTE SOURCES" in prompt
    assert "AAPL" in prompt
    assert "daily close" in prompt.lower()
    assert "MARKET FEATURES" not in prompt
