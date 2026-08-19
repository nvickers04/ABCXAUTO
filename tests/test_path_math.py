"""Expectancy / Kelly / ruin facts from the Orion path post."""

import math

from abcxauto.path_math import path_facts


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
