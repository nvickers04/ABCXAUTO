"""One book. The socket is the live switch. Not two rulebooks."""

from __future__ import annotations

import pytest

from abcxauto.broker.connection import (
    LIVE_CONFIRM_PHRASE,
    TradingModePortError,
    validate_trading_mode_port,
)
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.scorecard import format_scorecard_block
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK





def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK




def test_live_connect_without_confirm_still_refuses():
    with pytest.raises(TradingModePortError, match="LIVE_CONFIRM"):
        validate_trading_mode_port("live", 7496, "")
    with pytest.raises(TradingModePortError, match="LIVE_CONFIRM"):
        validate_trading_mode_port("live", 4001, "nope")
    validate_trading_mode_port("paper", 7497)
    validate_trading_mode_port("live", 7496, LIVE_CONFIRM_PHRASE)


def test_defined_risk_and_cash_only_fire_on_both_ports(monkeypatch):
    from abcxauto.config import Config, get_config
    from abcxauto.proposals import validate_proposal
    from abcxauto.risk_gates import check_defined_risk_only
    from tests.test_proposals import RATIONALE

    base = get_config()
    payload = {
        "symbol": "SPY",
        "expiration": "20260718",
        "long_strike": 500.0,
        "short_strike": 510.0,
        "right": "C",
        "ratio": 2,
        "quantity": 1,
    }
    for port, mode, paper in ((7497, "paper", True), (7496, "live", False)):
        cfg = Config(
            **{
                **base.__dict__,
                "ibkr_port": port,
                "trading_mode": mode,
                "defined_risk_only": True,
                "cash_only": True,
                "risk_posture": "balanced",
            }
        )
        monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda c=cfg: c)
        monkeypatch.setattr("abcxauto.proposals.get_config", lambda c=cfg: c)
        assert cfg.is_paper is paper
        ratio = validate_proposal("ratio_spread", payload, RATIONALE)
        ok, why = check_defined_risk_only(ratio)
        assert ok is False
        assert "defined_risk_only" in why
        mb = validate_proposal(
            "market_bracket",
            {
                "symbol": "SIRI",
                "quantity": 10,
                "direction": "LONG",
                "stop_price": 28.50,
                "target_price": 31.00,
            },
            RATIONALE,
            quote_last=29.75,
        )
        ok_stk, why_stk = check_defined_risk_only(mb)
        assert ok_stk is False
        assert "defined_risk_only" in why_stk
        assert cfg.defined_risk_only is True



def test_scorecard_header_labels_the_book(monkeypatch):
    sc = {
        "startup_cash": 1000.0,
        "net_liquidation": 1100.0,
        "book_pnl": 100.0,
        "book_return_pct": 10.0,
        "model_calls": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "model_cost_usd": 0.01,
        "model_cost_pct": 0.001,
        "edge_usd": 99.99,
        "edge_pct": 9.999,
        "beating_model": True,
        "fastest_beating": None,
        "best_pace": None,
        "windows": {},
        "session": None,
    }
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"is_paper": True, "trading_mode": "paper"})(),
    )
    paper = format_scorecard_block(sc=sc)
    assert paper.startswith("SCORECARD (paper TWS):")
    assert "+100.00$ paper" not in paper
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"is_paper": False, "trading_mode": "live"})(),
    )
    live = format_scorecard_block(sc=sc)
    assert live.startswith("SCORECARD (live TWS):")
    assert "real xAI" not in live
