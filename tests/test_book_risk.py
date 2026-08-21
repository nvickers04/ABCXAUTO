"""Smoke tests for the abcxauto.book product surface."""

from __future__ import annotations

from abcxauto import book


def test_build_book_returns_net_liq(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {})(),
    )
    state = book.build_book(
        account={"netliquidation": 25_000, "dailypnl": 10},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
    )
    assert isinstance(state, dict)
    assert state["net_liq"] == 25_000
    assert "portfolio_risk" in state
    assert "exposure" in state
    assert "capital_liquidity" in state
    assert state["capital_liquidity"]["cash_pct_nl"] == 0.0
    assert state["capital_liquidity"]["deployed_long_pct_nl"] == 0.0


def test_build_book_portfolio_risk_pct_nl(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {})(),
    )
    state = book.build_book(
        account={
            "netliquidation": 10_000,
            "dailypnl": 0,
            "totalcashvalue": 7_000,
        },
        positions=[
            {
                "symbol": "QQQ",
                "quantity": 10,
                "marketValue": 3_000,
                "secType": "STK",
            }
        ],
        open_orders=[],
        protection={"unprotected_symbols": []},
        include_narrative=False,
    )
    assert state["portfolio_risk"]["top_symbol"] == "QQQ"
    assert state["portfolio_risk"]["top_concentration_pct"] == 30.0
    assert state["exposure"]["symbols"][0]["pct_nl"] == 30.0
    assert state["capital_liquidity"]["cash_pct_nl"] == 70.0
    assert state["capital_liquidity"]["deployed_long_pct_nl"] == 30.0


def test_build_book_from_snap(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {})(),
    )
    snap = {
        "account": {"netliquidation": 42_000},
        "positions": [],
        "protection": {"unprotected_symbols": ["SPY"]},
    }
    state = book.build_book_from_snap(snap)
    assert state["net_liq"] == 42_000
    assert state["unprotected_symbols"] == ["SPY"]


def test_build_book_clerk_halt_vs_trip(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"daily_loss_limit_pct": 25.0})(),
    )
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": False, "halt_reason": "", "halt_kind": ""})(),
    )
    state = book.build_book(
        account={"netliquidation": 35_216, "dailypnl": -373.85},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
        include_narrative=False,
    )
    assert state["clerk_halted"] is False
    assert state["daily_loss_limit_pct"] == 25.0
    assert state["halt_trips_at_usd"] == -8804.0
    assert state["ibkr_day_vs_halt"] == 8430.15
