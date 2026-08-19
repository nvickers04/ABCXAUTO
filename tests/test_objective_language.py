"""Book/wake text: no Controls lecture, no old scan headers."""

from __future__ import annotations

from abcxauto.config import get_config
from abcxauto.opportunity_scan import format_scan_tape, metrics_for_symbol


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


def test_metrics_no_advice_note():
    candles = [{"c": 100.0 + i * 0.1} for i in range(60)]
    idea = metrics_for_symbol(candles, "SPY")
    assert idea is not None
    assert "score" not in idea
    assert idea.get("source") == "mda"
    assert "uptrend support" not in str(idea).lower()


def test_book_facts_have_no_controls_lecture(tmp_path, monkeypatch):
    from abcxauto.brain import _book_payload
    from abcxauto.world_state import WorldState

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
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
    )
    blob = _book_payload(world)
    prompt = "\n".join(str(blob.get(k) or "") for k in ("world", "levers", "playbook"))
    assert "controls" not in blob
    assert "CONTROLS" not in prompt
    assert "mandate_summary" not in str(blob)
    assert "MANDATE" not in prompt
    assert "SCAN TAPE" in prompt or "scan_tape" in prompt.lower()
    assert "prefer manage" not in prompt.lower()
    assert "prefer acting" not in prompt.lower()
    assert "floor" not in blob
    assert "operator_card" not in blob
    assert "QUOTE SOURCES" in prompt or "IBKR" in prompt
    assert "idle_streak" not in prompt


def test_book_facts_surface_portfolio_risk(tmp_path, monkeypatch):
    from abcxauto.brain import _book_facts
    from abcxauto.world_state import WorldState

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    get_config.cache_clear()

    port = {
        "n_positions": 1,
        "top_symbol": "QQQ",
        "top_concentration_pct": 13.51,
        "exposure": {
            "top_symbol": "QQQ",
            "top_concentration_pct": 13.51,
            "symbols": [{"symbol": "QQQ", "pct_nl": 13.51}],
        },
        "capital_liquidity": {
            "total_cash": 32000.0,
            "cash_pct_nl": 86.49,
            "deployed_long_pct_nl": 13.51,
        },
    }
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk=port,
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    facts = _book_facts(world)
    assert facts["portfolio_risk"]["top_concentration_pct"] == 13.51
    assert facts["exposure"]["top_concentration_pct"] == 13.51
    assert facts["exposure"]["symbols"][0]["pct_nl"] == 13.51
    assert facts["capital_liquidity"]["cash_pct_nl"] == 86.49
    assert facts["capital_liquidity"]["deployed_long_pct_nl"] == 13.51


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
    )
    block = world.prompt_block()
    assert "SCAN TAPE" in block
    assert "MARKET FEATURES" not in block
    assert "OPPORTUNITIES (" not in block
    assert "QUOTE SOURCES" in block
    assert "daily close" in block.lower()
    assert "idle_streak" not in block
