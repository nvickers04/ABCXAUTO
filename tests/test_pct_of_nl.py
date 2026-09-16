"""Canonical % of NetLiq helper — None on unknown or zero book."""

from __future__ import annotations

import math

import pytest

from abcxauto.world_state import pct_of_nl


@pytest.mark.parametrize(
    ("usd", "nl", "want"),
    [
        (50, 1000, 5.0),
        (0, 1000, 0.0),
        (-25, 1000, -2.5),
        ("50", "1000", 5.0),
        (1, 3, 33.3333),
    ],
)
def test_pct_of_nl_known_book(usd, nl, want):
    assert pct_of_nl(usd, nl) == want


@pytest.mark.parametrize(
    ("usd", "nl"),
    [
        (None, 1000),
        (50, None),
        (None, None),
        ("", 1000),
        (50, ""),
        ("x", 1000),
        (50, "x"),
        (50, 0),
        (50, 0.0),
        (50, "0"),
        (0, 0),
    ],
)
def test_pct_of_nl_null_or_zero_book_is_none(usd, nl):
    assert pct_of_nl(usd, nl) is None


def test_pct_of_nl_nan_nl_is_none():
    assert pct_of_nl(50, math.nan) is None


def test_pct_of_nl_digits_for_display():
    assert pct_of_nl(50, 1000, digits=2) == 5.0
    assert pct_of_nl(1, 3, digits=2) == 33.33


def test_risk_gates_helper_still_disagrees_on_zero_book():
    """#207 finished the migration: gates use shared pct_of_nl (None on empty book)."""
    import abcxauto.risk_gates as gates

    assert not hasattr(gates, "_pct_of_nl")
    assert pct_of_nl(50.0, 0.0) is None
