"""Minimal tests for book/portfolio state builders."""

from __future__ import annotations

from abcxauto.book import build_book, portfolio_narrative
from abcxauto.portfolio import build_portfolio_state


def test_build_portfolio_state_core_fields(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"trading_mandate": "Trade SPY with brackets only. " * 20})(),
    )
    state = build_book(
        account={"netliquidation": 50_000, "dailypnl": -100},
        positions=[
            {
                "conId": 1,
                "symbol": "SPY",
                "secType": "STK",
                "quantity": 10,
                "avgCost": 500,
                "marketPrice": 505,
                "unrealizedPNL": 50,
            }
        ],
        open_orders=[{"orderId": 9}],
        protection={"unprotected_symbols": []},
    )
    assert "mandate_summary" in state
    assert len(state["mandate_summary"]) <= 240
    assert state["net_liq"] == 50_000
    assert state["daily_pnl"] == -100
    assert state["daily_pnl_pct"] is not None
    assert state["open_orders_count"] == 1
    assert state["unprotected_symbols"] == []
    assert len(state["positions"]) == 1
    assert state["positions"][0]["symbol"] == "SPY"
    assert "narrative" in state
    assert "NL=" in state["narrative"]
    # portfolio.py remains a thin re-export
    via_portfolio = build_portfolio_state(
        account={"netliquidation": 50_000, "dailypnl": -100},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
    )
    assert via_portfolio["net_liq"] == 50_000


def test_portfolio_narrative_flags_unprotected():
    state = {
        "net_liq": 1000,
        "daily_pnl": 5,
        "positions": [{"symbol": "AAPL"}],
        "open_orders_count": 0,
        "unprotected_symbols": ["AAPL"],
        "halt": False,
        "working_thesis": "",
    }
    line = portfolio_narrative(state)
    assert "UNPROTECTED:AAPL" in line
