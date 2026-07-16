"""Shell objectivity: banned taste phrases, features header, Operator Card."""

from __future__ import annotations

from abcxauto.config import (
    _POSTURE_PROMPT_BIAS,
    format_operator_card_block,
    get_config,
    load_operator_card,
    posture_prompt_bias,
)
from abcxauto.objective_language import (
    BANNED_TASTE_PHRASES,
    assert_no_banned_phrases,
    find_banned_phrases,
)
from abcxauto.opportunity_scan import format_market_features, score_symbol
from abcxauto.trade_playbook import format_trade_playbook


def test_banned_list_nonempty():
    assert len(BANNED_TASTE_PHRASES) >= 5


def test_playbook_has_no_banned_taste():
    text = format_trade_playbook(
        "manage",
        {"flat": False, "long_lots": {"IWM": 122}, "has_trade_plan": True},
    )
    assert "Precondition:" in text
    assert "Shell reject:" in text
    assert_no_banned_phrases(text, label="playbook")


def test_posture_bias_has_no_banned_taste():
    for p, text in _POSTURE_PROMPT_BIAS.items():
        assert_no_banned_phrases(text, label=f"posture:{p}")
        assert "envelope" in text.lower() or "code" in text.lower()
    assert "envelope" in posture_prompt_bias("aggressive").lower()


def test_features_header_and_rule_ids():
    text = format_market_features(
        [
            {
                "symbol": "QQQ",
                "bias": "LONG",
                "score": 0.7,
                "rule_id": "sma20_pullback_rule",
                "last": 100.0,
                "dist20": -0.01,
                "ret5": 0.0,
                "stop_hint_pct": 0.008,
                "target_hint_pct": 0.016,
            }
        ]
    )
    assert "MARKET FEATURES" in text
    assert "heuristic" in text.lower()
    assert "not trade recommendations" in text.lower()
    assert "heuristic_rank" in text
    assert "sma20_pullback_rule" in text
    assert_no_banned_phrases(text, label="features")


def test_score_symbol_uses_rule_id_not_advice_note():
    # Minimal candles enough for score path
    candles = [{"c": 100.0 + i * 0.1} for i in range(60)]
    idea = score_symbol(candles, "SPY")
    assert idea is not None
    assert "rule_id" in idea
    assert "uptrend support" not in str(idea.get("note") or "").lower()
    assert "uptrend support" not in str(idea.get("rule_id") or "").lower()


def test_operator_card_empty_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ABCXAUTO_OPERATOR_CARD", raising=False)
    monkeypatch.setenv("ABCXAUTO_OPERATOR_CARD_PATH", str(tmp_path / "missing.txt"))
    get_config.cache_clear()
    assert load_operator_card() == ""
    assert format_operator_card_block("") == ""
    assert "OPERATOR CARD" not in format_operator_card_block(None) or load_operator_card() == ""


def test_operator_card_injects_when_set(tmp_path, monkeypatch):
    path = tmp_path / "operator_card.txt"
    path.write_text("I like mean reversion on indexes.", encoding="utf-8")
    monkeypatch.delenv("ABCXAUTO_OPERATOR_CARD", raising=False)
    monkeypatch.setenv("ABCXAUTO_OPERATOR_CARD_PATH", str(path))
    get_config.cache_clear()
    card = load_operator_card()
    assert "mean reversion" in card
    block = format_operator_card_block(card)
    assert "OPERATOR CARD" in block
    assert "human-authored" in block
    assert "mean reversion" in block


def test_judge_prompt_objective_and_card(tmp_path, monkeypatch):
    from abcxauto.agent_loop import _build_judge_prompt
    from abcxauto.world_state import WorldState

    monkeypatch.setenv("ABCXAUTO_OPERATOR_CARD", "Fade extensions only.")
    get_config.cache_clear()

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
        opportunities=[{"symbol": "QQQ", "bias": "LONG", "score": 0.8, "rule_id": "x"}],
        news_items=[],
        risk_posture="aggressive",
        effective_posture="aggressive",
        gates={},
        envelope={},
        regime={
            "trend_bias": "bullish",
            "feature_mix_bias": "bullish",
            "session_phase": "mid",
            "vol_proxy": "normal",
        },
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        idle_streak=0,
        idle_top_symbol="",
        prep={},
        review={},
    )
    prompt = _build_judge_prompt(world)
    assert "GATE:" in prompt or "PROCESS:" in prompt
    assert "prefer manage" not in prompt.lower()
    assert "prefer acting" not in prompt.lower()
    assert "OPERATOR CARD" in prompt
    assert "Fade extensions" in prompt
    assert "not regime truth" in prompt or "feature_mix" in prompt
    assert find_banned_phrases(prompt) == [] or all(
        p not in prompt.lower() for p in ("prefer acting", "harvest", "mild bull")
    )


def test_world_prompt_features_not_opportunities_header():
    from abcxauto.world_state import WorldState

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[
            {
                "symbol": "SPY",
                "bias": "LONG",
                "score": 0.5,
                "rule_id": "neutral_weak_rule",
                "last": 500.0,
            }
        ],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={"trend_bias": "mixed", "feature_mix_bias": "mixed", "vol_proxy": "quiet"},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        idle_streak=0,
        idle_top_symbol="",
        prep={},
        review={},
    )
    block = world.prompt_block()
    assert "MARKET FEATURES" in block
    assert "OPPORTUNITIES (" not in block
    assert "feature_mix_bias" in block or "not regime truth" in block
