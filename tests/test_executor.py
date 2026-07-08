"""Executor dispatch against a fake gateway: right method, right kwargs."""

import pytest

from abcxauto.executor import execute_proposal
from abcxauto.proposals import STRATEGIES, validate_proposal

from tests.test_proposals import RATIONALE, VALID_PAYLOADS

_GATEWAY_PREFIXES = ("place_", "modify_", "cancel_", "close_")


class FakeGateway:
    """Records every gateway call; returns a canned success dict.

    Holds 10 AAPL long so exit-only orders (SELL <= 10 AAPL) pass the
    executor's position check.
    """

    def __init__(self, positions=None):
        self.calls = []
        self.positions = positions if positions is not None else [
            {"symbol": "AAPL", "quantity": 10, "sec_type": "STK"},
        ]

    async def get_positions(self):
        return self.positions

    def __getattr__(self, name):
        if not name.startswith(_GATEWAY_PREFIXES):
            raise AttributeError(name)

        async def _method(**kwargs):
            self.calls.append((name, kwargs))
            return {"success": True, "order_id": 1234, "method": name}

        return _method


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
@pytest.mark.asyncio
async def test_dispatch_maps_strategy_to_gateway_method(strategy):
    proposal = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
    gateway = FakeGateway()

    result = await execute_proposal(proposal, gateway)

    assert result["success"] is True
    assert len(gateway.calls) == 1
    method, kwargs = gateway.calls[0]
    assert method == STRATEGIES[strategy][1]
    # Every explicitly provided param must reach the gateway (symbol upper-cased);
    # closing_position is an exit-only assertion and never reaches the gateway.
    for key, value in VALID_PAYLOADS[strategy].items():
        if key == "closing_position":
            assert key not in kwargs
            continue
        expected = value.upper() if key == "symbol" else value
        if key == "ratio":
            expected = tuple(value)
        assert kwargs[key] == expected, f"{strategy}: {key}"


@pytest.mark.asyncio
async def test_none_params_are_omitted():
    proposal = validate_proposal(
        "vertical_spread", VALID_PAYLOADS["vertical_spread"], RATIONALE
    )
    gateway = FakeGateway()
    await execute_proposal(proposal, gateway)
    _, kwargs = gateway.calls[0]
    assert "limit_price" not in kwargs  # was None -> excluded


@pytest.mark.asyncio
async def test_gateway_error_propagates():
    class ExplodingGateway:
        async def get_positions(self):
            return [{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}]

        async def place_market_order(self, **kwargs):
            raise RuntimeError("connection lost")

    proposal = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    with pytest.raises(RuntimeError, match="connection lost"):
        await execute_proposal(proposal, ExplodingGateway())


class TestExitOnlyVerification:
    """Bare orders marked closing_position must actually reduce a position."""

    @pytest.mark.asyncio
    async def test_exit_without_position_blocked(self):
        gateway = FakeGateway(positions=[])
        proposal = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert "does not reduce" in result["error"]
        assert gateway.calls == []  # never reached the broker

    @pytest.mark.asyncio
    async def test_exit_oversized_blocked(self):
        gateway = FakeGateway(positions=[{"symbol": "AAPL", "quantity": 3, "sec_type": "STK"}])
        proposal = validate_proposal(
            "market_order",
            {"symbol": "AAPL", "action": "SELL", "quantity": 5, "closing_position": True},
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_short_cover_allowed(self):
        gateway = FakeGateway(positions=[{"symbol": "TSLA", "quantity": -10, "sec_type": "STK"}])
        proposal = validate_proposal(
            "market_order",
            {"symbol": "TSLA", "action": "BUY", "quantity": 10, "closing_position": True},
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_bracket_needs_no_position(self):
        gateway = FakeGateway(positions=[])
        proposal = validate_proposal("bracket", VALID_PAYLOADS["bracket"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_stock_exit_against_option_position_hints_close_option(self):
        """Stock orders can't close options — the error must point at close_option."""
        gateway = FakeGateway(positions=[
            {"symbol": "SPY", "quantity": 1, "sec_type": "OPT", "strike": 745.0, "right": "C"},
        ])
        proposal = validate_proposal(
            "market_order",
            {"symbol": "SPY", "action": "SELL", "quantity": 1, "closing_position": True},
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert "close_option" in result["error"]
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_close_option_dispatches_without_position_check(self):
        gateway = FakeGateway(positions=[])
        proposal = validate_proposal("close_option", VALID_PAYLOADS["close_option"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        method, kwargs = gateway.calls[0]
        assert method == "close_option_position"
        assert kwargs == {"symbol": "SPY", "expiration": "20260709", "strike": 745.0, "right": "C"}
