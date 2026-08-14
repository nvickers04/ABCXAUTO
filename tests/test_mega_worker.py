"""Capacity Fact — leftover mega_worker stream labels are gone."""

from types import SimpleNamespace

from abcxauto.world_state import capacity_allows_new_risk


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
