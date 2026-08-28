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


def test_invented_reserved_slots_does_not_change_bag_capacity():
    """reserved_slots is not an IBKR/ticket field — legs decide the charge."""
    four_legs = {
        "symbol": "IWM",
        "sec_type": "BAG",
        "action": "BUY",
        "order_type": "LMT",
        "quantity": 1,
        "combo_legs": [{}, {}, {}, {}],
        "reserved_slots": 1,
    }
    assert working_entry_slots([four_legs], []) == 4
    one_leg = dict(four_legs, combo_legs=[{}], reserved_slots=8)
    assert working_entry_slots([one_leg], []) == 1


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
    gates_on = SimpleNamespace(
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        risk_gates_enabled=True,
        max_open_positions=6,
    )
    assert capacity_allows_new_risk(open_ok, gates_on) is True
    assert capacity_allows_new_risk(full, gates_on) is False


def test_mop_zero_does_not_refuse_on_count():
    """mop 0 = off. 16 names are not a slot refuse on paper or live."""
    lots = [{"symbol": f"S{i}", "quantity": 1} for i in range(16)]
    leftover = SimpleNamespace(
        capacity={
            "open_count": 16,
            "max_open_positions": 0,
            "slots_left": None,
            "allows_new_risk": True,
        },
        positions=lots,
        open_orders=[],
    )
    paper = SimpleNamespace(
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        risk_gates_enabled=True,
        max_open_positions=0,
    )
    live = SimpleNamespace(
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        risk_gates_enabled=True,
        max_open_positions=0,
    )
    assert capacity_allows_new_risk(leftover, paper) is True
    assert capacity_allows_new_risk(leftover, live) is True


def test_grok_set_mop_four_refuses_the_fifth():
    """A Grok-set mop=4 is a ceiling. The 5th name dies on paper and live."""
    lots = [{"symbol": f"S{i}", "quantity": 1} for i in range(4)]
    full = SimpleNamespace(
        capacity={
            "open_count": 4,
            "max_open_positions": 4,
            "slots_left": 0,
            "allows_new_risk": False,
        },
        positions=lots,
        open_orders=[],
    )
    paper = SimpleNamespace(
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        risk_gates_enabled=True,
        max_open_positions=4,
    )
    live = SimpleNamespace(
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        risk_gates_enabled=True,
        max_open_positions=4,
    )
    assert capacity_allows_new_risk(full, paper) is False
    assert capacity_allows_new_risk(full, live) is False


def test_capacity_fact_paints_open_and_nl_without_refusing_when_unarmed():
    from abcxauto.trade_plan import capacity_fact

    lots = [{"symbol": "X", "quantity": 1} for _ in range(15)]
    cap = capacity_fact(
        lots,
        max_open_positions=12,
        net_liq=1_000.0,
        cap_armed=False,
    )
    assert cap["open_count"] == 15
    assert cap["nl"] == 1_000.0
    assert cap["max_open_positions"] == 12
    assert cap["allows_new_risk"] is True
    assert cap["cap_armed"] is False
    assert cap["with_size"] == "size_pct_nl"
    armed = capacity_fact(
        lots,
        max_open_positions=12,
        net_liq=35_000.0,
        cap_armed=True,
    )
    assert armed["allows_new_risk"] is False
    assert armed["cap_armed"] is True
    assert armed["nl"] == 35_000.0


def test_working_entries_still_reserve_when_cap_armed():
    from abcxauto.trade_plan import capacity_fact

    orders = [
        {"symbol": "QQQ", "sec_type": "STK", "action": "BUY", "order_type": "LMT", "quantity": 10},
    ]
    cap = capacity_fact(
        [],
        max_open_positions=1,
        open_orders=orders,
        net_liq=35_000.0,
        cap_armed=True,
    )
    assert cap["pending_entries"] == 1
    assert cap["allows_new_risk"] is False
    free = capacity_fact(
        [],
        max_open_positions=1,
        open_orders=orders,
        net_liq=1_000.0,
        cap_armed=False,
    )
    assert free["pending_entries"] == 1
    assert free["allows_new_risk"] is True
