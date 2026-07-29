"""Pure mapping tests for Pro API (no TWS required)."""

from abcxauto.pro_api import map_account, map_orders, map_positions


def test_map_positions_stop_protected():
    pos = [
        {
            "symbol": "NVDA",
            "quantity": 60,
            "avg_cost": 118.4,
            "market_price": 131.2,
            "unrealized_pnl": 769,
            "sec_type": "STK",
            "conId": 1,
        }
    ]
    orders = [
        {
            "order_id": 9,
            "symbol": "NVDA",
            "action": "SELL",
            "quantity": 60,
            "order_type": "STP",
            "aux_price": 122.5,
            "lmt_price": None,
            "status": "PreSubmitted",
        }
    ]
    out = map_positions(pos, orders)
    assert len(out) == 1
    assert out[0]["symbol"] == "NVDA"
    assert out[0]["protected"] is True
    assert "122.50" in out[0]["details"]


def test_map_positions_unprotected_without_stop():
    pos = [
        {
            "symbol": "META",
            "quantity": 10,
            "avg_cost": 400,
            "market_price": 410,
            "unrealized_pnl": 100,
            "sec_type": "STK",
            "conId": 2,
        }
    ]
    out = map_positions(pos, [])
    assert out[0]["protected"] is False
    assert "unprotected" in out[0]["details"]


def test_map_orders_roles():
    pos = [{"symbol": "AAPL", "quantity": 50}]
    orders = [
        {
            "order_id": 1,
            "symbol": "AAPL",
            "action": "SELL",
            "quantity": 50,
            "order_type": "STP",
            "aux_price": 190,
            "status": "Submitted",
        },
        {
            "order_id": 2,
            "symbol": "AAPL",
            "action": "SELL",
            "quantity": 50,
            "order_type": "LMT",
            "lmt_price": 220,
            "status": "Submitted",
        },
        {
            "order_id": 3,
            "symbol": "QQQ",
            "action": "BUY",
            "quantity": 15,
            "order_type": "LMT",
            "lmt_price": 478,
            "status": "Submitted",
        },
    ]
    out = map_orders(orders, pos)
    roles = {o["id"]: o["role"] for o in out}
    assert roles["1"] == "stop"
    assert roles["2"] == "target"
    assert roles["3"] == "entry"


def test_map_account():
    a = map_account(
        {"netliquidation": 250000.5, "dailypnl": -12.3, "account_id": "DU1"}
    )
    assert a["netLiq"] == 250000.5
    assert a["dayPnl"] == -12.3
    assert a["accountId"] == "DU1"
