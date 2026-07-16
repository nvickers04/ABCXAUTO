"""Opportunity scan scoring + prompt formatting."""

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
        [{"symbol": "QQQ", "bias": "LONG", "score": 0.7, "rule_id": "test_rule",
          "stop_hint_pct": 0.01, "target_hint_pct": 0.02, "last": 100.0}]
    )
    assert "QQQ" in text
    assert "MARKET FEATURES" in text
    assert "heuristic_rank" in text


def test_prompt_includes_features_and_envelope(tmp_path, monkeypatch):
    from abcxauto.world_state import WorldState

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_risk_posture("balanced", persist=True)

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[
            {
                "symbol": "SPY",
                "bias": "LONG",
                "score": 0.8,
                "rule_id": "fixture",
                "stop_hint_pct": 0.008,
                "target_hint_pct": 0.016,
                "last": 500.0,
            }
        ],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={"feature_mix_bias": "mixed", "trend_bias": "mixed"},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        idle_streak=0,
        idle_top_symbol="",
        prep={},
        review={},
    )
    prompt = world.prompt_block()
    assert "MARKET FEATURES" in prompt
    assert "SPY" in prompt
    assert get_config().risk_posture == "balanced"
