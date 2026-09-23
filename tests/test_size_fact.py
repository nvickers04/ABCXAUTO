"""Size page: equations + filled inputs; never a solved share count."""

import math
import re

from abcxauto.path_math import growth_unequal
from abcxauto.size_fact import format_size_page


def test_nl_only_blanks_price_and_f_no_share_count():
    text = format_size_page(nl=35000)
    assert "NL=35000" in text
    assert "f=" in text
    assert re.search(r"\bf=\S", text) is None  # f never filled
    assert "n=" in text
    assert re.search(r"\bn=\d", text) is None  # n blank in size eqs (Kelly may fill n=)
    # size equations leave price blank
    assert "n= price=\n" in text or "n= price=\r\n" in text or "NL=35000 n= price=" in text
    assert re.search(r"(?m)^q=\d+", text) is None
    assert "q=48" not in text
    assert "target_shares" not in text


def test_nl_price_stop_fills_spread_no_integer_q():
    text = format_size_page(nl=100000, price=50.0, stop=45.0)
    assert "|entry-stop|=5" in text
    assert "price=50" in text
    assert "stop=45" in text
    assert re.search(r"(?m)^q=\d+", text) is None
    assert not re.search(r"\bq=\d{2,}\b", text)


def test_thin_path_no_kelly():
    text = format_size_page(nl=10_000, path={"n": 2, "note": "thin closed-fill sample"})
    assert "path: thin closed-fill sample — no Kelly" in text
    assert "Kelly:" not in text
    assert "g(f*)" not in text
    assert "g_f" not in text


def test_none_path_is_thin():
    text = format_size_page(nl=1.0, path=None)
    assert "path: thin closed-fill sample — no Kelly" in text


def test_fat_path_b_not_1_g_star_unequal():
    p, b, kelly = 0.55, 2.0, 0.275
    path = {
        "n": 40,
        "p": p,
        "q": 1.0 - p,
        "b": b,
        "A": 2.0,
        "B": 1.0,
        "kelly": kelly,
        "g_f": 0.999,  # must not be used
    }
    text = format_size_page(nl=50_000, path=path)
    assert "Kelly:" in text
    assert "f*/2=" in text
    assert "g_f" not in text
    g_star = growth_unequal(p, b, kelly)
    assert g_star is not None
    even = p * math.log(1.0 + kelly) + (1.0 - p) * math.log(1.0 - kelly)
    assert abs(g_star - even) > 1e-9
    # printed g(f*) must be unequal growth, not even-money / path g_f
    m = re.search(r"g\(f\*\)=([^\s]+)", text)
    assert m is not None
    printed = float(m.group(1))
    assert abs(printed - g_star) < 1e-6
    assert abs(printed - 0.999) > 1e-6
    assert abs(printed - even) > 1e-6
    assert "f*/2=0.1375" in text


def test_growth_unequal_unit():
    g = growth_unequal(0.55, 2.0, 0.1)
    expect = 0.55 * math.log(1.0 + 2.0 * 0.1) + 0.45 * math.log(1.0 - 0.1)
    assert g is not None
    assert abs(g - expect) < 1e-12
    assert growth_unequal(0.5, 1.0, 0.0) is None
    assert growth_unequal(0.5, 1.0, 1.0) is None
    assert growth_unequal(0.5, 0.0, 0.1) is None
    assert growth_unequal(0.5, -1.0, 0.1) is None


def test_ceiling_line_when_max_risk_set():
    text = format_size_page(nl=20_000, max_risk_per_trade_pct=0.75)
    assert "ceiling: max_risk_per_trade_pct=0.75% of NL (existing gate only; not f)" in text


def test_no_target_shares_import():
    import abcxauto.size_fact as mod
    import inspect

    src = inspect.getsource(mod)
    assert "target_shares" not in src
