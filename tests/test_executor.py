"""Executor dispatch against a fake gateway: right method, right kwargs."""

import pytest

from abcxauto.executor import execute_proposal
from abcxauto.proposals import STRATEGIES, validate_proposal

from tests.test_proposals import RATIONALE, VALID_PAYLOADS

_GATEWAY_PREFIXES = ("place_", "modify_", "cancel_", "close_", "buy_", "sell_", "roll_")


@pytest.fixture(autouse=True)
def _disable_risk_gates(monkeypatch):
    """Dispatch tests isolate gateway mapping; risk gates covered in test_risk_gates."""
    from abcxauto.config import Config, get_config

    base = get_config()
    monkeypatch.setattr(
        "abcxauto.executor.get_config",
        lambda: Config(**{**base.__dict__, "risk_gates_enabled": False}),
    )
    # Proposal validation still runs for payload construction — relax Sprint-2
    # defined-risk / R:R so every strategy in VALID_PAYLOADS remains constructible.
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(**{**base.__dict__, "defined_risk_only": False, "min_reward_risk": 0, "risk_posture": "balanced"}),
    )


class FakeGateway:
    """Records every gateway call; returns a canned success dict.

    Holds stock for exit-only / trailing / covered-call checks, plus a SPY OPT
    for close_option. Exposes get_open_orders for cancel_order guards.
    """

    def __init__(self, positions=None, account=None, open_orders=None):
        self.calls = []
        self.positions = positions if positions is not None else [
            {"symbol": "AAPL", "quantity": 10, "sec_type": "STK"},
            {"symbol": "NVDA", "quantity": 10, "sec_type": "STK"},
            {"symbol": "TSLA", "quantity": 10, "sec_type": "STK"},
            {"symbol": "SPY", "quantity": 100, "sec_type": "STK"},
            {
                "symbol": "SPY", "quantity": 1, "sec_type": "OPT",
                "strike": 745.0, "right": "C", "expiration": "20260709",
                "market_value": 150.0,
            },
        ]
        self.account = account if account is not None else {
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
        }
        self.open_orders = open_orders if open_orders is not None else [
            # Redundant stop so cancel_order VALID_PAYLOAD (order_id=103) is not
            # the sole protector when AAPL is held — last-stop tests override this.
            {
                "order_id": 103, "symbol": "AAPL", "sec_type": "STK",
                "action": "SELL", "quantity": 10, "order_type": "LMT",
            },
        ]

    async def get_positions(self):
        return self.positions

    async def get_account_summary(self):
        return self.account

    async def get_open_orders(self):
        return self.open_orders

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
    # Fields with Field(exclude=True) stay on the model for gates but never hit IBKR.
    _never_dispatch = frozenset({"closing_position", "price_hint"})
    for key, value in VALID_PAYLOADS[strategy].items():
        if key in _never_dispatch:
            assert key not in kwargs
            continue
        expected = value.upper() if key == "symbol" else value
        if key == "ratio" and isinstance(value, (list, tuple)):
            expected = tuple(value)
        assert kwargs[key] == expected, f"{strategy}: {key}"


@pytest.mark.asyncio
async def test_none_params_are_omitted():
    proposal = validate_proposal(
        "close_option",
        {**VALID_PAYLOADS["close_option"], "limit_price": None},
        RATIONALE,
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

        async def get_account_summary(self):
            return {"netliquidation": 100_000.0, "dailypnl": 0.0}

        async def get_open_orders(self):
            return []

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


class TestCloseOptionVerification:
    """close_option must match a live OPT position before dispatch."""

    @pytest.mark.asyncio
    async def test_close_option_without_position_blocked(self):
        gateway = FakeGateway(positions=[])
        proposal = validate_proposal("close_option", VALID_PAYLOADS["close_option"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert "close_option" in result["error"].lower() or "no matching" in result["error"].lower()
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_close_option_with_matching_position_dispatches(self):
        gateway = FakeGateway(positions=[
            {
                "symbol": "SPY", "quantity": 2, "sec_type": "OPT",
                "strike": 745.0, "right": "C", "expiration": "20260709",
            },
        ])
        proposal = validate_proposal("close_option", VALID_PAYLOADS["close_option"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        method, kwargs = gateway.calls[0]
        assert method == "close_option_position"
        assert kwargs["symbol"] == "SPY"
        assert kwargs["expiration"] == "20260709"
        assert kwargs["strike"] == 745.0
        assert kwargs["right"] == "C"
        assert kwargs.get("quantity") == 1

    @pytest.mark.asyncio
    async def test_close_option_by_conId_resolves_identity(self):
        gateway = FakeGateway(positions=[
            {
                "symbol": "SPY", "quantity": -2, "sec_type": "OPT",
                "strike": 745.0, "right": "C", "expiration": "20260709",
                "conId": 999001,
            },
        ])
        proposal = validate_proposal(
            "close_option",
            {"symbol": "SPY", "conId": 999001, "quantity": 1},
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        method, kwargs = gateway.calls[0]
        assert method == "close_option_position"
        assert kwargs["expiration"] == "20260709"
        assert kwargs["strike"] == 745.0
        assert kwargs["right"] == "C"
        assert "conId" not in kwargs

    @pytest.mark.asyncio
    async def test_close_option_partial_allowed(self):
        gateway = FakeGateway(positions=[
            {
                "symbol": "SPY", "quantity": 3, "sec_type": "OPT",
                "strike": 745.0, "right": "C", "expiration": "20260709",
            },
        ])
        proposal = validate_proposal(
            "close_option",
            {
                "symbol": "SPY", "expiration": "20260709", "strike": 745.0,
                "right": "C", "quantity": 1,
            },
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_close_option_oversized_blocked(self):
        gateway = FakeGateway(positions=[
            {
                "symbol": "SPY", "quantity": 1, "sec_type": "OPT",
                "strike": 745.0, "right": "C", "expiration": "20260709",
            },
        ])
        proposal = validate_proposal(
            "close_option",
            {
                "symbol": "SPY", "expiration": "20260709", "strike": 745.0,
                "right": "C", "quantity": 5,
            },
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_close_option_short_cover_allowed(self):
        gateway = FakeGateway(positions=[
            {
                "symbol": "SPY", "quantity": -2, "sec_type": "OPT",
                "strike": 745.0, "right": "C", "expiration": "20260709",
            },
        ])
        proposal = validate_proposal(
            "close_option",
            {
                "symbol": "SPY", "expiration": "20260709", "strike": 745.0,
                "right": "C", "quantity": 1,
            },
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True


class TestCancelOrderLastStopGuard:
    """Reject cancelling the only working stop on an open stock position."""

    @pytest.mark.asyncio
    async def test_last_stop_cancel_rejected(self):
        gateway = FakeGateway(
            positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 103, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "STP",
                },
            ],
        )
        proposal = validate_proposal("cancel_order", {"order_id": 103}, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert "only working stop" in result["error"].lower() or "replacement" in result["error"].lower()
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_redundant_stop_cancel_allowed(self):
        gateway = FakeGateway(
            positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 103, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "STP",
                },
                {
                    "order_id": 104, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "TRAIL",
                },
            ],
        )
        proposal = validate_proposal("cancel_order", {"order_id": 103}, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_non_stop_cancel_allowed(self):
        gateway = FakeGateway(
            positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 103, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "LMT",
                },
                {
                    "order_id": 200, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "STP",
                },
            ],
        )
        proposal = validate_proposal("cancel_order", {"order_id": 103}, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_stop_on_flat_symbol_allowed(self):
        gateway = FakeGateway(
            positions=[],  # flat
            open_orders=[
                {
                    "order_id": 103, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "STP",
                },
            ],
        )
        proposal = validate_proposal("cancel_order", {"order_id": 103}, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_fail_closed_when_orders_unreadable(self):
        class BrokenOrders(FakeGateway):
            async def get_open_orders(self):
                raise RuntimeError("tws down")

        gateway = BrokenOrders(
            positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
        )
        proposal = validate_proposal("cancel_order", {"order_id": 103}, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert "fail-closed" in result["error"].lower()
        assert gateway.calls == []


class TestProtectionRequiresPosition:
    """oca needs a live open position."""

    @pytest.mark.asyncio
    async def test_oca_without_position_blocked(self):
        gateway = FakeGateway(positions=[])
        proposal = validate_proposal("oca", VALID_PAYLOADS["oca"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert "error" in result
        assert "protection order rejected" in result["error"].lower()
        assert "NVDA" in result["error"]
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_oca_with_position_dispatches(self):
        gateway = FakeGateway(
            positions=[{"symbol": "NVDA", "quantity": 10, "sec_type": "STK"}]
        )
        proposal = validate_proposal("oca", VALID_PAYLOADS["oca"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert gateway.calls[0][0] == "place_oca"

    @pytest.mark.asyncio
    async def test_modify_cancel_unaffected(self):
        """modify_* / cancel_order stay unrestricted by the protection check."""
        gateway = FakeGateway(positions=[])
        for strategy in ("modify_stop", "modify_target"):
            proposal = validate_proposal(strategy, VALID_PAYLOADS[strategy], RATIONALE)
            result = await execute_proposal(proposal, gateway)
            assert result["success"] is True, strategy
        # cancel of non-stop / flat symbol still ok
        proposal = validate_proposal("cancel_order", {"order_id": 103}, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True


def _cancel_ids(gateway):
    return [kw["order_id"] for name, kw in gateway.calls if name == "cancel_order"]


class TestStackedProtectiveExits:
    """Replace-on-place: a new protective exit becomes the protection."""

    @pytest.mark.asyncio
    async def test_trailing_stop_rejected_when_lot_covered(self):
        gateway = FakeGateway(
            positions=[{"symbol": "SPY", "quantity": 100, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 200, "symbol": "SPY", "sec_type": "STK",
                    "action": "SELL", "quantity": 100, "order_type": "STP",
                },
                {
                    "order_id": 201, "symbol": "SPY", "sec_type": "STK",
                    "action": "SELL", "quantity": 100, "order_type": "LMT",
                },
            ],
        )
        proposal = validate_proposal(
            "trailing_stop", VALID_PAYLOADS["trailing_stop"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert result["order_id"] == 1234
        assert gateway.calls[0][0] == "place_trailing_stop"
        assert _cancel_ids(gateway) == [200]
        assert result.get("replaced_ids") == [200]
        assert 1234 not in _cancel_ids(gateway)
        assert 201 not in _cancel_ids(gateway)

    @pytest.mark.asyncio
    async def test_trailing_stop_allowed_when_unprotected(self):
        gateway = FakeGateway(
            positions=[{"symbol": "SPY", "quantity": 100, "sec_type": "STK"}],
            open_orders=[],
        )
        proposal = validate_proposal(
            "trailing_stop", VALID_PAYLOADS["trailing_stop"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert gateway.calls[0][0] == "place_trailing_stop"
        assert _cancel_ids(gateway) == []
        assert result.get("replaced_ids") == []
        assert "replace_skipped" not in result


    @pytest.mark.asyncio
    async def test_replace_skipped_when_place_has_no_order_id(self):
        gateway = FakeGateway(
            positions=[{"symbol": "SPY", "quantity": 100, "sec_type": "STK"}],
            open_orders=[],
        )

        async def _place(**kwargs):
            gateway.calls.append(("place_trailing_stop", kwargs))
            return {"success": True}

        gateway.place_trailing_stop = _place
        proposal = validate_proposal(
            "trailing_stop", VALID_PAYLOADS["trailing_stop"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert result.get("replace_skipped") == "no_order_id"

    @pytest.mark.asyncio
    async def test_stop_order_rejected_when_covered(self):
        gateway = FakeGateway(
            positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 9, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "TRAIL",
                },
            ],
        )
        proposal = validate_proposal(
            "stop_order", VALID_PAYLOADS["stop_order"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert result["order_id"] == 1234
        assert gateway.calls[0][0] == "place_stop_order"
        assert _cancel_ids(gateway) == [9]
        assert 1234 not in _cancel_ids(gateway)

    @pytest.mark.asyncio
    async def test_oca_rejected_when_covered(self):
        gateway = FakeGateway(
            positions=[{"symbol": "NVDA", "quantity": 10, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 9, "symbol": "NVDA", "sec_type": "STK",
                    "action": "SELL", "quantity": 10, "order_type": "STP",
                },
            ],
        )
        proposal = validate_proposal("oca", VALID_PAYLOADS["oca"], RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert result["order_id"] == 1234
        assert gateway.calls[0][0] == "place_oca"
        assert _cancel_ids(gateway) == [9]
        assert 1234 not in _cancel_ids(gateway)

    @pytest.mark.asyncio
    async def test_failed_place_does_not_cancel_old_stops(self):
        gateway = FakeGateway(
            positions=[{"symbol": "SPY", "quantity": 100, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 200, "symbol": "SPY", "sec_type": "STK",
                    "action": "SELL", "quantity": 100, "order_type": "STP",
                },
            ],
        )

        async def _fail(**kwargs):
            gateway.calls.append(("place_trailing_stop", kwargs))
            return {"success": False, "error": "ibkr reject"}

        gateway.place_trailing_stop = _fail
        proposal = validate_proposal(
            "trailing_stop", VALID_PAYLOADS["trailing_stop"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result.get("success") is False
        assert "ibkr reject" in str(result.get("error") or "")
        assert _cancel_ids(gateway) == []

    @pytest.mark.asyncio
    async def test_new_trail_cancels_scale_stops(self):
        gateway = FakeGateway(
            positions=[{"symbol": "SPY", "quantity": 50, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 11, "symbol": "SPY", "sec_type": "STK",
                    "action": "SELL", "quantity": 25, "order_type": "TRAIL",
                },
                {
                    "order_id": 12, "symbol": "SPY", "sec_type": "STK",
                    "action": "SELL", "quantity": 25, "order_type": "TRAIL",
                },
            ],
        )
        proposal = validate_proposal(
            "trailing_stop", VALID_PAYLOADS["trailing_stop"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert sorted(_cancel_ids(gateway)) == [11, 12]
        assert 1234 not in _cancel_ids(gateway)

    @pytest.mark.asyncio
    async def test_undercovered_stop_still_allowed(self):
        gateway = FakeGateway(
            positions=[{"symbol": "AAPL", "quantity": 50, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 9, "symbol": "AAPL", "sec_type": "STK",
                    "action": "SELL", "quantity": 20, "order_type": "STP",
                },
            ],
        )
        proposal = validate_proposal(
            "stop_order", VALID_PAYLOADS["stop_order"], RATIONALE
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert _cancel_ids(gateway) == [9]
        assert 1234 not in _cancel_ids(gateway)

    @pytest.mark.asyncio
    async def test_collapse_csco_keeps_newest_covering(self):
        from abcxauto.executor import collapse_stacked_protective_exits

        types = ("STP", "TRAIL")
        orders = [
            {
                "order_id": 101 + i, "symbol": "CSCO", "sec_type": "STK",
                "action": "SELL", "quantity": 50, "order_type": types[i % 2],
            }
            for i in range(7)
        ]
        gateway = FakeGateway(
            positions=[{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}],
            open_orders=orders,
        )
        cancelled = await collapse_stacked_protective_exits(
            gateway, gateway.positions, gateway.open_orders
        )
        assert sorted(cancelled) == list(range(101, 107))
        cancel_ids = [
            kw["order_id"] for name, kw in gateway.calls if name == "cancel_order"
        ]
        assert sorted(cancel_ids) == list(range(101, 107))
        assert 107 not in cancel_ids
        assert not any(name != "cancel_order" for name, _kw in gateway.calls)

    @pytest.mark.asyncio
    async def test_collapse_does_not_cancel_last_stop(self):
        from abcxauto.executor import collapse_stacked_protective_exits

        gateway = FakeGateway(
            positions=[{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}],
            open_orders=[
                {
                    "order_id": 107, "symbol": "CSCO", "sec_type": "STK",
                    "action": "SELL", "quantity": 50, "order_type": "STP",
                },
            ],
        )
        cancelled = await collapse_stacked_protective_exits(
            gateway, gateway.positions, gateway.open_orders
        )
        assert cancelled == []
        assert gateway.calls == []
