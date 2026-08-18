"""Path math from the Orion post: expectancy, vol, ruin, Kelly, geometric growth.

Facts only. Grok owns size via send / self_tune. Clerk does not retune from these.
"""

from __future__ import annotations

import math
from typing import Any


def _mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return sum(xs) / len(xs)


def _var(xs: list[float]) -> float | None:
    mu = _mean(xs)
    if mu is None or len(xs) < 2:
        return None
    return sum((x - mu) ** 2 for x in xs) / len(xs)


def _round(v: float | None, n: int = 4) -> float | None:
    if v is None:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(float(v), n)


def path_facts(
    pnls: list[float] | None,
    *,
    equity: float | None,
    risk_pct: float | None,
) -> dict[str, Any]:
    """Five numbers from closed-fill dollars plus current risk lever."""
    try:
        f = float(risk_pct or 0) / 100.0
    except (TypeError, ValueError):
        f = 0.0
    if f <= 0:
        f = None
    n_units = (1.0 / f) if f else None
    out: dict[str, Any] = {
        "n": 0,
        "f": _round(f, 4),
        "N": _round(n_units, 2),
    }
    xs: list[float] = []
    for raw in pnls or []:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v != v:
            continue
        xs.append(v)
    out["n"] = len(xs)
    if len(xs) < 4:
        out["note"] = "thin closed-fill sample"
        return out
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x < 0]
    p = len(wins) / len(xs)
    q = 1.0 - p
    a = _mean(wins)
    b = abs(_mean(losses) or 0.0) if losses else None
    e = _mean(xs)
    var = _var(xs)
    sig = math.sqrt(var) if var is not None and var >= 0 else None
    snr = (e / sig) if e is not None and sig else None
    net_odds = (a / b) if a and b else None
    kelly = None
    if net_odds and net_odds > 0 and p > 0:
        kelly = (net_odds * p - q) / net_odds
        if kelly != kelly:
            kelly = None
    ruin = None
    if p > 0 and q > 0 and n_units and n_units > 0:
        if p <= q:
            ruin = 1.0
        else:
            ruin = (q / p) ** n_units
    g_f = None
    g_kelly = None
    if p > 0 and f is not None and 0 < f < 1:
        try:
            g_f = p * math.log(1.0 + f) + q * math.log(1.0 - f)
        except ValueError:
            g_f = None
    if p > 0 and kelly is not None and 0 < kelly < 1:
        try:
            g_kelly = p * math.log(1.0 + kelly) + q * math.log(1.0 - kelly)
        except ValueError:
            g_kelly = None
    g_approx = None
    eq = None
    try:
        eq = float(equity) if equity is not None else None
    except (TypeError, ValueError):
        eq = None
    if eq and eq > 0 and e is not None and var is not None:
        mu = e / eq
        s2 = var / (eq * eq)
        g_approx = mu - 0.5 * s2
    out.update({
        "p": _round(p, 4),
        "q": _round(q, 4),
        "A": _round(a, 4),
        "B": _round(b, 4),
        "b": _round(net_odds, 4),
        "E": _round(e, 4),
        "sig": _round(sig, 4),
        "snr": _round(snr, 4),
        "ruin": _round(ruin, 4),
        "kelly": _round(kelly, 4),
        "g_f": _round(g_f, 6),
        "g_kelly": _round(g_kelly, 6),
        "g_approx": _round(g_approx, 6),
    })
    return out


def path_from_journal(
    journal: Any,
    *,
    equity: float | None,
    risk_pct: float | None,
) -> dict[str, Any]:
    pnls: list[float] = []
    fn = getattr(journal, "closed_fill_pnls", None) if journal is not None else None
    if callable(fn):
        try:
            pnls = list(fn() or [])
        except Exception:
            pnls = []
    return path_facts(pnls, equity=equity, risk_pct=risk_pct)
