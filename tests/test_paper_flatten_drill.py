"""Guards for the paper Flatten All drill — no TWS, no live path."""

from __future__ import annotations

import pytest

from scripts.paper_flatten_drill import (
    DrillRefused,
    book_must_be_empty,
    flatten_matches_book,
    paper_7497_or_refuse,
)


def test_paper_7497_is_the_only_ok_socket():
    paper_7497_or_refuse("paper", 7497)
    paper_7497_or_refuse("PAPER", "7497")


@pytest.mark.parametrize(
    "mode,port",
    [
        ("live", 7496),
        ("live", 7497),
        ("paper", 7496),
        ("paper", 4001),
        ("paper", 4002),
        ("paper", 0),
    ],
)
def test_live_or_non_7497_is_refused(mode, port):
    with pytest.raises(DrillRefused, match="REFUSED"):
        paper_7497_or_refuse(mode, port)


def test_dirty_book_is_refused():
    with pytest.raises(DrillRefused, match="not empty"):
        book_must_be_empty([{"symbol": "SPY", "quantity": 1}], [])
    with pytest.raises(DrillRefused, match="not empty"):
        book_must_be_empty([], [{"order_id": 1, "symbol": "SPY"}])
    book_must_be_empty([], [])
    book_must_be_empty([{"symbol": "SPY", "quantity": 0}], [])


def test_flatten_success_must_match_reread():
    lot = {"symbol": "SPY", "quantity": 1, "conId": 7, "sec_type": "STK"}
    lie = {
        "success": True,
        "remaining": [],
        "failed": [],
        "position_results": [{"method": "stock_mkt", "status": "closed"}],
        "positions_total": 1,
    }
    problems = flatten_matches_book(lie, [lot])
    assert any("success=True" in p for p in problems)

    honest_flat = {
        "success": True,
        "remaining": [],
        "failed": [],
        "position_results": [{"method": "stock_mkt", "status": "closed"}],
        "positions_total": 1,
    }
    assert flatten_matches_book(honest_flat, []) == []

    leftover = {
        "success": False,
        "remaining": [lot],
        "failed": [
            {
                "symbol": "SPY",
                "conId": 7,
                "sec_type": "STK",
                "protection": "last_stop",
            }
        ],
        "position_results": [{"method": "stock_mkt", "status": "failed"}],
        "positions_total": 1,
    }
    assert flatten_matches_book(leftover, [lot]) == []
