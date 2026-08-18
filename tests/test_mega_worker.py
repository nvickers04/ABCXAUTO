"""Capacity Fact — leftover mega_worker stream labels are gone."""

from types import SimpleNamespace

from abcxauto.trade_plan import capacity_fact, working_entry_slots
from abcxauto.world_state import capacity_allows_new_risk


def test_working_entries_reserve_capacity():
    orders = [
        {"symbol": "QQQ", "sec_type": "STK", "action": "BUY", "order_type": "LMT", "quantity": 10},
        {"symbol": "QQQ", "sec_type": "STK", "action": "SELL", "order_type": "STP", "quantity": 10},
        {"symbol": "QQQ", "sec_type": "STK", "action": "SELL", "order_type": "LMT", "quantity": 10},
        {
            "symbol": "IWM",
            "sec_type": "BAG",
            "action": "BUY",
            "order_type": "LMT",
            "quantity": 1,
            "combo_legs": [{}, {}],
        },
    ]
    assert working_entry_slots(orders, []) == 3
    cap = capacity_fact([], max_open_positions=4, open_orders=orders)
    assert cap["open_count"] == 0
    assert cap["pending_entries"] == 3
    assert cap["slots_left"] == 1
    assert cap["allows_new_risk"] is True
    full = capacity_fact([], max_open_positions=3, open_orders=orders)
    assert full["allows_new_risk"] is False
    held = capacity_fact(
        [{"symbol": "QQQ", "secType": "STK", "quantity": 10}],
        max_open_positions=4,
        open_orders=orders,
    )
    assert held["open_count"] == 1
    assert held["pending_entries"] == 2
    assert held["allows_new_risk"] is True


def test_capacity_allows_and_blocks():
    open_ok = SimpleNamespace(
        capacity={
            "open_count": 1,
            "max_open_positions": 6,
            "slots_left": 5,
            "allows_new_risk": True,
        },
        positions=[{"symbol": "SPY", "quantity": 8}],
    )
    full = SimpleNamespace(
        capacity={
            "open_count": 6,
            "max_open_positions": 6,
            "slots_left": 0,
            "allows_new_risk": False,
        },
        positions=[],
    )
    assert capacity_allows_new_risk(open_ok) is True
    assert capacity_allows_new_risk(full) is False
