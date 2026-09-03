"""Order examples catalog — agent-facing API contract coverage."""

import pytest

from abcxauto.config import Config, get_config
from abcxauto.order_examples import (
    COMBO_BAG_CLOSE,
    ORDER_EXAMPLES,
    SENDABLE_TYPES,
    assert_examples_cover_strategies,
    combo_close_example,
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
        lambda: Config(**{**base.__dict__, "defined_risk_only": False, "risk_posture": "balanced"}),
    )


def test_every_strategy_has_an_example():
    assert_examples_cover_strategies()
    for key in STRATEGIES:
        assert key in ORDER_EXAMPLES


def test_hold_absent_from_catalog():
    assert "hold" not in ORDER_EXAMPLES
    assert "hold" not in SENDABLE_TYPES


def test_set_risk_present():
    assert "set_risk" in ORDER_EXAMPLES
    assert "size_pct_nl" in ORDER_EXAMPLES["set_risk"]


def test_self_tune_present():
    assert "self_tune" in ORDER_EXAMPLES
    assert "self_tune" in SENDABLE_TYPES
    assert "controls" not in ORDER_EXAMPLES["self_tune"]


def test_sendable_types_matches_examples():
    assert SENDABLE_TYPES == frozenset(ORDER_EXAMPLES)


def test_format_order_examples():
    from abcxauto.agent_loop import ALLOWED_ACTIONS
    from abcxauto.order_examples import NOT_TICKETS, ticket_strategy_names

    text = format_order_examples()
    assert text
    assert "ORDER EXAMPLES" in text
    assert "market_bracket" in text
    assert "vertical_spread" in text  # Act allowlist parity
    assert "vwap" in text
    assert "vwap" in ALLOWED_ACTIONS
    assert "market_on_open" in ALLOWED_ACTIONS
    assert "self_tune:" not in text
    assert "set_risk:" not in text
    assert "self_tune" not in ticket_strategy_names()
    assert NOT_TICKETS <= SENDABLE_TYPES
    # Combo close is a sibling line, not a new strategy key.
    assert "vertical_spread close:" in text
    assert '"closing_position":true' in text
    assert "Never close_option / oca / trailing a combo leg" in text
    assert "together, not pick-one" in text
    assert "same size on 3 lots is not the same book as on 20 lots" in text
    assert "Widening the book does not replace size" in text
    # Illustration of together-not-pick-one — not a working size or slot count.
    assert "5%" not in text.split("iron condor")[0]
    assert "1%" not in text.split("iron condor")[0]
    assert "one WORKING at a time" in text
    assert "Fill or cancel, then the next" in text
    assert "Filled lots do not use that slot" in text
    assert "vertical, calendar, and diagonal are not in that IBKR [202] bucket" in text
    assert "Transmit" not in text
    assert "leg the combo" not in text.lower()
    header = text.split("\n\n")[0] if "\n\n" in text else text.split("vertical_spread:")[0]
    assert "iron condor" in header
    assert "iron butterfly" in header
    # Ticket lines still teach vertical/calendar/diagonal as sendable — not as [202].
    assert "vertical_spread:" in text
    assert "calendar_spread:" in text
    assert "diagonal_spread:" in text
    assert "Clerk will not invent the close price" in text
    assert "Clerk will not invent omitted stop/target/qty" in text
    assert "Clerk fills missing" not in text
    assert "price_hint" not in text
    assert "ratio_spread close:" not in text
    assert "jade_lizard close:" not in text
    # OPEN dict values stay free of closing_position (assert_examples 1:1).
    for name in COMBO_BAG_CLOSE:
        assert "closing_position" not in ORDER_EXAMPLES[name]
    for line in text.splitlines():
        if " close:" in line:
            assert '"limit_price"' not in line
            assert '"closing_position":true' in line
    text_narrow = format_order_examples(allowed=frozenset({"market_bracket"}))
    assert "market_bracket" in text_narrow
    assert "New risk requires card=" in text_narrow
    assert "card= is optional attribution" not in text_narrow
    assert '"card":"card-name"' in text_narrow
    assert "vertical_spread" not in text_narrow
    assert "vertical_spread close:" not in text_narrow
    assert "hold:" not in text_narrow


@pytest.mark.parametrize("strategy", sorted(COMBO_BAG_CLOSE))
def test_combo_close_example_same_legs_plus_close_fields(strategy):
    open_params = ORDER_EXAMPLES[strategy]
    close = combo_close_example(strategy)
    assert close["closing_position"] is True
    assert "limit_price" not in close
    for key, value in open_params.items():
        assert close[key] == value
    proposal = validate_proposal(strategy, close, RATIONALE)
    assert proposal.strategy == strategy
    assert proposal.params.closing_position is True
    assert proposal.params.limit_price is None


def test_examples_are_not_clerk_defaults_or_invented_fields():
    assert_examples_cover_strategies()
    for name, params in ORDER_EXAMPLES.items():
        if name not in STRATEGIES:
            continue
        fields = set(STRATEGIES[name][0].model_fields) | {"card"}
        assert set(params) <= fields, name
        assert "price_hint" not in params, name


def test_combo_bag_close_excludes_unlimited_shapes():
    assert "ratio_spread" not in COMBO_BAG_CLOSE
    assert "jade_lizard" not in COMBO_BAG_CLOSE
    assert COMBO_BAG_CLOSE <= set(ORDER_EXAMPLES)


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_validate_proposal_accepts_non_hold_examples(strategy):
    params = ORDER_EXAMPLES[strategy]
    # Clerk geometry uses live IBKR last, not a catalog price_hint.
    quote = None
    if strategy in ("market_bracket", "oca", "bracket"):
        quote = float(params.get("entry_price") or 100.0)
    proposal = validate_proposal(strategy, params, RATIONALE, quote_last=quote)
    assert proposal.strategy == strategy
