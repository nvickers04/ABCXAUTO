"""Min-tick rounding: fillable limits, BAG tick from legs — no IBKR."""

from __future__ import annotations

from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from abcxauto.broker.options import IBKROptionsMixin
from abcxauto.broker.orders import IBKROrdersMixin
from abcxauto.broker.ticks import round_to_min_tick


class _Opt:
    def __init__(self, strike, right):
        self.conId = int(strike * 10)
        self.strike = strike
        self.right = right
        self.lastTradeDateOrContractMonth = "20260925"


def _limit_mixin(tick: float):
    mixin = IBKROrdersMixin()
    mixin.ib = MagicMock()
    mixin._order_state_lock = Lock()

    async def _ok():
        return True

    mixin._ensure_connected = _ok
    mixin._prepare_contract = AsyncMock(return_value=SimpleNamespace(symbol="T"))
    mixin._contract_min_tick = AsyncMock(return_value=tick)
    mixin.get_open_orders = AsyncMock(return_value=[])
    mixin._check_order_rejection = AsyncMock(return_value=None)
    captured = []

    def _place(_c, order):
        captured.append(order)
        return SimpleNamespace(
            order=SimpleNamespace(orderId=7),
            orderStatus=SimpleNamespace(status="Submitted"),
        )

    mixin.ib.placeOrder = _place
    return mixin, captured


def test_buy_limit_ceils_off_grid():
    """BUY 100.123 @ 0.01 must go to 100.13 (through the ask), not 100.12."""
    assert round_to_min_tick(100.123, 0.01, action="BUY") == 100.13


def test_sell_limit_floors_off_grid():
    """SELL 1.234 @ 0.05 must go to 1.20 (through the bid), not 1.25."""
    assert round_to_min_tick(1.234, 0.05, action="SELL") == 1.20


def test_live_nok_xom_sell_limits_stay_on_fillable_side():
    """Paper 2026-09-16: NOK 0.215 and XOM 0.775 SELL were parked at 0.22 / 0.78."""
    assert round_to_min_tick(0.215, 0.01, action="SELL") == 0.21
    assert round_to_min_tick(0.775, 0.01, action="SELL") == 0.77
    assert round_to_min_tick(0.215, 0.01, action="SELL") != 0.22
    assert round_to_min_tick(0.775, 0.01, action="SELL") != 0.78


def test_on_grid_limits_stay_put():
    assert round_to_min_tick(0.22, 0.01, action="SELL") == 0.22
    assert round_to_min_tick(1.25, 0.05, action="BUY") == 1.25


def test_stop_prices_keep_nearest_tick():
    """Protective stops stay nearest — not BUY-ceil / SELL-floor."""
    assert round_to_min_tick(98.004, 0.01) == 98.00
    assert round_to_min_tick(98.006, 0.01) == 98.01
    assert round_to_min_tick(0.215, 0.01) == 0.22


@pytest.mark.asyncio
async def test_place_buy_limit_ceils_off_grid(monkeypatch):
    mixin, captured = _limit_mixin(0.01)
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())
    out = await mixin.place_limit_order("SPY", "BUY", 1, 100.123)
    assert out.get("success") is True
    assert captured[0].lmtPrice == 100.13


@pytest.mark.asyncio
async def test_place_sell_limit_floors_off_grid(monkeypatch):
    mixin, captured = _limit_mixin(0.05)
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())
    out = await mixin.place_limit_order("PENNY", "SELL", 1, 1.234)
    assert out.get("success") is True
    assert captured[0].lmtPrice == 1.20


@pytest.mark.asyncio
async def test_place_live_sell_limits_do_not_round_away_from_bid(monkeypatch):
    mixin, captured = _limit_mixin(0.01)
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())
    out = await mixin.place_limit_order("NOK", "SELL", 1, 0.215)
    assert out.get("success") is True
    assert captured[0].lmtPrice == 0.21
    captured.clear()
    out = await mixin.place_limit_order("XOM", "SELL", 1, 0.775)
    assert out.get("success") is True
    assert captured[0].lmtPrice == 0.77


@pytest.mark.asyncio
async def test_place_stop_keeps_nearest_aux(monkeypatch):
    mixin, captured = _limit_mixin(0.01)
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", AsyncMock())
    out = await mixin.place_stop_order("SPY", "SELL", 10, 98.004)
    assert out.get("success") is True
    assert captured[0].auxPrice == 98.00


@pytest.mark.asyncio
async def test_bag_min_tick_does_not_req_contract_details_on_bag():
    mixin = IBKROrdersMixin()
    calls = []

    async def fake_req(contract):
        calls.append(contract)
        sec = str(getattr(contract, "secType", "") or "").upper()
        if sec == "BAG":
            raise RuntimeError("Error 321: 'BAG' isn't supported for contract data request")
        cid = int(getattr(contract, "conId", 0) or 0)
        tick = 0.05 if cid == 222 else 0.01
        return [SimpleNamespace(minTick=tick)]

    mixin.ib = SimpleNamespace(reqContractDetailsAsync=fake_req)
    bag = SimpleNamespace(
        secType="BAG",
        comboLegs=[
            SimpleNamespace(conId=111),
            SimpleNamespace(conId=222),
        ],
    )
    tick = await mixin._contract_min_tick(bag)
    assert all(str(getattr(c, "secType", "") or "").upper() != "BAG" for c in calls)
    assert [getattr(c, "conId", None) for c in calls] == [111, 222]
    assert tick == 0.05


@pytest.mark.asyncio
async def test_bag_min_tick_fallback_warns(caplog):
    mixin = IBKROrdersMixin()
    calls = []

    async def fake_req(contract):
        calls.append(contract)
        return [SimpleNamespace(minTick=0.01)]

    mixin.ib = SimpleNamespace(reqContractDetailsAsync=fake_req)
    bag = SimpleNamespace(secType="BAG", comboLegs=[])
    import logging

    with caplog.at_level(logging.WARNING, logger="abcxauto.broker.orders"):
        tick = await mixin._contract_min_tick(bag)
    assert tick == 0.05
    assert calls == []
    assert "BAG" in caplog.text
    assert "0.05" in caplog.text


@pytest.mark.asyncio
async def test_combo_sell_live_prices_floor_on_bag_place():
    placed: list = []
    details_calls: list = []

    class Mix(IBKROrdersMixin, IBKROptionsMixin):
        pass

    class FakeIB:
        def placeOrder(self, contract, order):
            placed.append((contract, order))
            return SimpleNamespace(
                order=SimpleNamespace(orderId=9),
                orderStatus=SimpleNamespace(status="Submitted"),
                log=[],
            )

        async def reqContractDetailsAsync(self, contract):
            details_calls.append(contract)
            sec = str(getattr(contract, "secType", "") or "").upper()
            if sec == "BAG":
                raise RuntimeError("Error 321: 'BAG' isn't supported for contract data request")
            return [SimpleNamespace(minTick=0.01)]

    mix = Mix()
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

    out = await mix._place_combo_as_bag(
        "NOK",
        "20260925",
        [(10.0, "C", "BUY", 1), (10.5, "C", "SELL", 1)],
        1,
        "SELL",
        0.215,
        "Close vertical",
    )
    assert out.get("success") is True
    assert placed[0][0].secType == "BAG"
    assert placed[0][1].lmtPrice == 0.21
    assert all(str(getattr(c, "secType", "") or "").upper() != "BAG" for c in details_calls)

    placed.clear()
    details_calls.clear()
    out = await mix._place_combo_as_bag(
        "XOM",
        "20260925",
        [(170.0, "C", "BUY", 1), (175.0, "C", "SELL", 1)],
        1,
        "SELL",
        0.775,
        "Close vertical",
    )
    assert out.get("success") is True
    assert placed[0][1].lmtPrice == 0.77
    assert all(str(getattr(c, "secType", "") or "").upper() != "BAG" for c in details_calls)
