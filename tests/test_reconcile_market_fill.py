"""Market-fill reconcile is execution-only: not qty-blind, wrong-side, or last."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.orders import IBKROrdersMixin, _action_matches, _oid_matches


class _Status:
    def __init__(self, status="Submitted", filled=0, avgFillPrice=0.0):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avgFillPrice


class _Order:
    def __init__(self, order_id=0, perm_id=0, action="BUY"):
        self.orderId = order_id
        self.permId = perm_id
        self.action = action


class _Trade:
    def __init__(
        self,
        *,
        order_id=0,
        perm_id=0,
        action="BUY",
        symbol="NVDA",
        status="Filled",
        filled=10,
        avg=120.5,
    ):
        self.order = _Order(order_id, perm_id, action)
        self.orderStatus = _Status(status, filled, avg)
        self.contract = SimpleNamespace(symbol=symbol)


class _Exec:
    def __init__(self, *, order_id=0, perm_id=0, side="BOT", shares=10, price=120.5, symbol="NVDA"):
        self.orderId = order_id
        self.permId = perm_id
        self.side = side
        self.shares = shares
        self.price = price
        self.avgPrice = price
        self.symbol = symbol


class _Fill:
    def __init__(self, ex, symbol="NVDA"):
        self.execution = ex
        self.contract = SimpleNamespace(symbol=symbol)


class _Harness(IBKROrdersMixin):
    def __init__(self, *, trades=None, fills=None, open_trades=None, positions=None):
        self._trades = list(trades or [])
        self._fills = list(fills or [])
        self._open = list(open_trades or [])
        self._positions = list(positions or [])
        self.ib = SimpleNamespace(
            trades=lambda: list(self._trades),
            fills=lambda: list(self._fills),
            openTrades=lambda: list(self._open),
        )

    async def get_positions(self):
        return list(self._positions)


def test_action_matches_bot_sld():
    assert _action_matches("BOT", "BUY")
    assert _action_matches("SELL", "SELL")
    assert not _action_matches("SLD", "BUY")
    assert not _action_matches("", "BUY")


@pytest.mark.asyncio
async def test_existing_lot_is_not_this_order_fill():
    """Qty-blind + last-as-fill: a same-side position is not this market order."""
    h = _Harness(
        positions=[
            {
                "symbol": "NVDA",
                "quantity": 40,
                "avg_cost": 0,
                "market_price": 181.25,
            }
        ]
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=4278
    )
    assert out["filled"] is False
    assert out["filled_quantity"] == 0
    assert out["avg_fill_price"] is None
    assert out["reconciled"] is False


@pytest.mark.asyncio
async def test_wrong_side_exec_is_not_a_buy_fill():
    h = _Harness(
        fills=[
            _Fill(_Exec(order_id=4278, side="SLD", shares=10, price=181.0), "NVDA")
        ]
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=4278
    )
    assert out["filled"] is False


@pytest.mark.asyncio
async def test_other_symbol_fill_is_ignored():
    h = _Harness(
        trades=[
            _Trade(order_id=1, action="BUY", symbol="AAPL", filled=10, avg=200.0)
        ],
        fills=[_Fill(_Exec(order_id=4278, side="BOT", shares=10, price=50.0), "AAPL")],
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=4278
    )
    assert out["filled"] is False


@pytest.mark.asyncio
async def test_status_filled_does_not_invent_ticket_qty():
    """Qty-blind: Filled + 0 shares must not become the requested quantity."""
    h = _Harness(
        trades=[
            _Trade(
                order_id=4278,
                action="BUY",
                symbol="NVDA",
                status="Filled",
                filled=0,
                avg=0,
            )
        ]
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=4278
    )
    assert out["filled"] is False
    assert out["filled_quantity"] == 0


@pytest.mark.asyncio
async def test_missing_order_id_does_not_take_the_first_print():
    h = _Harness(
        trades=[_Trade(order_id=99, action="SELL", symbol="TSLA", filled=50, avg=9.0)]
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=None
    )
    assert out["filled"] is False


@pytest.mark.asyncio
async def test_matching_trade_is_the_fill():
    h = _Harness(
        trades=[
            _Trade(
                order_id=4278,
                action="BUY",
                symbol="NVDA",
                filled=7,
                avg=120.5,
            )
        ]
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=4278
    )
    assert out["filled"] is True
    assert out["filled_quantity"] == 7
    assert out["avg_fill_price"] == 120.5
    assert out["reconciled"] is True


@pytest.mark.asyncio
async def test_matching_exec_print_is_the_fill():
    h = _Harness(
        fills=[
            _Fill(_Exec(order_id=4278, side="BOT", shares=3, price=10.0), "NVDA"),
            _Fill(_Exec(order_id=4278, side="BOT", shares=7, price=11.0), "NVDA"),
        ]
    )
    out = await h._reconcile_market_fill(
        symbol="NVDA", action="BUY", quantity=10, order_id=4278
    )
    assert out["filled"] is True
    assert out["filled_quantity"] == 10
    assert out["avg_fill_price"] == 10.7


@pytest.mark.asyncio
async def test_perm_id_finds_the_trade():
    trade = _Trade(order_id=0, perm_id=1503, action="SELL", symbol="CSCO", filled=50, avg=68.2)
    h = _Harness(open_trades=[trade], trades=[trade])
    found = h._find_trade_by_order_id(1503)
    assert found is trade
    out = await h._reconcile_market_fill(
        symbol="CSCO", action="SELL", quantity=50, order_id=1503
    )
    assert out["filled"] is True
    assert out["filled_quantity"] == 50
    assert out["avg_fill_price"] == 68.2


@pytest.mark.asyncio
async def test_market_bracket_refuses_qty_blind_oca():
    class _Bracket(_Harness):
        async def place_market_order(self, symbol, action, quantity, **_k):
            return {
                "success": True,
                "filled": True,
                "order_id": 1,
                "filled_quantity": 0,
                "avg_fill_price": None,
                "symbol": symbol,
                "action": action,
            }

        async def place_oca(self, *_a, **_k):
            raise AssertionError("must not size OCA from the ticket qty")

        async def cancel_order(self, *_a, **_k):
            return {"success": True}

    h = _Bracket()
    out = await h.place_market_bracket("NVDA", 10, "LONG", 170.0, 190.0)
    assert out.get("success") is False
    assert out.get("filled") is False
    assert "quantity" in (out.get("reason") or "").lower()


def test_oid_still_matches_perm():
    assert _oid_matches(SimpleNamespace(orderId=0, permId=1503), 1503)
