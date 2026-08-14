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
