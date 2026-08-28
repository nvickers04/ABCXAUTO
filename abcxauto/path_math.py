"""Path math from the Orion post: expectancy, vol, ruin, Kelly, geometric growth.

Facts only. Grok owns size via send / self_tune. Clerk does not retune from these.

Option / vertical cash is signed fill premium: debit negative, credit positive.
A last / mid / mark is not a fill. Qty-blind premium is not cash.
"""

from __future__ import annotations

import math
from typing import Any

_FILL_PX_KEYS = (
    "avg_fill_price",
    "fill_price",
    "avgFillPrice",
    "avgPrice",
    "avg_price",
)
_QTY_KEYS = ("quantity", "qty", "shares", "filled_quantity")
_BUY = frozenset({"BUY", "BOT"})
_SELL = frozenset({"SELL", "SLD"})
_OPT_SEC = frozenset({"OPT", "FOP", "BAG"})


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


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _row_qty(row: dict[str, Any]) -> float | None:
    for key in _QTY_KEYS:
        if row.get(key) is not None:
            return _finite(row.get(key))
    return None


def _side_sign(row: dict[str, Any], qty: float) -> float | None:
    side = str(row.get("side") or row.get("action") or "").upper()
    if side in _BUY:
        return -1.0
    if side in _SELL:
        return 1.0
    # Signed qty only — unsigned +qty without a side would guess debit and
    # invert a credit vertical.
    if qty < -1e-9:
        return 1.0
    return None


def _multiplier(row: dict[str, Any]) -> float:
    raw = _finite(row.get("multiplier"))
    if raw is not None and raw > 0:
        return raw
    sec = str(row.get("sec_type") or row.get("secType") or row.get("sec") or "").upper()
    if sec in _OPT_SEC or row.get("strike") is not None or row.get("right"):
        return 100.0
    return 1.0


def _looks_like_fill(row: dict[str, Any]) -> bool:
    if row.get("exec_id") not in (None, ""):
        return True
    if any(row.get(k) is not None for k in _FILL_PX_KEYS):
        return True
    side = str(row.get("side") or row.get("action") or "").upper()
    return side in _BUY or side in _SELL


def _fill_price(row: dict[str, Any]) -> float | None:
    """Execution price only. last / mid / mark / close are not fills."""
    for key in _FILL_PX_KEYS:
        if row.get(key) is None:
            continue
        px = _finite(row.get(key))
        if px is not None and px >= 0:
            return px
    if row.get("price") is not None and _looks_like_fill(row):
        px = _finite(row.get("price"))
        if px is not None and px >= 0:
            return px
    return None


def signed_premium_usd(row: dict[str, Any] | None) -> float | None:
    """Cash of one fill print. Debit negative, credit positive.

    Requires qty and a fill price. A last-only row is not cash.
    OPT / FOP / BAG use multiplier (default 100).
    """
    if not isinstance(row, dict):
        return None
    qty = _row_qty(row)
    if qty is None or abs(qty) <= 1e-9:
        return None
    px = _fill_price(row)
    if px is None:
        return None
    sign = _side_sign(row, qty)
    if sign is None:
        return None
    return sign * abs(qty) * px * _multiplier(row)


_BID_KEYS = ("bid", "nbbo_bid", "bid_at_send", "send_bid")
_ASK_KEYS = ("ask", "nbbo_ask", "ask_at_send", "send_ask")


def commission_cost(row: dict[str, Any] | None) -> float:
    """Broker fee as a positive cost. Missing is 0, not a guess."""
    if not isinstance(row, dict):
        return 0.0
    raw = _finite(row.get("commission"))
    if raw is None:
        return 0.0
    return abs(raw)


def net_realized_usd(row: dict[str, Any] | None) -> float | None:
    """IBKR realized minus commission. None when realized is missing."""
    if not isinstance(row, dict):
        return None
    pnl = _finite(row.get("realized_pnl"))
    if pnl is None:
        return None
    return pnl - commission_cost(row)


def _quote_px(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        px = _finite(row.get(key))
        if px is not None and px > 0:
            return px
    return None


def quote_bid_ask(row: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """NBBO / send-side quotes on a fill or send row. Missing stays None."""
    if not isinstance(row, dict):
        return None, None
    return _quote_px(row, _BID_KEYS), _quote_px(row, _ASK_KEYS)


def conservative_px(row: dict[str, Any] | None) -> float | None:
    """Debit at ask / credit at bid, or the worse of fill vs that NBBO.

    A mid fill inside the spread marks to the far side. A fill already
    through the far side keeps the fill. No quote side means None — a
    paper TWS mid is not a conservative print.
    """
    if not isinstance(row, dict):
        return None
    qty = _row_qty(row)
    sign = _side_sign(row, qty if qty is not None else 1.0)
    if sign is None:
        return None
    fill = _fill_price(row)
    bid, ask = quote_bid_ask(row)
    if sign < 0:
        if ask is None:
            return None
        return max(fill, ask) if fill is not None else ask
    if bid is None:
        return None
    return min(fill, bid) if fill is not None else bid


def conservative_premium_usd(row: dict[str, Any] | None) -> float | None:
    """Signed cash at the conservative print. Debit negative, credit positive."""
    if not isinstance(row, dict):
        return None
    qty = _row_qty(row)
    if qty is None or abs(qty) <= 1e-9:
        return None
    px = conservative_px(row)
    if px is None:
        return None
    sign = _side_sign(row, qty)
    if sign is None:
        return None
    return sign * abs(qty) * px * _multiplier(row)


def conservative_trade_pnl(fills: list[Any] | None) -> float | None:
    """Round-trip conservative cash minus commissions.

    Needs at least two qty-bearing fills (entry and exit), each with a
    quote side. A single closer is not a mark. Missing quotes return None
    rather than falling back to a paper mid.
    """
    total = 0.0
    n = 0
    for raw in fills or []:
        if not isinstance(raw, dict):
            continue
        qty = _row_qty(raw)
        if qty is None or abs(qty) <= 1e-9:
            continue
        prem = conservative_premium_usd(raw)
        if prem is None:
            return None
        total += prem - commission_cost(raw)
        n += 1
    return total if n >= 2 else None


def net_signed_premium(legs: list[Any] | None) -> float | None:
    """Net cash of a vertical / combo. Each wing must be a fill, not a last."""
    total = 0.0
    n = 0
    for leg in legs or []:
        cash = signed_premium_usd(leg if isinstance(leg, dict) else None)
        if cash is None:
            return None
        total += cash
        n += 1
    return total if n >= 2 else None


def _order_key(row: dict[str, Any]) -> Any:
    oid = row.get("order_id")
    if oid is None or oid == "":
        return None
    try:
        n = int(oid)
    except (TypeError, ValueError):
        return str(oid)
    return None if n == 0 else n


def _closed_fill_pnl(row: dict[str, Any]) -> float | None:
    """Closed-fill dollars. realized_pnl only — never last or a quote price.

    Qty-blind rows (missing / zero qty) are not fills.
    """
    pnl = _finite(row.get("realized_pnl"))
    if pnl is None or abs(pnl) <= 1e-9:
        return None
    qty = _row_qty(row)
    if qty is None or abs(qty) <= 1e-9:
        return None
    return pnl


def path_pnls_from_rows(rows: list[Any] | None) -> list[float]:
    """Closed-fill path samples. One number per ticket — vertical legs net.

    Bare floats stay one sample each (journal already realized dollars).
    Fill dicts use realized_pnl + qty, grouped by order_id so a BAG debit
    and its credit wing are one signed close, not a win plus a loss.
    """
    grouped: dict[Any, float] = {}
    singles: list[float] = []
    for raw in rows or []:
        if isinstance(raw, dict):
            pnl = _closed_fill_pnl(raw)
            if pnl is None:
                continue
            key = _order_key(raw)
            if key is None:
                singles.append(pnl)
            else:
                grouped[key] = grouped.get(key, 0.0) + pnl
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v != v:
            continue
        singles.append(v)
    out = [v for v in grouped.values() if abs(v) > 1e-9]
    out.extend(singles)
    return out


def path_facts(
    pnls: list[Any] | None,
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
    xs = path_pnls_from_rows(pnls)
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

    def _pct_of_nl(usd: float | None) -> float | None:
        if usd is None or not eq or eq <= 0:
            return None
        return _round(100.0 * float(usd) / float(eq), 4)

    out.update({
        "p": _round(p, 4),
        "q": _round(q, 4),
        "A": _round(a, 4),
        "A_pct_of_nl": _pct_of_nl(a),
        "B": _round(b, 4),
        "B_pct_of_nl": _pct_of_nl(b),
        "b": _round(net_odds, 4),
        "E": _round(e, 4),
        "E_pct_of_nl": _pct_of_nl(e),
        "sig": _round(sig, 4),
        "sig_pct_of_nl": _pct_of_nl(sig),
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
    pnls: list[Any] = []
    if journal is not None:
        for name in ("closing_fills", "closed_fill_pnls"):
            fn = getattr(journal, name, None)
            if not callable(fn):
                continue
            try:
                rows = list(fn() or [])
            except Exception:
                rows = []
            if rows:
                pnls = rows
                break
    return path_facts(pnls, equity=equity, risk_pct=risk_pct)
