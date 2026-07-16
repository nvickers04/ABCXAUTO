"""Smoke tests for abcxauto.book and abcxauto.risk product surfaces."""

from __future__ import annotations

from abcxauto import book, risk


def test_build_book_returns_net_liq(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"trading_mandate": "test mandate"})(),
    )
    state = book.build_book(
        account={"netliquidation": 25_000, "dailypnl": 10},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
    )
    assert isinstance(state, dict)
    assert state["net_liq"] == 25_000


def test_build_portfolio_state_alias(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"trading_mandate": ""})(),
    )
    via_book = book.build_book(account={"NetLiquidation": 10_000})
    via_alias = book.build_portfolio_state(account={"NetLiquidation": 10_000})
    assert via_book["net_liq"] == 10_000
    assert via_alias["net_liq"] == 10_000


def test_build_book_from_snap(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"trading_mandate": ""})(),
    )
    snap = {
        "account": {"netliquidation": 42_000},
        "positions": [],
        "protection": {"unprotected_symbols": ["SPY"]},
    }
    state = book.build_book_from_snap(snap)
    assert state["net_liq"] == 42_000
    assert state["unprotected_symbols"] == ["SPY"]


def test_risk_get_risk_gate():
    gate = risk.get_risk_gate()
    assert gate is not None
    assert hasattr(gate, "is_halted")
    reset = risk.reset_risk_gate()
    assert reset is not None
    assert risk.is_exit_or_management is not None
