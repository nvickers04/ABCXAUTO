"""One book. The socket is the live switch. Not two rulebooks."""

from __future__ import annotations

import pytest

from abcxauto.broker.connection import (
    LIVE_CONFIRM_PHRASE,
    TradingModePortError,
    validate_trading_mode_port,
)
from abcxauto.lab_playbook import (
    apply_from_judgment,
    book_label,
    card_verdict,
    live_has_promoted,
    live_new_risk_allowed,
    load_lab,
    type_cards,
)
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.scorecard import format_scorecard_block
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


def _card(name: str) -> dict:
    return {
        "name": name,
        "thesis": "flush into support bounces",
        "retire_if": {"sample": 3, "condition": "x", "max_losses": 2},
        "fill_assumption": "full_spread",
    }


def _book(*names: str) -> dict:
    return {"types": {"market_bracket": {"cards": [_card(n) for n in names]}}}


def _paired_mode_keys(blob: dict) -> list[str]:
    keys = set(blob) | set((blob.get("calibration") or {}) if isinstance(blob.get("calibration"), dict) else {})
    pairs = []
    for k in keys:
        if k.startswith("paper_"):
            live = "live_" + k[len("paper_") :]
            if live in keys:
                pairs.append(k)
        if k.startswith("live_") and k != "live_confirm":
            paper = "paper_" + k[len("live_") :]
            if paper in keys:
                pairs.append(k)
    return pairs


def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_paper_may_write_four_testing_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    out = apply_from_judgment({"lab_playbook": _book("a", "b", "c", "d")})
    assert out is not None
    assert out.get("status") != "rejected"
    assert "hypothesis_cap" not in (out.get("rejected") or {})
    names = [c["name"] for c in type_cards(load_lab()["types"], "market_bracket")]
    assert names[:4] == ["a", "b", "c", "d"]


def test_card_verdict_has_no_paper_live_pair():
    card = {
        **_card("flush bounce"),
        "type": "market_bracket",
    }
    verdict = card_verdict(
        {"resolved": 4, "resolved_wins": 3, "resolved_pnl": 40.0, "conservative_pnl": 12.0},
        card,
    )
    assert verdict["resolved_pnl"] == 40.0
    assert verdict["calibration"]["hit_rate"] == 75.0
    assert _paired_mode_keys(verdict) == []
    for banned in (
        "paper_resolved_pnl",
        "live_resolved_pnl",
        "paper_hit_rate",
        "live_hit_rate",
        "paper_win_rate",
        "live_win_rate",
        "live_evidence",
    ):
        assert banned not in verdict
        assert banned not in (verdict.get("calibration") or {})


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
            }
        )
        monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda c=cfg: c)
        monkeypatch.setattr("abcxauto.proposals.get_config", lambda c=cfg: c)
        assert cfg.is_paper is paper
        ratio = validate_proposal("ratio_spread", payload, RATIONALE)
        ok, why = check_defined_risk_only(ratio)
        assert ok is False
        assert "defined_risk_only" in why


def test_new_risk_is_allowed_on_live_without_promote(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    assert live_has_promoted() is False
    assert live_new_risk_allowed() is True
    assert book_label() == "live TWS"


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
