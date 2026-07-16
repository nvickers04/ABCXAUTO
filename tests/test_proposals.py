"""Proposal validation matrix: core allowlist + exit-only bare orders."""

import pytest

from abcxauto.config import Config, get_config
from abcxauto.order_examples import ORDER_EXAMPLES
from abcxauto.proposals import (
    STRATEGIES,
    ProposalValidationError,
    render_ticket,
    validate_proposal,
)

RATIONALE = "test rationale"

# One valid payload per allowlisted strategy (hold is prompt-only, not STRATEGIES).
VALID_PAYLOADS = {k: dict(v) for k, v in ORDER_EXAMPLES.items() if k in STRATEGIES}


@pytest.fixture(autouse=True)
def _relax_proposal_gates(monkeypatch):
    """Matrix tests cover schema shape; R:R gates have dedicated cases."""
    base = get_config()
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(**{**base.__dict__, "defined_risk_only": False, "min_reward_risk": 0}),
    )


def test_every_strategy_has_a_valid_payload_case():
    assert set(VALID_PAYLOADS) == set(STRATEGIES)


@pytest.mark.parametrize("strategy", sorted(VALID_PAYLOADS))
def test_valid_payloads_accepted(strategy):
    proposal = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
    assert proposal.strategy == strategy
    assert proposal.gateway_method == STRATEGIES[strategy][1]
    if "symbol" in VALID_PAYLOADS[strategy]:
        assert proposal.params.symbol == VALID_PAYLOADS[strategy]["symbol"].upper()
    render_ticket(proposal)


def test_proposal_ids_increment():
    a = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    b = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    assert b.id == a.id + 1


def test_unknown_strategy_rejected():
    with pytest.raises(ProposalValidationError, match="Unknown strategy"):
        validate_proposal("naked_yolo", {"symbol": "AAPL"}, RATIONALE)


def test_deleted_exotic_strategies_rejected():
    for name in ("modify_order", "naked_yolo"):
        with pytest.raises(ProposalValidationError, match="Unknown strategy"):
            validate_proposal(name, {"symbol": "SPY"}, RATIONALE)


def test_covered_call_and_trailing_are_allowlisted():
    for name in ("covered_call", "trailing_stop", "vertical_spread", "vwap", "roll_option"):
        assert name in STRATEGIES
        validate_proposal(name, VALID_PAYLOADS[name], RATIONALE)


def test_missing_rationale_rejected():
    with pytest.raises(ProposalValidationError, match="rationale"):
        validate_proposal("market_order", VALID_PAYLOADS["market_order"], "  ")


def test_missing_required_field_rejected():
    with pytest.raises(ProposalValidationError, match="limit_price"):
        validate_proposal(
            "limit_order",
            {"symbol": "AAPL", "action": "BUY", "quantity": 10},
            RATIONALE,
        )


def test_negative_quantity_rejected():
    with pytest.raises(ProposalValidationError, match="quantity"):
        validate_proposal(
            "market_order",
            {"symbol": "AAPL", "action": "BUY", "quantity": -5},
            RATIONALE,
        )


def test_bad_action_rejected():
    with pytest.raises(ProposalValidationError):
        validate_proposal(
            "market_order",
            {"symbol": "AAPL", "action": "HOLD", "quantity": 5},
            RATIONALE,
        )


class TestProtectionRequired:
    """Bare orders must declare closing_position; entries must use brackets."""

    @pytest.mark.parametrize("strategy", ["limit_order", "market_order", "stop_order", "stop_limit"])
    def test_bare_order_without_closing_flag_rejected(self, strategy):
        payload = {k: v for k, v in VALID_PAYLOADS[strategy].items() if k != "closing_position"}
        with pytest.raises(ProposalValidationError, match="stop loss and take profit"):
            validate_proposal(strategy, payload, RATIONALE)

    def test_bare_order_with_closing_flag_accepted(self):
        proposal = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
        assert "closing_position" not in proposal.params.model_dump()

    def test_market_bracket_price_ordering(self):
        with pytest.raises(ProposalValidationError, match="stop_price < target_price"):
            validate_proposal(
                "market_bracket",
                {
                    "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                    "stop_price": 110.0, "target_price": 95.0,
                },
                RATIONALE,
            )

    def test_management_strategies_flagged(self):
        for strategy in ("oca", "modify_stop", "modify_target", "cancel_order"):
            p = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
            assert p.is_management, strategy
        for strategy in ("bracket", "market_bracket", "market_order", "close_option"):
            p = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
            assert not p.is_management, strategy


class TestBracketOrdering:
    def test_long_stop_above_entry_rejected(self):
        with pytest.raises(ProposalValidationError, match="stop < entry < target"):
            validate_proposal(
                "bracket",
                {
                    "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                    "entry_price": 100.0, "stop_price": 105.0, "target_price": 110.0,
                },
                RATIONALE,
            )

    def test_short_ordering_enforced(self):
        with pytest.raises(ProposalValidationError, match="target < entry < stop"):
            validate_proposal(
                "bracket",
                {
                    "symbol": "NVDA", "quantity": 10, "direction": "SHORT",
                    "entry_price": 100.0, "stop_price": 95.0, "target_price": 110.0,
                },
                RATIONALE,
            )

    def test_valid_short_bracket(self):
        p = validate_proposal(
            "bracket",
            {
                "symbol": "NVDA", "quantity": 10, "direction": "SHORT",
                "entry_price": 100.0, "stop_price": 103.0, "target_price": 94.0,
                "price_hint": 100.0,
            },
            RATIONALE,
            quote_last=100.0,
        )
        assert p.params.direction == "SHORT"


class TestCloseOption:
    def test_bad_expiration_format_rejected(self):
        with pytest.raises(ProposalValidationError, match="YYYYMMDD"):
            validate_proposal(
                "close_option",
                {"symbol": "SPY", "expiration": "2026-07-31", "strike": 565.0, "right": "C"},
                RATIONALE,
            )


class TestMinRewardRisk:
    """Bracket / market_bracket must meet min reward:risk when configured."""

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        base = get_config()
        monkeypatch.setattr(
            "abcxauto.proposals.get_config",
            lambda: Config(**{**base.__dict__, "defined_risk_only": False, "min_reward_risk": 2.0}),
        )

    def test_bracket_below_min_rejected(self):
        with pytest.raises(ProposalValidationError, match="reward:risk"):
            validate_proposal(
                "bracket",
                {
                    "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                    "entry_price": 100.0, "stop_price": 95.0, "target_price": 105.0,
                },
                RATIONALE,
            )

    def test_bracket_meets_min_accepted(self):
        p = validate_proposal(
            "bracket",
            {
                "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                "entry_price": 100.0, "stop_price": 95.0, "target_price": 110.0,
                "price_hint": 100.0,
            },
            RATIONALE,
            quote_last=100.0,
        )
        assert p.strategy == "bracket"

    def test_market_bracket_skips_rr_without_price_hint(self):
        # quote_last satisfies geometry; absent price_hint still skips R:R
        p = validate_proposal(
            "market_bracket",
            {
                "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                "stop_price": 97.0, "target_price": 106.0,
            },
            RATIONALE,
            quote_last=100.0,
        )
        assert p.strategy == "market_bracket"
        assert getattr(p.params, "price_hint", None) is None

    def test_market_bracket_enforces_rr_with_price_hint(self):
        p = validate_proposal(
            "market_bracket",
            {
                "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                "stop_price": 95.0, "target_price": 110.0, "price_hint": 100.0,
            },
            RATIONALE,
            quote_last=100.0,
        )
        assert p.params.price_hint == 100.0

        with pytest.raises(ProposalValidationError, match="reward:risk"):
            validate_proposal(
                "market_bracket",
                {
                    "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                    "stop_price": 95.0, "target_price": 105.0, "price_hint": 100.0,
                },
                RATIONALE,
                quote_last=100.0,
            )

    def test_disabled_skips(self, monkeypatch):
        base = get_config()
        monkeypatch.setattr(
            "abcxauto.proposals.get_config",
            lambda: Config(**{**base.__dict__, "min_reward_risk": 0}),
        )
        p = validate_proposal(
            "bracket",
            {
                "symbol": "NVDA", "quantity": 10, "direction": "LONG",
                "entry_price": 100.0, "stop_price": 95.0, "target_price": 105.0,
                "price_hint": 100.0,
            },
            RATIONALE,
            quote_last=100.0,
        )
        assert p.strategy == "bracket"

    def test_market_bracket_accepts_side_buy_alias(self):
        """Live Grok often sends side=BUY instead of direction=LONG."""
        p = validate_proposal(
            "market_bracket",
            {
                "symbol": "SPY",
                "quantity": 2,
                "side": "BUY",
                "stop_price": 749.5,
                "target_price": 760.0,
                "price_hint": 752.0,
                "secType": "STK",
                "exchange": "SMART",
                "currency": "USD",
                "tif": "DAY",
            },
            RATIONALE,
            quote_last=752.0,
        )
        assert p.strategy == "market_bracket"
        assert p.params.direction == "LONG"
        dumped = p.params.model_dump(exclude_none=True)
        assert "side" not in dumped
        assert "secType" not in dumped
        assert "tif" not in dumped
