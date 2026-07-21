"""Order examples catalog — agent-facing API contract coverage."""

import pytest

from abcxauto.config import Config, get_config
from abcxauto.order_examples import (
    ORDER_EXAMPLES,
    SENDABLE_TYPES,
    assert_examples_cover_strategies,
    format_order_examples,
)
from abcxauto.proposals import STRATEGIES, validate_proposal

RATIONALE = "test rationale"


@pytest.fixture(autouse=True)
def _relax_proposal_gates(monkeypatch):
    """Schema-shape checks; R:R gates have dedicated cases elsewhere."""
    base = get_config()
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(**{**base.__dict__, "defined_risk_only": False, "min_reward_risk": 0}),
    )


def test_every_strategy_has_an_example():
    assert_examples_cover_strategies()
    for key in STRATEGIES:
        assert key in ORDER_EXAMPLES


def test_hold_present():
    assert "hold" in ORDER_EXAMPLES
    assert "hold" in SENDABLE_TYPES
    assert ORDER_EXAMPLES["hold"] == {}


def test_set_risk_present():
    assert "set_risk" in ORDER_EXAMPLES
    assert "max_risk_per_trade_pct" in ORDER_EXAMPLES["set_risk"]


def test_sendable_types_matches_examples():
    assert SENDABLE_TYPES == frozenset(ORDER_EXAMPLES)


def test_format_order_examples():
    from abcxauto.agent_loop import ALLOWED_ACTIONS

    text = format_order_examples()
    assert text
    assert "ORDER EXAMPLES" in text
    assert "market_bracket" in text
    assert "set_risk" in text
    assert "vertical_spread" in text  # Act allowlist parity
    # Act-filtered: algo exits in ORDER_EXAMPLES but not ALLOWED_ACTIONS
    assert "vwap" not in text or "vwap" in ALLOWED_ACTIONS
    text_narrow = format_order_examples(allowed=frozenset({"hold", "market_bracket"}))
    assert "market_bracket" in text_narrow
    assert "vertical_spread" not in text_narrow
    assert "hold" in text_narrow


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_validate_proposal_accepts_non_hold_examples(strategy):
    proposal = validate_proposal(strategy, ORDER_EXAMPLES[strategy], RATIONALE)
    assert proposal.strategy == strategy
