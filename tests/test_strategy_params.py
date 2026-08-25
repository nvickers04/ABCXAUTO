"""Extra stock schemas stay exit-only; a naked BUY without closing_position is a leak."""

from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from abcxauto.order_examples import ORDER_EXAMPLES
from abcxauto.proposals import ProposalValidationError, validate_proposal
from abcxauto.strategy_params import (
    EXIT_ONLY_EXTRA,
    EXTRA_MANAGEMENT,
    EXTRA_STRATEGIES,
    OPTION_STRATEGIES,
    _exit_only,
    _naked_buy_probe,
    assert_extra_stock_exit_only,
    extra_bare_stock_strategies,
    extra_stock_naked_buy_leaks,
)

RATIONALE = "test rationale"


def test_exit_only_is_true_not_truthy():
    _exit_only(True)
    for closing in (False, None, 0, 1, "true"):
        with pytest.raises(ValueError, match="stop loss and take profit"):
            _exit_only(closing)  # type: ignore[arg-type]


def test_exit_only_registry_is_exactly_extra_bare_stock():
    assert extra_bare_stock_strategies() == EXIT_ONLY_EXTRA
    assert EXIT_ONLY_EXTRA.isdisjoint(OPTION_STRATEGIES)
    assert EXIT_ONLY_EXTRA.isdisjoint(EXTRA_MANAGEMENT)
    assert EXIT_ONLY_EXTRA <= frozenset(EXTRA_STRATEGIES)


def test_no_extra_stock_schema_leaks_naked_buy():
    assert extra_stock_naked_buy_leaks() == []
    assert_extra_stock_exit_only()


@pytest.mark.parametrize("strategy", sorted(EXIT_ONLY_EXTRA))
def test_extra_stock_without_closing_rejected(strategy):
    payload = {k: v for k, v in ORDER_EXAMPLES[strategy].items() if k != "closing_position"}
    with pytest.raises(ProposalValidationError, match="stop loss and take profit"):
        validate_proposal(strategy, payload, RATIONALE)


@pytest.mark.parametrize("strategy", sorted(EXIT_ONLY_EXTRA))
def test_extra_stock_naked_buy_rejected(strategy):
    payload = {k: v for k, v in ORDER_EXAMPLES[strategy].items() if k != "closing_position"}
    if "action" in payload:
        payload["action"] = "BUY"
    with pytest.raises(ProposalValidationError, match="stop loss and take profit"):
        validate_proposal(strategy, payload, RATIONALE)


@pytest.mark.parametrize("strategy", sorted(EXIT_ONLY_EXTRA))
def test_extra_stock_exit_accepted(strategy):
    proposal = validate_proposal(strategy, ORDER_EXAMPLES[strategy], RATIONALE)
    assert proposal.strategy == strategy
    assert "closing_position" not in proposal.params.model_dump()


@pytest.mark.parametrize("strategy", sorted(EXIT_ONLY_EXTRA))
def test_extra_stock_buy_to_cover_accepted(strategy):
    payload = dict(ORDER_EXAMPLES[strategy])
    payload["closing_position"] = True
    if "action" in payload:
        payload["action"] = "BUY"
    proposal = validate_proposal(strategy, payload, RATIONALE)
    assert proposal.strategy == strategy
    assert proposal.params.action == "BUY"


def test_naked_buy_probe_omits_closing_position():
    for name in sorted(EXIT_ONLY_EXTRA):
        model, _ = EXTRA_STRATEGIES[name]
        probe = _naked_buy_probe(model)
        assert "closing_position" not in probe
        assert probe.get("action") == "BUY"
        with pytest.raises(ValidationError, match="stop loss and take profit"):
            model.model_validate(probe)


def test_unlisted_extra_stock_is_a_leak(monkeypatch):
    class Orphan(BaseModel):
        symbol: str
        action: Literal["BUY", "SELL"]
        quantity: int = Field(gt=0)

    extra = dict(EXTRA_STRATEGIES)
    extra["orphan_moc"] = (Orphan, "place_orphan")
    monkeypatch.setattr("abcxauto.strategy_params.EXTRA_STRATEGIES", extra)
    leaks = extra_stock_naked_buy_leaks()
    assert any("orphan_moc" in item for item in leaks)


def test_schema_that_accepts_naked_buy_is_a_leak(monkeypatch):
    class Leaky(BaseModel):
        symbol: str
        action: Literal["BUY", "SELL"]
        quantity: int = Field(gt=0)

    extra = dict(EXTRA_STRATEGIES)
    extra["leaky_market"] = (Leaky, "place_leaky")
    monkeypatch.setattr("abcxauto.strategy_params.EXTRA_STRATEGIES", extra)
    monkeypatch.setattr(
        "abcxauto.strategy_params.EXIT_ONLY_EXTRA",
        EXIT_ONLY_EXTRA | {"leaky_market"},
    )
    leaks = extra_stock_naked_buy_leaks()
    assert any("leaky_market" in item and "accepted BUY" in item for item in leaks)
    with pytest.raises(RuntimeError, match="fail-closed"):
        assert_extra_stock_exit_only()
