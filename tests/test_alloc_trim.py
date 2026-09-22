"""alloc_trim: excess-only SELL limits; no market; no stop cancel; tickets only."""

from __future__ import annotations

from abcxauto.alloc_trim import trim_tickets


def test_avgo_excess_sell_at_bid() -> None:
    sized = {
        "AVGO": {
            "held": 89,
            "sized": 22,
            "excess": 67,
            "last": 363.2,
            "stop": None,
        }
    }
    tickets = trim_tickets(sized, bids={"AVGO": 363.2})
    assert tickets == [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "quantity": 67,
            "limit_price": 363.2,
            "reason": "alloc_excess",
        }
    ]


def test_sized_none_skips_bad_panel() -> None:
    sized = {"AVGO": None}
    assert trim_tickets(sized, bids={"AVGO": 363.2}) == []

    sized_field_none = {
        "AVGO": {
            "held": 89,
            "sized": None,
            "excess": 67,
            "last": 363.2,
            "stop": None,
        }
    }
    assert trim_tickets(sized_field_none, bids={"AVGO": 363.2}) == []


def test_vs_spy_sized_zero_sells_full_excess() -> None:
    """vs_spy already lands as sized=0; excess 89 sells 89 only."""
    sized = {
        "AVGO": {
            "held": 89,
            "sized": 0,
            "excess": 89,
            "last": 363.2,
            "stop": None,
        }
    }
    tickets = trim_tickets(sized, bids={"AVGO": 363.2})
    assert len(tickets) == 1
    assert tickets[0]["quantity"] == 89
    assert tickets[0]["action"] == "SELL"
    assert tickets[0]["limit_price"] == 363.2
    assert tickets[0]["reason"] == "alloc_excess"


def test_excess_without_bid_skipped() -> None:
    sized = {
        "AVGO": {
            "held": 89,
            "sized": 22,
            "excess": 67,
            "last": 363.2,
            "stop": None,
        }
    }
    assert trim_tickets(sized, bids={}) == []
    assert trim_tickets(sized, bids={"AVGO": 0.0}) == []
