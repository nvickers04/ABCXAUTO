"""Payoff page: loss_per_unit facts, no solved q, no advice words."""

from __future__ import annotations

import re

from abcxauto.order_type_fact import (
    format_order_type_page,
    loss_per_unit_credit_vertical,
    loss_per_unit_debit,
    loss_per_unit_stock,
)

_BANNED = ("should", "must", "buy this", "sell this")


def _page(**kwargs) -> str:
    return format_order_type_page(**kwargs)


def test_helpers_return_unit_loss_not_share_count():
    assert loss_per_unit_stock(100, 95) == 5.0
    assert loss_per_unit_stock(100, None) is None
    assert loss_per_unit_debit(1.25) == 125.0
    assert loss_per_unit_debit(None) is None
    assert loss_per_unit_credit_vertical(5.0, 1.5) == 350.0
    assert loss_per_unit_credit_vertical(5.0, None) is None


def test_cash_only_default_short_stk_unavailable():
    text = _page()
    assert "short_stk unavailable cash_only=on" in text
    assert "short_stk loss_per_unit" not in text


def test_cash_only_false_prints_short_stk_loss():
    text = _page(cash_only=False, price=100, entry=100, stop=95)
    assert "short_stk unavailable" not in text
    assert "short_stk loss_per_unit=|entry-stop|=5" in text


def test_identity_nl_filled_f_q_blank_no_solved_q():
    text = _page(nl=37000, price=100, stop=95)
    assert "NL=37000" in text
    assert "f=?" in text
    assert "q=?" in text
    assert re.search(r"\bq=\d+\b", text) is None
    assert "dollars_at_risk=q * loss_per_unit" in text


def test_long_stk_price_and_stop_fills_loss_not_q():
    text = _page(nl=37000, price=100, stop=95, target=110, atr=2)
    assert "long_stk loss_per_unit=|entry-stop|=5" in text
    assert "cash_per_share=100" in text
    assert "reward_per_share=10" in text
    assert "reward/risk=2" in text
    assert "stop_atr=2.5" in text
    # q stays blank — identity only
    top = text.splitlines()[0]
    assert "q=?" in top
    assert not re.search(r"q=\d+", top)


def test_long_opt_debit_and_breakevens():
    text = _page(debit=2.5, strike=100, price=100, bid=2.4, ask=2.6, delta=0.45)
    assert "long_opt loss_per_unit=debit*100=250" in text
    assert "breakeven_call=102.5" in text
    assert "breakeven_put=97.5" in text
    assert "delta(risk-neutral)=0.45" in text
    assert "spread=(ask-bid)/mid=" in text


def test_credit_vertical_fraction_of_width_not_probability():
    text = _page(width=5, credit=1.5)
    assert "vertical_credit loss_per_unit=(width-credit)*100=350" in text
    assert "fraction_of_width=0.3" in text
    assert "probability" not in text.lower()
    assert "max_profit=credit*100=150" in text


def test_debit_vertical_fraction_of_width_not_probability():
    text = _page(width=5, debit=1.5)
    assert "vertical_debit loss_per_unit=net_debit*100=150" in text
    assert "fraction_of_width=" in text
    assert "probability" not in text.lower()


def test_csp_effective_buy():
    text = _page(strike=100, credit=2)
    assert "cash_secured_put loss_per_unit=(strike-credit)*100=9800" in text
    assert "cash_secured=10000" in text
    assert "effective_buy=98" in text
    assert "yield_if_expire=0.02" in text
    assert "probability" not in text.lower()


def test_covered_call_credit_alone_is_not_loss_when_stop_missing():
    text = _page(credit=1.5, price=100, strike=105)
    cc_line = [ln for ln in text.splitlines() if ln.startswith("covered_call")][0]
    assert "loss_per_unit=?" not in cc_line
    assert "upside_cap=5" in cc_line
    assert "loss_per_share=" not in cc_line
    assert "150" not in cc_line  # credit*100 must not masquerade as loss


def test_covered_call_with_stop_and_credit():
    text = _page(entry=100, stop=95, credit=1.5, price=100, strike=105)
    cc_line = [ln for ln in text.splitlines() if ln.startswith("covered_call")][0]
    assert "loss_per_share=|entry-stop|-credit=3.5" in cc_line
    assert "upside_cap=5" in cc_line


def test_jade_ratio_not_on_page():
    text = _page()
    assert "ratio_spread" not in text
    assert "jade_lizard" not in text


def test_iron_and_butterfly_and_straddle_notes():
    text = _page(wing=5, credit=1.2, debit=2.0, price=50)
    assert "iron_condor loss_per_unit=(wing-credit)*100=380" in text
    assert "fraction_of_wing=" in text
    assert "butterfly loss_per_unit=debit*100=200" in text
    assert "long_straddle loss_per_unit=debit*100=200" in text
    assert "move_pct=4" in text  # 2/50*100


def test_execution_and_management_notes():
    text = _page(stock_bid=1.0, stock_ask=1.2)
    assert "execution limit|market|stop|vwap|twap does not change loss_per_unit" in text
    ex_line = [ln for ln in text.splitlines() if ln.startswith("execution ")][0]
    assert "spread=(ask-bid)/mid=" in ex_line
    assert "cancel modify close roll size=existing_order" in text

    opt_only = _page(bid=1.0, ask=1.2)
    opt_ex = [ln for ln in opt_only.splitlines() if ln.startswith("execution ")][0]
    assert "spread=" not in opt_ex

    with_opt = _page(debit=2.0, bid=1.0, ask=1.2)
    with_opt_ex = [ln for ln in with_opt.splitlines() if ln.startswith("execution ")][0]
    assert "spread=" not in with_opt_ex
    long_opt = [ln for ln in with_opt.splitlines() if ln.startswith("long_opt ")][0]
    assert "spread=(ask-bid)/mid=" in long_opt


def test_iv_minus_rv_once_when_both_passed():
    text = _page(iv=22, rv=14, debit=2.0)
    assert text.count("iv_minus_rv=") == 1
    assert "iv_minus_rv=8" in text


def test_no_banned_advice_words():
    text = _page(
        nl=37000,
        price=100,
        entry=100,
        stop=95,
        target=110,
        atr=2,
        debit=2,
        credit=1.5,
        width=5,
        wing=5,
        strike=100,
        put_strike=95,
        net_debit=0.5,
        bid=1,
        ask=1.2,
        delta=0.4,
        theta=-0.05,
        vega=0.1,
        iv=20,
        rv=12,
        front_iv=25,
        back_iv=22,
    )
    low = text.lower()
    for word in _BANNED:
        assert word not in low, word


def test_collar_and_calendar():
    text = _page(
        entry=100,
        put_strike=95,
        net_debit=-0.5,
        debit=1.0,
        front_iv=30,
        back_iv=25,
    )
    assert "collar loss_per_share=entry-put_strike+net_debit=4.5" in text
    assert "long_calendar loss_per_unit=debit*100=100" in text
    assert "front_iv=30" in text
    assert "back_iv=25" in text
    assert "front_minus_back=5" in text


def test_protective_put_floor():
    text = _page(debit=2, strike=100)
    assert "protective_put loss_per_unit=put_debit*100_extra=200" in text
    assert "floor=98" in text


def test_blank_inputs_print_question_marks_not_invented():
    text = _page()
    assert "q=floor(NL * f / loss_per_unit)" in text
    assert "dollars_at_risk=q * loss_per_unit" in text
    assert "execution limit|market|stop|vwap|twap does not change loss_per_unit" in text
    assert "cancel modify close roll size=existing_order" in text
    assert "short_stk unavailable cash_only=on" in text
    assert "long_stk" not in text
    assert "long_opt" not in text
    assert "vertical_" not in text
    assert "iron_" not in text
    assert "butterfly" not in text
    assert "straddle" not in text
    assert "cash_secured" not in text
    assert "covered_call" not in text
    assert "protective_put" not in text
    assert "collar" not in text
    assert "calendar" not in text
    assert "diagonal" not in text
    # identity keeps f=? q=? NL=?; no ? structure rows
    for ln in text.splitlines():
        if ln.startswith("q="):
            continue
        assert "?" not in ln, ln
