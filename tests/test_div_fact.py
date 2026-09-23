"""Diversification fact: measurement only, no ticket language."""

from __future__ import annotations

from abcxauto.div_fact import (
    beta_from_returns,
    diversification_facts,
    format_diversification,
    hhi_and_neff,
)

_BANNED = ("sell", "buy", "rotate", "should", "must", "trim", "excess")


def test_hhi_empty_and_one():
    assert hhi_and_neff([]) == (None, None)
    h, n = hhi_and_neff([1.0])
    assert h == 1.0
    assert n == 1.0


def test_hhi_two_equal():
    h, n = hhi_and_neff([0.5, 0.5])
    assert abs(h - 0.5) < 1e-9
    assert abs(n - 2.0) < 1e-9


def test_one_name_xom():
    facts = diversification_facts(
        [{"symbol": "XOM", "secType": "STK", "quantity": 48, "marketValue": 5400}],
        net_liq=32500,
        total_cash=24700,
    )
    assert facts["names"] == 1
    row = facts["rows"][0]
    assert row["symbol"] == "XOM"
    assert row["net_dir"] == 48
    assert row["side"] == "long"
    assert abs(facts["undeployed_pct_nl"] - 76.0) < 0.05
    assert facts["undeployed_usd"] == 24700
    assert facts["hhi"] == 1.0
    assert facts["n_eff"] == 1.0
    assert facts["heat"] == "no pair"
    assert facts["top_symbol"] == "XOM"
    assert abs(facts["top_pct_nl"] - 16.62) < 0.01


def test_two_equal_names_neff():
    facts = diversification_facts(
        [
            {"symbol": "AAA", "secType": "STK", "quantity": 10, "marketValue": 5000},
            {"symbol": "BBB", "secType": "STK", "quantity": 10, "marketValue": 5000},
        ],
        net_liq=20000,
        total_cash=10000,
    )
    assert facts["names"] == 2
    assert abs(facts["n_eff"] - 2.0) < 1e-9
    assert abs(facts["hhi"] - 0.5) < 1e-9


def test_long_put_negative_net_dir():
    facts = diversification_facts(
        [
            {
                "symbol": "XOM",
                "secType": "OPT",
                "quantity": 1,
                "right": "P",
                "marketValue": 200,
            }
        ],
        net_liq=32500,
        total_cash=24700,
    )
    assert facts["names"] == 1
    assert facts["rows"][0]["net_dir"] == -100
    assert facts["rows"][0]["opt"] == -100
    assert facts["rows"][0]["side"] == "short"


def test_beta_from_returns_perfect():
    name = [float(i) for i in range(25)]
    spy = list(name)
    b = beta_from_returns(name, spy)
    assert b is not None
    assert abs(b - 1.0) < 1e-9


def test_beta_needs_twenty_pairs():
    assert beta_from_returns([1.0] * 19, [1.0] * 19) is None


def test_beta_var_zero():
    assert beta_from_returns([1.0] * 25, [0.0] * 25) is None


def test_format_undeployed_heat_no_banned():
    facts = diversification_facts(
        [{"symbol": "XOM", "secType": "STK", "quantity": 48, "marketValue": 5400}],
        net_liq=32500,
        total_cash=24700,
    )
    text = format_diversification(facts)
    assert "undeployed" in text
    assert "heat=no pair" in text
    low = text.lower()
    for w in _BANNED:
        assert w not in low, w


def test_format_board_line_appended():
    facts = diversification_facts(
        [{"symbol": "XOM", "secType": "STK", "quantity": 48, "marketValue": 5400}],
        net_liq=32500,
        total_cash=24700,
    )
    text = format_diversification(facts, board_line="board ok")
    assert text.endswith("board ok")


def test_format_beta_when_passed():
    facts = diversification_facts(
        [{"symbol": "XOM", "secType": "STK", "quantity": 48, "marketValue": 5400}],
        net_liq=32500,
        total_cash=24700,
        betas={"XOM": 0.8},
    )
    text = format_diversification(facts)
    assert "beta XOM=0.80" in text


def test_heat_multi_name_component():
    facts = diversification_facts(
        [
            {"symbol": "AAA", "secType": "STK", "quantity": 1, "marketValue": 1000},
            {"symbol": "BBB", "secType": "STK", "quantity": 1, "marketValue": 1000},
            {"symbol": "CCC", "secType": "STK", "quantity": 1, "marketValue": 1000},
        ],
        net_liq=10000,
        total_cash=7000,
        heat_groups=[["AAA", "BBB"], ["CCC"]],
    )
    assert facts["heat"] == "AAA+BBB"
