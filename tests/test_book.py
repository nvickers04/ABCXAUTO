"""Minimal tests for the book state builders."""

from __future__ import annotations

from abcxauto.book import build_book, portfolio_narrative


def test_build_book_core_fields(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {})(),
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
    assert "mandate_summary" not in state
    assert state["net_liq"] == 50_000
    assert state["daily_pnl"] == -100
    assert state["ibkr_daily_pnl"] == -100
    assert state["open_upnl"] == 50.0
    assert state["daily_pnl_pct"] is not None
    assert "ibkrDay=-100" in state["narrative"]
    assert "openU=50.0" in state["narrative"]
    assert "dayPnL=" not in state["narrative"]
    assert state["open_orders_count"] == 1
    assert state["unprotected_symbols"] == []
    assert len(state["positions"]) == 1
    assert state["positions"][0]["symbol"] == "SPY"
    assert "narrative" in state
    assert "NL=" in state["narrative"]


def test_build_book_daily_pnl_is_not_unrealized():
    state = build_book(
        account={"netliquidation": 50_000, "unrealizedpnl": -800},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
        include_narrative=False,
    )
    assert state["daily_pnl"] is None
    state2 = build_book(
        account={"netliquidation": 50_000, "dailypnl": 0.0, "unrealizedpnl": -800},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
        include_narrative=False,
    )
    assert state2["daily_pnl"] == 0.0


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
