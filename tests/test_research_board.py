"""Research board: same columns, no borrowed chain, sizeable sorts first."""

from __future__ import annotations

import pytest

from abcxauto.research_board import board_line, build_board, on_board, sort_board_rows

COLS = (
    "symbol",
    "last",
    "spread",
    "vs_spy",
    "earnings_in",
    "news_n",
    "web",
    "odds",
    "chain",
    "sized",
    "option",
    "sizeable",
)


def _held_avgo(**extra):
    row = {
        "symbol": "AVGO",
        "last": 180.0,
        "bid": 179.9,
        "ask": 180.1,
        "vs_spy": 0.12,
        "earnings_in": 30,
        "headlines": ["AVGO beats estimates"],
        "web": "ok",
        "odds": "unavailable",
        "chain": {
            "call": {"bid": 2.0, "ask": 2.2},
            "put": {"bid": 1.5, "ask": 1.7},
        },
        "stop_dist": 1.4,
        "sized": 22,
        "held": True,
    }
    row.update(extra)
    return row


def _scan_nvda(**extra):
    row = {
        "symbol": "NVDA",
        "last": 120.0,
        "bid": 119.8,
        "ask": 120.2,
        "vs_spy": 0.05,
        "earnings_in": 40,
        "headlines": ["NVDA supply update"],
        "web": "unavailable",
        "odds": "ok",
        "chain": None,
        "stop_dist": None,
        "sized": 0,
    }
    row.update(extra)
    return row


def test_held_and_scan_share_same_columns():
    board = build_board([_held_avgo(), _scan_nvda()])
    assert len(board) == 2
    by_sym = {r["symbol"]: r for r in board}
    assert set(by_sym) == {"AVGO", "NVDA"}
    for sym in ("AVGO", "NVDA"):
        for key in COLS:
            assert key in by_sym[sym]
    assert by_sym["AVGO"]["held"] is True
    assert by_sym["AVGO"]["spread"] == pytest.approx(0.2)
    assert by_sym["AVGO"]["option"] == "debit_vertical"
    assert by_sym["AVGO"]["sizeable"] is True
    assert by_sym["NVDA"]["chain"] == "unavailable"
    assert by_sym["NVDA"]["option"] == "unavailable"
    assert by_sym["NVDA"]["sizeable"] is False
    assert on_board(board, "avgo") is True
    assert on_board(board, "INTC") is False
    line = board_line(board)
    assert line.startswith("board ")
    assert "AVGO last=" in line
    assert "NVDA last=" in line
    assert "sell" not in line.lower()
    assert "rotate" not in line.lower()
    assert "should" not in line.lower()
    assert "must" not in line.lower()


def test_off_topic_headline_not_counted():
    board = build_board(
        [
            _held_avgo(
                headlines=[
                    "Markets rally on soft CPI",
                    "Fed speakers today",
                    "AVGO guidance raised",
                ]
            )
        ]
    )
    row = board[0]
    assert row["news_n"] == 1
    assert row.get("news") != "unavailable"

    all_off = build_board(
        [_scan_nvda(headlines=["Oil jumps", "Dollar softens"])]
    )
    assert all_off[0]["news_n"] == 0
    assert all_off[0].get("news") == "unavailable"


def test_missing_chain_is_unavailable_not_borrowed():
    held_chain = {
        "call": {"bid": 2.0, "ask": 2.2},
        "put": {"bid": 1.5, "ask": 1.7},
    }
    board = build_board(
        [
            _held_avgo(chain=held_chain),
            _scan_nvda(chain=None, last=120.0),
        ]
    )
    by_sym = {r["symbol"]: r for r in board}
    assert by_sym["AVGO"]["chain"] is held_chain
    assert by_sym["NVDA"]["chain"] == "unavailable"
    assert by_sym["NVDA"]["chain"] is not held_chain
    assert by_sym["NVDA"]["last"] == 120.0
    assert by_sym["NVDA"]["last"] != by_sym["AVGO"]["last"]


def test_caller_passes_names_ruled_out_not_this_module():
    """Ruled-out filtering is the caller's job; this module keeps what it is given."""
    board = build_board(
        [
            _held_avgo(),
            _scan_nvda(symbol="INTC", headlines=["INTC foundry update"]),
        ]
    )
    assert on_board(board, "INTC") is True
    assert {r["symbol"] for r in board} == {"AVGO", "INTC"}


def test_sizeable_sorts_before_headline_only():
    headline_only = {
        "symbol": "MU",
        "last": None,
        "vs_spy": 0.99,
        "headlines": ["MU memory demand"],
        "chain": None,
        "stop_dist": None,
    }
    sizeable_low_rs = {
        "symbol": "AMD",
        "last": 150.0,
        "vs_spy": 0.01,
        "headlines": [],
        "chain": {"call": {"bid": 1.0, "ask": 1.1}, "put": {"bid": 0.8, "ask": 0.9}},
        "stop_dist": None,
    }
    board = build_board([headline_only, sizeable_low_rs, _held_avgo(vs_spy=0.2)])
    assert board[0]["sizeable"] is True
    symbols = [r["symbol"] for r in board]
    assert symbols.index("AMD") < symbols.index("MU")
    assert board[0]["symbol"] in {"AVGO", "AMD"}


def test_spy_dropped_and_cap_keeps_held_plus_eight():
    rows = [_held_avgo()]
    rows.append(
        {
            "symbol": "SPY",
            "last": 500.0,
            "vs_spy": 0.0,
            "headlines": ["SPY etf flows"],
            "chain": None,
        }
    )
    for i, sym in enumerate(
        ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]
    ):
        rows.append(
            {
                "symbol": sym,
                "last": 10.0 + i,
                "vs_spy": 0.5 - i * 0.01,
                "stop_dist": 0.5,
                "headlines": [f"{sym} update"],
                "chain": None,
            }
        )
    board = build_board(rows)
    assert "SPY" not in {r["symbol"] for r in board}
    assert any(r.get("held") and r["symbol"] == "AVGO" for r in board)
    assert len(board) == 9  # held + 8 others
    assert len([r for r in board if not r.get("held")]) == 8


def test_sort_board_rows_exported():
    rows = [
        {"symbol": "B", "sizeable": False, "vs_spy": 0.9},
        {"symbol": "A", "sizeable": True, "vs_spy": 0.1},
    ]
    out = sort_board_rows(rows)
    assert [r["symbol"] for r in out] == ["A", "B"]
