"""Expectancy / Kelly / ruin facts from the Orion path post."""

import math

from abcxauto.path_math import (
    commission_cost,
    conservative_premium_usd,
    conservative_px,
    conservative_trade_pnl,
    net_realized_usd,
    net_signed_premium,
    path_facts,
    path_from_journal,
    path_pnls_from_rows,
    signed_premium_usd,
)


def test_even_money_coin_matches_post():
    pnls = [1.0] * 55 + [-1.0] * 45
    out = path_facts(pnls, equity=100.0, risk_pct=10.0)
    assert out["n"] == 100
    assert out["p"] == 0.55
    assert out["q"] == 0.45
    assert out["E"] == 0.1
    assert out["A"] == 1.0
    assert out["B"] == 1.0
    assert out["E_pct_of_nl"] == 0.1
    assert out["A_pct_of_nl"] == 1.0
    assert out["B_pct_of_nl"] == 1.0
    assert out["sig_pct_of_nl"] == round(100.0 * out["sig"] / 100.0, 4)
    assert out["b"] == 1.0
    assert out["kelly"] == 0.1
    assert out["f"] == 0.1
    assert out["N"] == 10.0
    assert out["ruin"] == round((0.45 / 0.55) ** 10, 4)
    expect_g = 0.55 * math.log(1.1) + 0.45 * math.log(0.9)
    assert out["g_kelly"] == round(expect_g, 6)


def test_thin_sample_keeps_lever_only():
    out = path_facts([1.0, -1.0], equity=35000.0, risk_pct=0.75)
    assert out["n"] == 2
    assert out["f"] == 0.0075
    assert "kelly" not in out
    assert "thin" in out["note"]


def test_ruin_is_certain_when_p_not_above_half():
    pnls = [1.0] * 4 + [-1.0] * 6
    out = path_facts(pnls, equity=100.0, risk_pct=25.0)
    assert out["p"] == 0.4
    assert out["ruin"] == 1.0


def test_signed_premium_keeps_debit_and_credit():
    debit = signed_premium_usd(
        {
            "avg_fill_price": 1.25,
            "quantity": 2,
            "side": "BUY",
            "sec_type": "OPT",
        }
    )
    credit = signed_premium_usd(
        {
            "avg_fill_price": 1.25,
            "quantity": 2,
            "side": "SELL",
            "sec_type": "OPT",
        }
    )
    assert debit == -250.0
    assert credit == 250.0


def test_last_is_not_a_fill_premium():
    last_only = {
        "last": 2.50,
        "mid": 2.48,
        "quantity": 2,
        "side": "BUY",
        "sec_type": "OPT",
    }
    assert signed_premium_usd(last_only) is None
    assert path_pnls_from_rows([{**last_only, "realized_pnl": None}]) == []
    # last sitting next to a real close must not replace the fill P&L
    assert path_pnls_from_rows(
        [{"order_id": 7, "quantity": 1, "realized_pnl": -80.0, "last": 2.50}]
    ) == [-80.0]


def test_qty_blind_premium_is_not_cash():
    assert signed_premium_usd(
        {"avg_fill_price": 1.25, "side": "BUY", "sec_type": "OPT"}
    ) is None
    assert signed_premium_usd(
        {"avg_fill_price": 1.25, "quantity": 0, "side": "SELL", "sec_type": "OPT"}
    ) is None
    # realized dollars without a qty are not a fill
    assert path_pnls_from_rows([{"realized_pnl": 80.0, "last": 2.5}]) == []


def test_debit_vertical_does_not_invert():
    long_call = {
        "avg_fill_price": 2.00,
        "quantity": 1,
        "side": "BUY",
        "sec_type": "OPT",
        "right": "C",
        "strike": 370,
    }
    short_call = {
        "avg_fill_price": 0.75,
        "quantity": 1,
        "side": "SELL",
        "sec_type": "OPT",
        "right": "C",
        "strike": 375,
    }
    assert net_signed_premium([long_call, short_call]) == -125.0
    # last on a wing fails the combo closed — do not invent a credit
    assert net_signed_premium([long_call, {**short_call, "avg_fill_price": None, "last": 0.75}]) is None


def test_vertical_close_nets_one_signed_sample():
    # Losing debit vertical: long wing -150, short wing +100. Net debit -50.
    # Per-leg path samples would look like a win and a loss (inverted structure).
    rows = [
        {"order_id": 10, "quantity": 1, "realized_pnl": -150.0, "last": 1.50},
        {"order_id": 10, "quantity": 1, "realized_pnl": 100.0, "last": 0.75},
        {"order_id": 11, "quantity": 2, "realized_pnl": -80.0},
        {"order_id": 12, "quantity": 1, "realized_pnl": 40.0},
        {"order_id": 13, "quantity": 1, "realized_pnl": -20.0},
    ]
    xs = path_pnls_from_rows(rows)
    assert xs == [-50.0, -80.0, 40.0, -20.0]
    out = path_facts(rows, equity=10_000.0, risk_pct=1.0)
    assert out["n"] == 4
    assert out["E"] == -27.5
    assert out["p"] == 0.25


def test_path_from_journal_nets_vertical_not_leg_tape():
    class _J:
        def closing_fills(self):
            return [
                {"order_id": 10, "quantity": 1, "realized_pnl": -150.0, "last": 2.5},
                {"order_id": 10, "quantity": 1, "realized_pnl": 100.0, "last": 0.8},
                {"order_id": 11, "quantity": 1, "realized_pnl": -40.0},
                {"order_id": 12, "quantity": 1, "realized_pnl": 10.0},
                {"order_id": 13, "quantity": 1, "realized_pnl": -10.0},
            ]

        def closed_fill_pnls(self):
            # Per-leg tape — using this would invert the vertical into a coin flip.
            return [-150.0, 100.0, -40.0, 10.0, -10.0]

    out = path_from_journal(_J(), equity=10_000.0, risk_pct=1.0)
    assert out["n"] == 4
    assert out["E"] == -22.5
    assert out["p"] == 0.25


def test_commission_is_a_positive_cost():
    assert commission_cost({"commission": 1.25}) == 1.25
    assert commission_cost({"commission": -0.65}) == 0.65
    assert commission_cost({"realized_pnl": 50.0}) == 0.0
    assert net_realized_usd({"realized_pnl": 50.0, "commission": 1.3}) == 48.7
    assert net_realized_usd({"commission": 1.0}) is None


def test_debit_marks_at_ask_and_credit_at_bid():
    buy = {
        "price": 2.00,
        "quantity": 1,
        "side": "BUY",
        "sec_type": "OPT",
        "bid": 1.95,
        "ask": 2.05,
    }
    sell = {
        "price": 2.50,
        "quantity": 1,
        "side": "SELL",
        "sec_type": "OPT",
        "bid": 2.45,
        "ask": 2.55,
    }
    assert conservative_px(buy) == 2.05
    assert conservative_px(sell) == 2.45
    assert conservative_premium_usd(buy) == -205.0
    assert conservative_premium_usd(sell) == 245.0
    # Paper mid was -200 + 250 = +50. Conservative is +40 before fees.
    assert conservative_trade_pnl(
        [
            {**buy, "commission": 0.65},
            {**sell, "commission": 0.65, "realized_pnl": 50.0},
        ]
    ) == 38.7


def test_fill_worse_than_nbbo_keeps_the_fill():
    paid_through = {
        "price": 100.20,
        "quantity": 10,
        "side": "BOT",
        "sec_type": "STK",
        "bid": 99.90,
        "ask": 100.10,
    }
    assert conservative_px(paid_through) == 100.20


def test_paper_mid_without_quotes_is_not_a_conservative_mark():
    mid_only = {
        "price": 100.0,
        "quantity": 10,
        "side": "BOT",
        "sec_type": "STK",
        "realized_pnl": 0.0,
    }
    closer = {
        "price": 105.0,
        "quantity": 10,
        "side": "SLD",
        "sec_type": "STK",
        "realized_pnl": 50.0,
    }
    assert conservative_px(mid_only) is None
    assert conservative_trade_pnl([mid_only, closer]) is None
    assert conservative_trade_pnl([closer]) is None
