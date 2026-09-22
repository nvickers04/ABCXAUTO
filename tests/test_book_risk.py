"""Smoke tests for the abcxauto.book product surface."""

from __future__ import annotations

import pytest

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
    assert state["capital_liquidity"].get("cash_pct_nl") is None
    assert "total_cash" not in state["capital_liquidity"]
    assert state["capital_liquidity"]["deployed_long_pct_nl"] == 0.0
    assert "note" not in state["capital_liquidity"]
    assert "note" not in state["exposure"]


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
    assert "note" not in state["capital_liquidity"]
    assert "note" not in state["exposure"]


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
    # portfolio_risk must not count zero-qty / bare-ticker tape residue
    assert state["portfolio_risk"]["n_positions"] == 2
    assert [r["symbol"] for r in state["exposure"]["symbols"]] == ["AAPL", "MSFT"]


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


def test_allocation_line_leads_with_leftover():
    from abcxauto.world_state import allocation_facts, allocation_line

    facts = allocation_facts(
        [
            {
                "symbol": "QQQ",
                "quantity": 10,
                "marketValue": 3000,
                "secType": "STK",
            }
        ],
        net_liq=10_000,
        total_cash=7000,
    )
    assert facts["leftover_usd"] == 7000.0
    assert facts["deployed_usd"] == 3000.0
    assert facts["cash_pct_nl"] == 70.0
    assert facts["deployed_pct_nl"] == 30.0
    facts["daily_pnl"] = -72
    line = allocation_line(facts)
    assert line.startswith("leftover $7000 cash=70.0% deployed=30.0% day=-72 on deployed")
    assert "vs lots-to-target" not in line
    assert "QQQ" in line


def test_allocation_line_leftover_vs_lots_to_target():
    from abcxauto.world_state import allocation_facts, allocation_line

    facts = allocation_facts(
        [
            {
                "symbol": "AMZN",
                "quantity": 10,
                "secType": "STK",
                "mkt": 255.22,
            }
        ],
        net_liq=50_000,
        total_cash=27_304,
        quotes={"AMZN": 255.22},
        orders=[
            {
                "symbol": "AMZN",
                "type": "LMT",
                "lmt": 256,
                "role": "exit",
            }
        ],
    )
    lot = facts["lots"][0]
    assert lot["symbol"] == "AMZN"
    assert lot["qty"] == 10
    assert lot["last"] == 255.22
    assert lot["target"] == 256
    assert lot["to_target_usd"] == pytest.approx(7.8)
    assert facts["lots_to_target_usd"] == pytest.approx(7.8)
    assert facts["leftover_usd"] == 27304.0
    line = allocation_line(facts)
    assert line.startswith("leftover $27304")
    assert " vs lots-to-target $" in line
    assert "7.8" in line
    assert "tgt=256" in line
    assert "to_tgt=" in line
    assert "AMZN" in line


def test_allocation_liquidity_cut_and_risk_to_stop_full_book():
    """Near-full STK book: dollars freed by cut + risk-to-stop, not advice."""
    from abcxauto.world_state import allocation_facts, allocation_line

    # Live-shaped 2026-09-22: 89 AVGO ~99% NL, ~$214 cash, stop close.
    nl = 32_343.0
    last = 361.0
    stop = 356.35
    qty = 89
    facts = allocation_facts(
        [
            {
                "symbol": "AVGO",
                "quantity": qty,
                "secType": "STK",
                "mkt": last,
            },
            {
                "symbol": "GHOST",
                "quantity": 0,
                "secType": "STK",
                "mkt": 100.0,
            },
        ],
        net_liq=nl,
        total_cash=214.0,
        quotes={"AVGO": last},
        orders=[{"symbol": "AVGO", "type": "STP", "stop": stop}],
    )
    assert [lot["symbol"] for lot in facts["lots"]] == ["AVGO"]
    lot = facts["lots"][0]
    assert lot["qty"] == 89
    assert lot["last"] == last
    assert lot["stop"] == stop
    assert lot["cut_all_usd"] == pytest.approx(89 * last)
    assert lot["cut_half_usd"] == pytest.approx(44 * last)
    assert lot["risk_pct_nl"] == pytest.approx(
        round(abs(last - stop) * qty / nl * 100, 2)
    )
    assert facts["leftover_usd"] == 214.0
    assert facts["liquidity"]["cut_all_usd"] == pytest.approx(89 * last)
    assert facts["liquidity"]["cut_half_usd"] == pytest.approx(44 * last)
    assert facts["risk_to_stop_usd"] == pytest.approx(abs(last - stop) * qty)
    line = allocation_line(facts)
    assert "leftover $214" in line
    assert "cut-half $" in line
    assert "cut-all $" in line
    assert "risk-to-stop $" in line
    assert "AVGO89" in line
    assert f"risk={lot['risk_pct_nl']}%" in line
    lower = line.lower()
    assert "sell" not in lower
    assert "should" not in lower
    assert "must" not in lower
    assert "you " not in lower


def test_format_wake_full_avgo_book_includes_cut_half_and_risk_to_stop():
    """Continuation wake must carry liquidity dollars — book tool rows are omitted."""
    from abcxauto.world_state import allocation_facts, format_wake

    nl = 32_343.0
    last = 362.77
    stop = 356.35
    qty = 89
    cash = 214.23
    alloc = allocation_facts(
        [
            {
                "symbol": "AVGO",
                "quantity": qty,
                "secType": "STK",
                "mkt": last,
            }
        ],
        net_liq=nl,
        total_cash=cash,
        quotes={"AVGO": last},
        orders=[
            {"symbol": "AVGO", "type": "STP", "stop": stop},
            {"symbol": "AVGO", "type": "LMT", "lmt": 369.85, "role": "exit"},
        ],
    )
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "nl": nl,
            "daily_pnl": 0.0,
            "allocation": alloc,
            "capital_liquidity": {
                "total_cash": cash,
                "cash_pct_nl": alloc.get("cash_pct_nl"),
                "deployed_long_pct_nl": alloc.get("deployed_pct_nl"),
            },
            "exposure": {
                "top_symbol": "AVGO",
                "top_concentration_pct": alloc["lots"][0]["pct_nl"],
            },
            "capacity": {"open_count": 1, "max_open_positions": 0},
            "open_lots": ["AVGO STK long 89"],
            "buying_power_usd": 21854.01,
            "cash_only": False,
            "stop_dist": {
                "ident": "AVGO STK long 89",
                "dist": 6.42,
                "stop": stop,
                "last": last,
            },
        },
    )
    assert "cut-half $" in wake
    assert "cut-all $" in wake
    assert "risk-to-stop $" in wake
    assert "leftover $214.23" in wake or "leftover $214" in wake
    assert "AVGO89" in wake
    assert "stp=356.35" in wake
    assert "tgt=369.85" in wake
    assert "to_tgt=" in wake
    assert "%NL" in wake
    assert "buying_power=$21854.01" in wake
    assert "cash_only=off" in wake
    assert wake.splitlines()[0].startswith("fact: closest_stop AVGO")
    assert "to_tgt=" in wake.splitlines()[0]
    assert "eff=0.0195" in wake
    assert "alloc AVGO cap=" in wake
    lower = wake.lower()
    assert "sell" not in lower
    assert "rotate" not in lower
    assert "should" not in lower


def test_format_wake_day_lines_alloc_research_spend():
    """Preformatted day lines ride the wake as fact sentences."""
    from abcxauto.world_state import format_wake

    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 0,
            "lots": 0,
            "capacity": {"open_count": 0, "max_open_positions": 0},
            "alloc_line": "alloc fact FOO=1",
            "research_line": "research budget left=2",
            "session_spend_usd": 12.5,
        },
    )
    assert "alloc fact FOO=1" in wake
    assert "research budget left=2" in wake
    assert "spend session=$12.50" in wake
    lower = wake.lower()
    assert "sell" not in lower
    assert "rotate" not in lower


def test_allocation_zero_cash_stays_zero_and_bid_feeds_cut():
    from abcxauto.world_state import allocation_facts, allocation_line

    facts = allocation_facts(
        [
            {
                "symbol": "MSFT",
                "quantity": 10,
                "secType": "STK",
                "bid": 400.0,
            }
        ],
        net_liq=10_000.0,
        total_cash=0.0,
    )
    assert facts["leftover_usd"] == 0.0
    assert facts["cash_pct_nl"] == 0.0
    lot = facts["lots"][0]
    assert lot["last"] == 400.0
    assert lot["cut_all_usd"] == 4000.0
    assert lot["cut_half_usd"] == 2000.0
    assert "risk_to_stop_usd" not in facts
    assert "stop" not in lot
    line = allocation_line(facts)
    assert "leftover $0" in line
    assert "cash=0" in line
    assert "cut-half $2000" in line
    assert "cut-all $4000" in line
    assert "risk-to-stop" not in line


def test_allocation_opt_and_short_skip_cut_liquidity():
    from abcxauto.world_state import allocation_facts

    facts = allocation_facts(
        [
            {
                "symbol": "QQQ",
                "quantity": 1,
                "secType": "OPT",
                "mkt": 5.0,
            },
            {
                "symbol": "SPY",
                "quantity": -10,
                "secType": "STK",
                "mkt": 500.0,
                "stop": 510.0,
            },
        ],
        net_liq=100_000.0,
        total_cash=50_000.0,
    )
    by_sym = {lot["symbol"]: lot for lot in facts["lots"]}
    assert "cut_all_usd" not in by_sym["QQQ"]
    assert "cut_half_usd" not in by_sym["QQQ"]
    assert "cut_all_usd" not in by_sym["SPY"]
    assert "liquidity" not in facts
    # Short has risk_pct_nl but does not add to long-only risk_to_stop_usd.
    assert by_sym["SPY"].get("risk_pct_nl") is not None
    assert "risk_to_stop_usd" not in facts
