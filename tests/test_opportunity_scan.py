"""Opportunity scan scoring + prompt formatting."""

from abcxauto.agent_loop import _build_prompt, _risk_prompt_block
from abcxauto.config import (
    apply_risk_posture,
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
)
from abcxauto.opportunity_scan import (
    format_opportunities,
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
        # Mild uptrend with a soft pullback near the end
        if i < n - 5:
            price += 0.15
        else:
            price -= 0.05
        rows.append({"t": i, "o": price, "h": price + 0.5, "l": price - 0.5, "c": price, "v": 1e6})
    return rows


def test_score_symbol_insufficient_data():
    assert score_symbol([{"c": 1.0}] * 5, "SPY") is None


def test_score_symbol_ranks_spy_pullback():
    idea = score_symbol(_uptrend_candles(), "SPY")
    assert idea is not None
    assert idea["symbol"] == "SPY"
    assert idea["bias"] in ("LONG", "SHORT")
    assert 0 < float(idea["score"]) <= 1.0
    assert "stop_hint_pct" in idea


def test_format_opportunities_empty():
    text = format_opportunities([])
    assert "none" in text.lower()


def test_format_opportunities_lists():
    text = format_opportunities(
        [{"symbol": "QQQ", "bias": "LONG", "score": 0.7, "note": "test",
          "stop_hint_pct": 0.01, "target_hint_pct": 0.02}]
    )
    assert "QQQ" in text
    assert "OPPORTUNITIES" in text


def test_prompt_includes_opportunities_and_envelope(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_risk_posture("balanced", persist=True)

    snap = {
        "portfolio_state": {"positions": []},
        "positions": [],
        "opportunities": [
            {
                "symbol": "SPY",
                "bias": "LONG",
                "score": 0.8,
                "note": "fixture",
                "stop_hint_pct": 0.008,
                "target_hint_pct": 0.016,
            }
        ],
        "news_prompt": "",
        "reality_pulse": {},
    }
    prompt = _build_prompt(1, snap, needs_prot=False, c=None)
    assert "OPPORTUNITIES" in prompt
    assert "SPY" in prompt
    assert "RISK POSTURE" in prompt
    assert "balanced" in prompt
    assert "ENVELOPE" in _risk_prompt_block()
