"""Proposal validation matrix: every strategy, good and bad payloads."""

import pytest

from abcxauto.proposals import (
    STRATEGIES,
    ProposalValidationError,
    render_ticket,
    validate_proposal,
)

RATIONALE = "test rationale"

VALID_PAYLOADS = {
    # Bare orders are exit-only: closing_position=True is required by validation
    # and re-verified against live positions by the executor.
    "limit_order": {
        "symbol": "AAPL", "action": "SELL", "quantity": 10,
        "limit_price": 150.0, "closing_position": True,
    },
    "market_order": {"symbol": "AAPL", "action": "SELL", "quantity": 5, "closing_position": True},
    "stop_order": {
        "symbol": "AAPL", "action": "SELL", "quantity": 10,
        "stop_price": 140.0, "closing_position": True,
    },
    "stop_limit": {
        "symbol": "AAPL", "action": "SELL", "quantity": 10,
        "stop_price": 140.0, "limit_price": 139.5, "closing_position": True,
    },
    "bracket": {
        "symbol": "NVDA", "quantity": 10, "direction": "LONG",
        "entry_price": 100.0, "stop_price": 95.0, "target_price": 110.0,
    },
    "market_bracket": {
        "symbol": "NVDA", "quantity": 10, "direction": "LONG",
        "stop_price": 95.0, "target_price": 110.0,
    },
    "oca": {
        "symbol": "NVDA", "quantity": 10, "direction": "LONG",
        "stop_price": 95.0, "target_price": 110.0,
    },
    "modify_stop": {"order_id": 101, "new_stop_price": 97.5},
    "modify_target": {"order_id": 102, "new_limit_price": 112.0},
    "modify_order": {"order_id": 104, "limit_price": 101.5, "quantity": 8},
    "cancel_order": {"order_id": 103},
    "close_option": {"symbol": "SPY", "expiration": "20260709", "strike": 745.0, "right": "C"},
    "trailing_stop": {
        "symbol": "TSLA", "quantity": 10, "direction": "LONG", "trail_percent": 5.0,
    },
    "trailing_stop_limit": {
        "symbol": "TSLA", "quantity": 10, "direction": "LONG",
        "trail_amount": 2.0, "limit_offset": 0.25,
    },
    "vertical_spread": {
        "symbol": "SPY", "expiration": "20260731",
        "long_strike": 550.0, "short_strike": 560.0, "right": "C",
    },
    "iron_condor": {
        "symbol": "SPY", "expiration": "20260731",
        "put_long_strike": 540.0, "put_short_strike": 550.0,
        "call_short_strike": 580.0, "call_long_strike": 590.0,
    },
    "iron_butterfly": {
        "symbol": "SPY", "expiration": "20260731",
        "center_strike": 565.0, "wing_width": 10.0,
    },
    "straddle": {"symbol": "SPY", "expiration": "20260731", "strike": 565.0},
    "strangle": {
        "symbol": "SPY", "expiration": "20260731",
        "put_strike": 550.0, "call_strike": 580.0,
    },
    "butterfly": {
        "symbol": "SPY", "expiration": "20260731",
        "lower_strike": 550.0, "middle_strike": 565.0, "upper_strike": 580.0,
    },
    "calendar_spread": {
        "symbol": "SPY", "strike": 565.0,
        "near_expiration": "20260731", "far_expiration": "20260831",
    },
    "diagonal_spread": {
        "symbol": "SPY", "near_strike": 560.0, "far_strike": 570.0,
        "near_expiration": "20260731", "far_expiration": "20260831",
    },
    "covered_call": {"symbol": "AAPL", "expiration": "20260731", "strike": 160.0, "shares": 100},
    "protective_put": {"symbol": "AAPL", "expiration": "20260731", "strike": 140.0, "shares": 200},
    "collar": {
        "symbol": "AAPL", "expiration": "20260731",
        "put_strike": 140.0, "call_strike": 160.0, "shares": 100,
    },
    "ratio_spread": {
        "symbol": "SPY", "expiration": "20260731",
        "long_strike": 560.0, "short_strike": 570.0, "right": "C", "ratio": [1, 2],
    },
    "jade_lizard": {
        "symbol": "SPY", "expiration": "20260731",
        "put_strike": 550.0, "call_short_strike": 580.0, "call_long_strike": 585.0,
    },
}


def test_every_strategy_has_a_valid_payload_case():
    assert set(VALID_PAYLOADS) == set(STRATEGIES)


@pytest.mark.parametrize("strategy", sorted(VALID_PAYLOADS))
def test_valid_payloads_accepted(strategy):
    proposal = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
    assert proposal.strategy == strategy
    assert proposal.gateway_method == STRATEGIES[strategy][1]
    if "symbol" in VALID_PAYLOADS[strategy]:
        assert proposal.params.symbol == VALID_PAYLOADS[strategy]["symbol"].upper()
    # Ticket rendering must not raise for any valid proposal
    render_ticket(proposal)


def test_proposal_ids_increment():
    a = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    b = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    assert b.id == a.id + 1


def test_unknown_strategy_rejected():
    with pytest.raises(ProposalValidationError, match="Unknown strategy"):
        validate_proposal("naked_yolo", {"symbol": "AAPL"}, RATIONALE)


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
        # The flag is an assertion, not a gateway parameter
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
        """Order management strategies are flagged separately from new trades."""
        for strategy in ("oca", "modify_stop", "modify_target", "modify_order", "cancel_order"):
            p = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
            assert p.is_management, strategy
        for strategy in ("bracket", "market_bracket", "market_order", "close_option"):
            p = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
            assert not p.is_management, strategy

    def test_modify_order_requires_a_change(self):
        with pytest.raises(ProposalValidationError, match="at least one"):
            validate_proposal("modify_order", {"order_id": 104}, RATIONALE)


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
                "entry_price": 100.0, "stop_price": 105.0, "target_price": 90.0,
            },
            RATIONALE,
        )
        assert p.params.direction == "SHORT"


class TestTrailingStop:
    def test_both_trails_rejected(self):
        with pytest.raises(ProposalValidationError, match="exactly one"):
            validate_proposal(
                "trailing_stop",
                {
                    "symbol": "TSLA", "quantity": 10, "direction": "LONG",
                    "trail_amount": 2.0, "trail_percent": 5.0,
                },
                RATIONALE,
            )

    def test_neither_trail_rejected(self):
        with pytest.raises(ProposalValidationError, match="exactly one"):
            validate_proposal(
                "trailing_stop",
                {"symbol": "TSLA", "quantity": 10, "direction": "LONG"},
                RATIONALE,
            )


class TestOptionValidation:
    def test_bad_expiration_format_rejected(self):
        with pytest.raises(ProposalValidationError, match="YYYYMMDD"):
            validate_proposal(
                "straddle",
                {"symbol": "SPY", "expiration": "2026-07-31", "strike": 565.0},
                RATIONALE,
            )

    def test_iron_condor_strike_ordering_rejected(self):
        with pytest.raises(ProposalValidationError, match="put_long < put_short"):
            validate_proposal(
                "iron_condor",
                {
                    "symbol": "SPY", "expiration": "20260731",
                    "put_long_strike": 550.0, "put_short_strike": 540.0,
                    "call_short_strike": 580.0, "call_long_strike": 590.0,
                },
                RATIONALE,
            )

    def test_vertical_same_strikes_rejected(self):
        with pytest.raises(ProposalValidationError, match="must differ"):
            validate_proposal(
                "vertical_spread",
                {
                    "symbol": "SPY", "expiration": "20260731",
                    "long_strike": 560.0, "short_strike": 560.0, "right": "C",
                },
                RATIONALE,
            )

    def test_calendar_expiration_ordering_rejected(self):
        with pytest.raises(ProposalValidationError, match="before far_expiration"):
            validate_proposal(
                "calendar_spread",
                {
                    "symbol": "SPY", "strike": 565.0,
                    "near_expiration": "20260831", "far_expiration": "20260731",
                },
                RATIONALE,
            )

    def test_odd_share_count_rejected_for_covered_call(self):
        with pytest.raises(ProposalValidationError, match="shares"):
            validate_proposal(
                "covered_call",
                {"symbol": "AAPL", "expiration": "20260731", "strike": 160.0, "shares": 150},
                RATIONALE,
            )

    def test_collar_put_above_call_rejected(self):
        with pytest.raises(ProposalValidationError, match="put_strike < call_strike"):
            validate_proposal(
                "collar",
                {
                    "symbol": "AAPL", "expiration": "20260731",
                    "put_strike": 160.0, "call_strike": 140.0, "shares": 100,
                },
                RATIONALE,
            )
