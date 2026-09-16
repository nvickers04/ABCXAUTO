"""Expectancy / Kelly / ruin facts from the Orion path post."""

import math

from abcxauto.path_math import (
    commission_cost,
    conservative_premium_usd,
    conservative_px,
    conservative_trade_pnl,
    net_realized_usd,
    net_signed_premium,
    path_by_structure,
    path_facts,
    path_from_journal,
    path_pnls_from_rows,
    signed_premium_usd,
    structure_label,
)


def _leg(order_id, sec_type, pnl, qty=1, *, side=None, price=None, strike=None):
    # qty matters: _closed_fill_pnl treats a qty-blind row as "not a fill".
    # Legs of one ticket must differ by contract (strike) or print (side /
    # price) to count as separate legs — that is the journal's fill shape.
    row = {
        "order_id": order_id,
        "sec_type": sec_type,
        "realized_pnl": pnl,
        "quantity": qty,
    }
    if side is not None:
        row["side"] = side
    if price is not None:
        row["price"] = price
    if strike is not None:
        row["strike"] = strike
    return row


def _fill(order_id, sec_type, side, qty, price, pnl, exec_id, symbol="SPY"):
    """A journal ``fills`` row as ``closing_fills`` / ``record_fills`` shape it."""
    return {
        "exec_id": exec_id,
        "order_id": order_id,
        "symbol": symbol,
        "sec_type": sec_type,
        "side": side,
        "quantity": qty,
        "price": price,
        "commission": 1.0,
        "realized_pnl": pnl,
    }


def test_structure_label_names_the_shape_not_a_grade():
    assert structure_label([_leg(1, "OPT", 5.0)]) == "single_option"
    assert structure_label(
        [_leg(1, "OPT", 5.0, strike=400), _leg(1, "OPT", -2.0, strike=405)]
    ) == "spread_2leg"
    assert structure_label(
        [_leg(1, "OPT", 1.0, strike=k) for k in (95, 100, 105)]
    ) == "spread_3leg"
    assert structure_label([]) == "unknown"


def test_structure_label_stock_leg_wins():
    """A covered call is managed as the share lot, not a two-leg option bet."""
    legs = [_leg(1, "STK", 10.0), _leg(1, "OPT", -3.0)]
    assert structure_label(legs) == "stock"


def test_bag_parent_row_is_the_wrapper_not_a_leg():
    """IBKR prints the combo itself (sec_type BAG, realized 0) plus one row
    per leg. Counting the wrapper turned every vertical into a 3-leg."""
    vertical = [
        _fill(13131, "BAG", "SLD", 1, 0.60, 0.0, "0000e22a.6a9654ee.01.01", "HPQ"),
        _fill(13131, "OPT", "BOT", 1, 0.85, -134.7, "0000e22a.6a9654ee.02.01", "HPQ"),
        _fill(13131, "OPT", "SLD", 1, 1.45, 168.3, "0000e22a.6a9654ee.03.01", "HPQ"),
    ]
    assert structure_label(vertical) == "spread_2leg"
    fly = [
        _fill(14698, "BAG", "SLD", 2, 0.19, 0.0, "0001938b.6a97b8d6.01.01", "TLT"),
        _fill(14698, "OPT", "SLD", 2, 0.17, -63.44, "0001938b.6a97b8d6.02.01", "TLT"),
        _fill(14698, "OPT", "BOT", 4, 0.40, 193.11, "0001938b.6a97b8d6.03.01", "TLT"),
        _fill(14698, "OPT", "SLD", 2, 0.82, -141.44, "0001938b.6a97b8d6.04.01", "TLT"),
    ]
    assert structure_label(fly) == "spread_3leg"
    # the wrapper alone says "combo" but not how many legs
    assert structure_label(vertical[:1]) == "unknown"


def test_partial_fills_of_one_contract_are_one_leg():
    """QQQ oid 18136: one option closed in two executions is not a spread."""
    partials = [
        _fill(18136, "OPT", "SLD", 1, 8.18, -110.71, "000182cd.6a9a5854.01.01", "QQQ"),
        _fill(18136, "OPT", "SLD", 1, 8.18, -110.01, "000182cd.6a9a5855.01.01", "QQQ"),
    ]
    assert structure_label(partials) == "single_option"
    out = path_by_structure(partials, equity=10_000.0, risk_pct=5.0)
    assert set(out) == {"single_option"}
    assert out["single_option"]["n"] == 1


def test_contract_identity_beats_the_print_fallback():
    """con_id / local_symbol / strike name the contract; price is the last resort."""
    same_con = [
        _leg(1, "OPT", -5.0, side="SLD", price=1.0, strike=None) | {"con_id": 7},
        _leg(1, "OPT", -6.0, side="SLD", price=1.1, strike=None) | {"con_id": 7},
    ]
    assert structure_label(same_con) == "single_option"
    two_con = [
        _leg(1, "OPT", -5.0, side="SLD", price=1.0) | {"con_id": 7},
        _leg(1, "OPT", 4.0, side="SLD", price=1.0) | {"con_id": 8},
    ]
    assert structure_label(two_con) == "spread_2leg"
    by_local = [
        _leg(1, "OPT", -5.0) | {"local_symbol": "SPY 260918C00650000"},
        _leg(1, "OPT", 4.0) | {"local_symbol": "SPY 260918C00655000"},
    ]
    assert structure_label(by_local) == "spread_2leg"


def test_bag_wrapper_nets_as_zero_and_pools_by_leg_count():
    """Through path_by_structure the wrapper neither adds P&L nor a leg."""
    rows = [
        _fill(13209, "BAG", "SLD", 1, 1.06, 0.0, "00020057.6a970096.01.01", "IWM"),
        _fill(13209, "OPT", "BOT", 1, 0.02, 78.58, "00020057.6a970096.02.01", "IWM"),
        _fill(13209, "OPT", "SLD", 1, 1.08, -369.42, "00020057.6a970096.03.01", "IWM"),
        _fill(14698, "BAG", "SLD", 2, 0.19, 0.0, "0001938b.6a97b8d6.01.01", "TLT"),
        _fill(14698, "OPT", "SLD", 2, 0.17, -63.44, "0001938b.6a97b8d6.02.01", "TLT"),
        _fill(14698, "OPT", "BOT", 4, 0.40, 193.11, "0001938b.6a97b8d6.03.01", "TLT"),
        _fill(14698, "OPT", "SLD", 2, 0.82, -141.44, "0001938b.6a97b8d6.04.01", "TLT"),
    ]
    out = path_by_structure(rows, equity=10_000.0, risk_pct=5.0)
    assert set(out) == {"spread_2leg", "spread_3leg"}
    assert out["spread_2leg"]["n"] == 1
    assert out["spread_3leg"]["n"] == 1
    assert [round(x, 2) for x in path_pnls_from_rows(rows)] == [-290.84, -11.77]


def test_path_by_structure_splits_pools_and_nets_legs():
    """A spread's debit and credit wing are one signed close, not W plus L."""
    rows = [
        # one 2-leg spread: +100 / -160 nets to -60
        _leg(10, "OPT", 100.0, side="SLD", price=2.0),
        _leg(10, "OPT", -160.0, side="BOT", price=3.5),
        # two single options
        _leg(11, "OPT", 40.0),
        _leg(12, "OPT", -20.0),
        # one stock ticket
        _leg(13, "STK", 15.0),
    ]
    out = path_by_structure(rows, equity=1000.0, risk_pct=5.0)
    assert set(out) == {"spread_2leg", "single_option", "stock"}
    # the spread is ONE sample, and it is a loss - not a win and a loss
    assert out["spread_2leg"]["n"] == 1
    assert out["single_option"]["n"] == 2
    assert out["stock"]["n"] == 1


def test_path_by_structure_keeps_the_thin_note():
    """Thin pools must not pretend a sample just because they are split out."""
    out = path_by_structure(
        [_leg(1, "OPT", 5.0), _leg(2, "OPT", -3.0)],
        equity=1000.0,
        risk_pct=5.0,
    )
    assert out["single_option"]["note"] == "thin closed-fill sample"


def test_path_by_structure_ignores_rows_without_an_order_id():
    assert path_by_structure(
        [{"sec_type": "OPT", "realized_pnl": 5.0}],
        equity=1000.0,
        risk_pct=5.0,
    ) == {}


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


def test_locked_last_quote_is_not_a_conservative_mark():
    """Paper TWS last printed as bid=ask=fill is the mid with a sticker."""
    buy = {
        "price": 100.0,
        "quantity": 10,
        "side": "BOT",
        "sec_type": "STK",
        "bid": 100.0,
        "ask": 100.0,
        "ibkr_last": 100.0,
        "realized_pnl": 0.0,
    }
    sell = {
        "price": 105.0,
        "quantity": 10,
        "side": "SLD",
        "sec_type": "STK",
        "bid": 105.0,
        "ask": 105.0,
        "ibkr_last": 105.0,
        "realized_pnl": 50.0,
    }
    assert conservative_px(buy) is None
    assert conservative_px(sell) is None
    assert conservative_trade_pnl([buy, sell]) is None


def test_mid_inside_spread_reprices_to_the_far_side():
    buy = {
        "price": 100.0,
        "quantity": 10,
        "side": "BUY",
        "sec_type": "STK",
        "bid": 99.90,
        "ask": 100.10,
        "ibkr_last": 100.0,
        "fill_label": "mid_inside_spread",
        "commission": 1.0,
    }
    sell = {
        "price": 105.0,
        "quantity": 10,
        "side": "SELL",
        "sec_type": "STK",
        "bid": 104.90,
        "ask": 105.10,
        "ibkr_last": 105.0,
        "fill_label": "mid_inside_spread",
        "commission": 1.0,
        "realized_pnl": 50.0,
    }
    assert conservative_px(buy) == 100.10
    assert conservative_px(sell) == 104.90
    assert conservative_trade_pnl([buy, sell]) == 46.0


def test_fill_at_ask_with_last_at_ask_stays_the_print():
    """Last sitting on the ask is a real far-side fill, not a locked mid."""
    row = {
        "price": 100.10,
        "quantity": 10,
        "side": "BUY",
        "sec_type": "STK",
        "bid": 99.90,
        "ask": 100.10,
        "ibkr_last": 100.10,
        "fill_label": "at_ask",
    }
    assert conservative_px(row) == 100.10
