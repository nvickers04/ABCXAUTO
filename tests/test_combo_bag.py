"""Multi-leg send is one IBKR BAG, not two naked singles."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.options import IBKROptionsMixin


class _Opt:
    def __init__(self, strike, right):
        self.conId = int(strike)
        self.strike = strike
        self.right = right
        self.lastTradeDateOrContractMonth = "20260828"


@pytest.mark.asyncio
async def test_place_combo_uses_bag_not_singles():
    placed: list = []

    class FakeIB:
        def placeOrder(self, contract, order):
            placed.append((contract, order))
            return SimpleNamespace(
                order=SimpleNamespace(orderId=9),
                orderStatus=SimpleNamespace(status="Submitted"),
                log=[],
            )

    mix = IBKROptionsMixin()
    mix.ib = FakeIB()

    async def connected():
        return True

    async def create_opts(_symbol, _expiration, pairs):
        return [_Opt(k, r) for k, r in pairs]

    async def no_reject(_trade, _name, _symbol):
        return None

    mix._ensure_connected = connected
    mix._create_options = create_opts
    mix._check_order_rejection = no_reject

    out = await mix._place_combo_order(
        "SPY",
        "20260828",
        [(780.0, "C", "BUY", 1), (782.0, "C", "SELL", 1)],
        1,
        "BUY",
        1.07,
        "Bull Call Spread",
    )
    assert out.get("success") is True
    assert out.get("note") == "IBKR combo (BAG)"
    assert placed[0][0].secType == "BAG"
    assert placed[0][1].action == "BUY"
    assert placed[0][1].lmtPrice == 1.07
    assert len(placed[0][0].comboLegs) == 2


@pytest.mark.asyncio
async def test_place_vertical_spread_close_sells_debit_combo():
    seen: dict = {}

    class Mix(IBKROptionsMixin):
        pass

    mix = Mix()

    async def connected():
        return True

    async def no_riskless(*_a, **_k):
        return None

    async def capture(*args, **kwargs):
        seen["combo_action"] = args[4] if len(args) > 4 else kwargs.get("combo_action")
        seen["limit_price"] = args[5] if len(args) > 5 else kwargs.get("limit_price")
        return {"success": True, "order_id": 4}

    mix._ensure_connected = connected
    mix._check_riskless_spread = no_riskless
    mix._place_combo_order = capture
    out = await mix.place_vertical_spread(
        "JPM",
        "20260918",
        370.0,
        375.0,
        "C",
        1,
        "LMT",
        0.71,
        True,
    )
    assert out.get("success") is True
    assert seen["combo_action"] == "SELL"
    assert seen["limit_price"] == 0.71


@pytest.mark.asyncio
async def test_place_vertical_spread_close_requires_limit():
    mix = IBKROptionsMixin()

    async def connected():
        return True

    async def no_riskless(*_a, **_k):
        return None

    mix._ensure_connected = connected
    mix._check_riskless_spread = no_riskless
    out = await mix.place_vertical_spread(
        "JPM", "20260918", 370.0, 375.0, "C", 1, "LMT", None, True
    )
    assert "error" in out
    assert "limit_price" in out["error"]


@pytest.mark.asyncio
async def test_singles_rollback_if_short_leg_rejects():
    cancelled: list = []

    class FakeIB:
        def openTrades(self):
            return [
                SimpleNamespace(order=SimpleNamespace(orderId=368)),
            ]

        def cancelOrder(self, order):
            cancelled.append(order.orderId)

    mix = IBKROptionsMixin()
    mix.ib = FakeIB()
    calls = {"n": 0}

    async def one_ok(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": True, "order_id": 368}
        return {"error": "trading permissions for the options strategy"}

    mix._place_single_option = one_ok
    out = await mix._place_combo_as_singles(
        "SPY",
        "20260828",
        [(780.0, "C", "BUY", 1), (782.0, "C", "SELL", 1)],
        1,
        "BUY",
        1.07,
        "Bull Call Spread",
    )
    assert out.get("success") is not True
    assert "incomplete" in str(out.get("error") or "")
    assert 368 in (out.get("cancelled_order_ids") or [])
    assert cancelled == [368]


@pytest.mark.asyncio
async def test_place_iron_condor_close_buys_combo():
    seen: dict = {}
    mix = IBKROptionsMixin()

    async def capture(*args, **kwargs):
        seen["combo_action"] = args[4] if len(args) > 4 else kwargs.get("combo_action")
        seen["limit_price"] = args[5] if len(args) > 5 else kwargs.get("limit_price")
        return {"success": True, "order_id": 5}

    mix._place_combo_order = capture
    out = await mix.place_iron_condor(
        "SPY", "20260918", 480.0, 490.0, 510.0, 520.0, 1, 1.25, True
    )
    assert out.get("success") is True
    assert seen["combo_action"] == "BUY"
    assert seen["limit_price"] == 1.25


@pytest.mark.asyncio
async def test_place_straddle_close_flips_parent_keeps_legs():
    seen: dict = {}
    mix = IBKROptionsMixin()

    async def capture(*args, **kwargs):
        seen["legs"] = args[2] if len(args) > 2 else kwargs.get("strikes_rights_actions")
        seen["combo_action"] = args[4] if len(args) > 4 else kwargs.get("combo_action")
        return {"success": True, "order_id": 6}

    mix._place_combo_order = capture
    out = await mix.place_straddle("SPY", "20260918", 500.0, 1, "BUY", 8.0, True)
    assert out.get("success") is True
    assert seen["combo_action"] == "SELL"
    assert seen["legs"][0][2] == "BUY"
    assert seen["legs"][1][2] == "BUY"


@pytest.mark.asyncio
async def test_place_calendar_spread_uses_bag():
    placed: list = []

    class FakeIB:
        def placeOrder(self, contract, order):
            placed.append((contract, order))
            return SimpleNamespace(
                order=SimpleNamespace(orderId=9),
                orderStatus=SimpleNamespace(status="Submitted"),
                log=[],
            )

    mix = IBKROptionsMixin()
    mix.ib = FakeIB()

    async def connected():
        return True

    async def create_opts(_symbol, _expiration, pairs):
        return [_Opt(k, r) for k, r in pairs]

    async def no_reject(_trade, _name, _symbol):
        return None

    mix._ensure_connected = connected
    mix._create_options = create_opts
    mix._check_order_rejection = no_reject
    out = await mix.place_calendar_spread(
        "SPY", 500.0, "20260718", "20260815", "C", 1, 1.10
    )
    assert out.get("success") is True
    assert out.get("note") == "IBKR combo (BAG)"
    assert placed[0][0].secType == "BAG"
    assert placed[0][1].action == "BUY"
    assert placed[0][1].lmtPrice == 1.10
    assert len(placed[0][0].comboLegs) == 2


@pytest.mark.asyncio
async def test_place_calendar_spread_close_sells_combo():
    mix = IBKROptionsMixin()
    seen: dict = {}

    async def capture(symbol, legs, quantity, combo_action, limit_price, strategy_name):
        seen["combo_action"] = combo_action
        seen["limit_price"] = limit_price
        seen["legs"] = legs
        return {"success": True, "order_id": 8}

    mix._place_combo_bag = capture
    out = await mix.place_calendar_spread(
        "SPY", 500.0, "20260718", "20260815", "C", 1, 0.85, True
    )
    assert out.get("success") is True
    assert seen["combo_action"] == "SELL"
    assert seen["limit_price"] == 0.85


@pytest.mark.asyncio
async def test_place_iron_condor_close_requires_limit():
    mix = IBKROptionsMixin()
    out = await mix.place_iron_condor(
        "SPY", "20260918", 480.0, 490.0, 510.0, 520.0, 1, None, True
    )
    assert "error" in out
    assert "limit_price" in out["error"]
