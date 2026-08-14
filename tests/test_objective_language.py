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
from abcxauto.opportunity_scan import format_scan_tape, metrics_for_symbol
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


def test_scan_tape_header_and_quote_sources():
    text = format_scan_tape(
        [
            {
                "symbol": "QQQ",
                "source": "mda",
                "freshness": "delayed",
                "mda_last": 100.0,
                "dist20": -0.01,
                "ret5": 0.0,
                "sma20": 101.0,
                "sma50": 100.0,
                "above_sma20": False,
            }
        ]
    )
    assert "SCAN TAPE" in text
    assert "delayed" in text.lower()
    assert "QUOTE SOURCES" in text or "IBKR" in text
    assert "heuristic_rank" not in text
    assert "MARKET FEATURES" not in text
    assert_no_banned_phrases(text, label="scan_tape")


def test_metrics_no_advice_note():
    candles = [{"c": 100.0 + i * 0.1} for i in range(60)]
    idea = metrics_for_symbol(candles, "SPY")
    assert idea is not None
    assert "score" not in idea
    assert idea.get("source") == "mda"
    assert "uptrend support" not in str(idea).lower()


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
    assert "secondary to CONTROLS" in block
    assert "mean reversion" in block


def test_format_controls_block_always_present():
    from abcxauto.config import format_controls_block

    block = format_controls_block()
    assert block.startswith("CONTROLS")
    assert "deliberation=" in block
    assert "intelligence_budget=" in block
    assert "capital_rotation=" in block
    assert "option_complexity=" in block
    assert "entry_surface=" in block
    assert "book_capacity" in block or "max_open_positions=" in block
    assert "UNIVERSE" in block


def test_book_facts_objective_and_card(tmp_path, monkeypatch):
    from abcxauto.brain import _book_payload
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
        opportunities=[
            {
                "symbol": "QQQ",
                "source": "mda",
                "freshness": "delayed",
                "mda_last": 100.0,
                "dist20": 0.0,
                "ret5": 0.0,
            }
        ],
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
    )
    blob = _book_payload(world)
    prompt = "\n".join(str(blob.get(k) or "") for k in ("world", "controls", "operator_card", "floor"))
    assert "CONTROLS" in prompt
    assert "deliberation=" in prompt
    assert "entry_surface=" in prompt or "option_complexity=" in prompt
    assert "SCAN TAPE" in prompt or "scan_tape" in prompt.lower()
    assert "prefer manage" not in prompt.lower()
    assert "prefer acting" not in prompt.lower()
    assert "OPERATOR CARD" in prompt or "Fade extensions" in prompt
    assert "QUOTE SOURCES" in prompt or "IBKR" in prompt
    assert find_banned_phrases(prompt) == [] or all(
        p not in prompt.lower() for p in ("prefer acting", "harvest", "mild bull")
    )


def test_world_prompt_scan_tape_not_opportunities_header():
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
                "source": "mda",
                "freshness": "delayed",
                "mda_last": 500.0,
                "dist20": 0.0,
                "ret5": 0.0,
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
    )
    block = world.prompt_block()
    assert "SCAN TAPE" in block
    assert "MARKET FEATURES" not in block
    assert "OPPORTUNITIES (" not in block
    assert "QUOTE SOURCES" in block
