"""Size page: equations and filled inputs. Model weighs f, n, k, sigma_target.

Never solves for integer share count. Never defaults f or n.
"""

from __future__ import annotations

from typing import Any

from abcxauto.path_math import growth_unequal


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e12:
        return str(int(round(v)))
    s = f"{v:.6g}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_g(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:.6g}"


def _fmt_sigma(v: float | None) -> str:
    """Annual vol as a decimal fraction; label unit in the blank line."""
    if v is None:
        return ""
    return f"{_fmt(v)} (annual)"


def _entry_stop_abs(
    *,
    entry: float | None,
    stop: float | None,
    price: float | None,
) -> float | None:
    if stop is None:
        return None
    if entry is not None:
        return abs(entry - stop)
    if price is not None:
        return abs(price - stop)
    return None


def _path_thin(path: dict[str, Any] | None) -> bool:
    if path is None:
        return True
    try:
        n = int(path.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 4:
        return True
    note = str(path.get("note") or "").lower()
    return "thin" in note


def _path_b(path: dict[str, Any]) -> float | None:
    b = _finite(path.get("b"))
    if b is not None and b > 0:
        return b
    a = _finite(path.get("A"))
    loss = _finite(path.get("B"))
    if a is not None and loss is not None and loss > 0 and a > 0:
        return a / loss
    return None


def _kelly_block(path: dict[str, Any]) -> list[str]:
    p = _finite(path.get("p"))
    b = _path_b(path)
    kelly = _finite(path.get("kelly"))
    if p is None or b is None or kelly is None:
        return ["path: thin closed-fill sample — no Kelly"]
    q = _finite(path.get("q"))
    if q is None:
        q = 1.0 - p
    n = path.get("n")
    try:
        n_i = int(n) if n is not None else None
    except (TypeError, ValueError):
        n_i = None
    g_star = growth_unequal(p, b, kelly)
    half = kelly / 2.0
    lines = [
        "Kelly: f* = (b*p - q) / b   with q = 1-p, b = avg win / avg loss",
        f"  n={_fmt(float(n_i) if n_i is not None else None)} "
        f"p={_fmt(p)} q={_fmt(q)} b={_fmt(b)} f*={_fmt(kelly)}",
        f"  f*/2={_fmt(half)}",
        "g(f) = p*ln(1+b*f) + (1-p)*ln(1-f)",
        f"  g(f*)={_fmt_g(g_star)}",
        "note: full Kelly is not shares",
    ]
    return lines


def format_size_page(
    *,
    nl,
    price=None,
    entry=None,
    stop=None,
    atr=None,
    k_atr=None,
    sigma_annual=None,
    sigma_target=None,
    max_loss_per_contract=None,
    max_risk_per_trade_pct=None,
    path=None,
) -> str:
    """Print size equations with filled inputs. Never solve for integer q."""
    # k_atr / sigma_target stay blank for the model; never defaulted or filled.
    _ = (k_atr, sigma_target)

    nl_v = _finite(nl)
    if nl_v is not None and nl_v <= 0:
        nl_v = None
    price_v = _finite(price)
    if price_v is not None and price_v <= 0:
        price_v = None
    entry_v = _finite(entry)
    stop_v = _finite(stop)
    atr_v = _finite(atr)
    if atr_v is not None and atr_v <= 0:
        atr_v = None
    sigma_v = _finite(sigma_annual)
    if sigma_v is not None and sigma_v <= 0:
        sigma_v = None
    max_loss_v = _finite(max_loss_per_contract)
    if max_loss_v is not None and max_loss_v <= 0:
        max_loss_v = None
    risk_pct_v = _finite(max_risk_per_trade_pct)

    spread = _entry_stop_abs(entry=entry_v, stop=stop_v, price=price_v)

    nl_s = _fmt(nl_v)
    price_s = _fmt(price_v)
    entry_s = _fmt(entry_v)
    stop_s = _fmt(stop_v)
    spread_s = _fmt(spread)
    atr_s = _fmt(atr_v)
    sigma_s = _fmt_sigma(sigma_v)
    max_loss_s = _fmt(max_loss_v)

    lines = [
        "size:",
        "q = floor(NL * f / |entry - stop|)",
        f"  NL={nl_s} f= entry={entry_s} stop={stop_s} |entry-stop|={spread_s}",
        "q = floor(NL * n / price)",
        f"  NL={nl_s} n= price={price_s}",
        "q = floor(NL * f / max_loss_per_contract)",
        f"  NL={nl_s} f= max_loss_per_contract={max_loss_s}",
        "|entry - stop| = k * ATR",
        f"  k= ATR={atr_s}",
        "q = floor(NL * sigma_target / (price * sigma * sqrt(252)))",
        f"  NL={nl_s} sigma_target= price={price_s} sigma={sigma_s}",
        "symbols: NL=net_liq; f,n,k,sigma_target=blanks the model weighs; "
        "q is not solved here",
    ]

    if risk_pct_v is not None:
        lines.append(
            f"ceiling: max_risk_per_trade_pct={_fmt(risk_pct_v)}% of NL "
            f"(existing gate only; not f)"
        )

    path_dict = path if isinstance(path, dict) else None
    if _path_thin(path_dict):
        lines.append("path: thin closed-fill sample — no Kelly")
    else:
        assert path_dict is not None
        lines.extend(_kelly_block(path_dict))

    return "\n".join(lines) + "\n"
