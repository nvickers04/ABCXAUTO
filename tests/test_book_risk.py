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


_CANNED_TAPE = ("SPY", "QQQ", "IWM", "DIA")


def _cfg_patch(monkeypatch) -> None:
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {})(),
    )


def test_slim_positions_empty_book_does_not_inject_index_defaults(monkeypatch):
    _cfg_patch(monkeypatch)
    assert book._slim_positions([]) == []
    state = book.build_book(
        account={"netliquidation": 10_000, "dailypnl": 0},
        positions=[],
        open_orders=[],
        protection={"unprotected_symbols": []},
        include_narrative=False,
    )
    symbols = [p.get("symbol") for p in (state.get("positions") or [])]
    assert symbols == []
    for name in _CANNED_TAPE:
        assert name not in symbols


def test_slim_positions_follows_open_lots_not_tape_names(monkeypatch):
    _cfg_patch(monkeypatch)

    def boom(*_a, **_k):
        raise AssertionError("book must not seed tape / universe names")

    monkeypatch.setattr("abcxauto.opportunity_scan.tape_seed_symbols", boom)
    monkeypatch.setattr("abcxauto.universe.legal_symbols", boom)

    mixed = [
        {"symbol": "SPY"},
        {"symbol": "QQQ", "quantity": 0},
        {"symbol": "AAPL", "quantity": 7, "secType": "STK", "conId": 11},
        {"symbol": "IWM"},
        {"symbol": "DIA", "position": 0},
        {"symbol": "MSFT", "position": 4, "secType": "STK", "conId": 12},
    ]
    slim = book._slim_positions(mixed)
    assert [p.get("symbol") for p in slim] == ["AAPL", "MSFT"]
    assert slim[0]["qty"] == 7
    assert slim[1]["qty"] == 4

    state = book.build_book(
        account={"netliquidation": 50_000, "dailypnl": 0},
        positions=mixed,
        open_orders=[],
        protection={"unprotected_symbols": []},
        include_narrative=False,
    )
    assert [p.get("symbol") for p in state["positions"]] == ["AAPL", "MSFT"]


def test_slim_positions_keeps_real_index_lots():
    """Broker-held SPY/QQQ is a lot. Defaults are the thing we refuse."""
    slim = book._slim_positions(
        [
            {
                "symbol": "SPY",
                "quantity": 11,
                "secType": "STK",
                "conId": 756733,
                "marketValue": 8_000,
            }
        ]
    )
    assert len(slim) == 1
    assert slim[0]["symbol"] == "SPY"
    assert slim[0]["qty"] == 11


def test_slim_positions_limit_skips_tape_filler():
    filler = [{"symbol": name} for name in _CANNED_TAPE * 4]
    lots = [
        {"symbol": "NVDA", "quantity": 3, "secType": "STK", "conId": 1},
        {"symbol": "XLE", "quantity": -1, "secType": "OPT", "conId": 2},
    ]
    slim = book._slim_positions(filler + lots, limit=12)
    assert [p.get("symbol") for p in slim] == ["NVDA", "XLE"]
    assert slim[1]["qty"] == -1
