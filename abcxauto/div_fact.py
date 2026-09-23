"""Layer-1 diversification measurement. Facts only. No tickets."""

from __future__ import annotations

import math
from typing import Any


def hhi_and_neff(weights: list[float]) -> tuple[float | None, float | None]:
    """H = sum w_i^2, N_eff = 1/H. Weights are deployed-capital shares that sum to ~1.

    Empty -> (None, None). One name -> (1.0, 1.0).
    """
    ws = [float(w) for w in weights if w is not None]
    if not ws:
        return None, None
    h = sum(w * w for w in ws)
    if h <= 0.0 or not math.isfinite(h):
        return None, None
    return float(h), float(1.0 / h)


def beta_from_returns(name_rets: list[float], spy_rets: list[float]) -> float | None:
    """beta = cov(name, spy) / var(spy) on paired returns. Need >= 20 pairs. var 0 -> None."""
    pairs: list[tuple[float, float]] = []
    for a, b in zip(name_rets or [], spy_rets or []):
        try:
            x, y = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        pairs.append((x, y))
    if len(pairs) < 20:
        return None
    n = float(len(pairs))
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs) / n
    var = sum((y - my) * (y - my) for _, y in pairs) / n
    if var == 0.0 or not math.isfinite(var) or not math.isfinite(cov):
        return None
    return float(cov / var)


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _sym(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _sec_type(pos: dict[str, Any]) -> str:
    return str(pos.get("secType") or pos.get("sec_type") or "").strip().upper()


def _qty(pos: dict[str, Any]) -> float:
    v = _finite(pos.get("quantity") if pos.get("quantity") is not None else pos.get("position"))
    return 0.0 if v is None else v


def _mv(pos: dict[str, Any]) -> float:
    raw = pos.get("marketValue")
    if raw is None:
        raw = pos.get("market_value")
    v = _finite(raw)
    return 0.0 if v is None else v


def _underlying(pos: dict[str, Any]) -> str:
    for key in ("underlying", "underSymbol"):
        su = _sym(pos.get(key))
        if su:
            return su
    return _sym(pos.get("symbol") or pos.get("ticker"))


def _right(pos: dict[str, Any]) -> str:
    r = str(pos.get("right") or "").strip().upper()
    if r in ("P", "PUT"):
        return "P"
    if r in ("C", "CALL"):
        return "C"
    return r


def _share_equiv(pos: dict[str, Any]) -> tuple[float, float]:
    """Return (stk, opt) share-equivalent for one lot."""
    qty = _qty(pos)
    if abs(qty) < 1e-12:
        return 0.0, 0.0
    st = _sec_type(pos)
    if st in ("CASH", "BAL", "MONEY"):
        return 0.0, 0.0
    right = _right(pos)
    is_opt = st in ("OPT", "FOP", "OPTION") or right in ("C", "P")
    if is_opt and right in ("C", "P"):
        if right == "P":
            return 0.0, -100.0 * qty
        return 0.0, 100.0 * qty
    return float(qty), 0.0


def _side(net_dir: float) -> str:
    if net_dir < 0:
        return "short"
    return "long"


def _pct_nl(mv: float, net_liq: float) -> float | None:
    if net_liq > 0 and math.isfinite(net_liq):
        return 100.0 * mv / net_liq
    return None


def _heat_line(held: set[str], heat_groups: list[list[str]] | None) -> str:
    if len(held) < 2:
        return "no pair"
    if not heat_groups:
        return "no pair"
    parts: list[str] = []
    for group in heat_groups:
        members = sorted({_sym(s) for s in (group or []) if _sym(s) in held})
        if len(members) < 2:
            continue
        parts.append("+".join(members))
    if not parts:
        return "no pair"
    return " | ".join(parts)


def diversification_facts(
    positions: list[Any] | None,
    *,
    net_liq: float,
    total_cash: float,
    betas: dict[str, float] | None = None,
    heat_groups: list[list[str]] | None = None,
) -> dict[str, Any]:
    """Aggregate open lots by underlying into concentration facts.

    positions: list of dicts with symbol, secType/sec_type, quantity/position,
    marketValue/market_value, right (C/P) optional.
    STK share-equiv = signed qty. OPT long put = -100*contracts, long call = +100*contracts.
    Aggregate by underlying. Cash is not a name.
    Weights for HHI are each name's |mv| / sum(|mv|) of held names only.
    """
    nl = _finite(net_liq) or 0.0
    cash = _finite(total_cash) or 0.0

    agg: dict[str, dict[str, float]] = {}
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        st = _sec_type(pos)
        if st in ("CASH", "BAL", "MONEY"):
            continue
        su = _underlying(pos)
        if not su:
            continue
        stk, opt = _share_equiv(pos)
        mv = _mv(pos)
        if abs(stk) < 1e-12 and abs(opt) < 1e-12 and abs(mv) < 1e-12:
            continue
        row = agg.setdefault(su, {"stk": 0.0, "opt": 0.0, "mv": 0.0})
        row["stk"] += stk
        row["opt"] += opt
        row["mv"] += mv

    rows: list[dict[str, Any]] = []
    for su in sorted(agg):
        rec = agg[su]
        stk = float(rec["stk"])
        opt = float(rec["opt"])
        mv = float(rec["mv"])
        net_dir = stk + opt
        if abs(stk) < 1e-12 and abs(opt) < 1e-12 and abs(mv) < 1e-12:
            continue
        pct = _pct_nl(mv, nl)
        rows.append(
            {
                "symbol": su,
                "side": _side(net_dir),
                "stk": stk,
                "opt": opt,
                "mv": mv,
                "pct_nl": pct,
                "net_dir": net_dir,
            }
        )

    rows.sort(key=lambda r: (-abs(float(r["mv"])), str(r["symbol"])))

    abs_mvs = [abs(float(r["mv"])) for r in rows]
    total_abs = sum(abs_mvs)
    if total_abs > 0:
        weights = [m / total_abs for m in abs_mvs]
    else:
        weights = []
    hhi, n_eff = hhi_and_neff(weights)

    top_symbol: str | None = None
    top_pct_nl: float | None = None
    if rows:
        top = rows[0]
        top_symbol = str(top["symbol"])
        top_pct_nl = top.get("pct_nl")

    undeployed_pct = _pct_nl(cash, nl)
    held = {str(r["symbol"]) for r in rows}
    heat = _heat_line(held, heat_groups)

    out: dict[str, Any] = {
        "names": len(rows),
        "rows": rows,
        "undeployed_usd": cash,
        "undeployed_pct_nl": undeployed_pct,
        "top_symbol": top_symbol,
        "top_pct_nl": top_pct_nl,
        "hhi": hhi,
        "n_eff": n_eff,
        "heat": heat,
    }
    if betas is not None:
        cleaned: dict[str, float] = {}
        for k, v in betas.items():
            su = _sym(k)
            fv = _finite(v)
            if su and fv is not None:
                cleaned[su] = float(fv)
        out["betas"] = cleaned
    return out


def _fmt_usd(v: float) -> str:
    return f"${int(round(v))}"


def _fmt_pct(v: float | None, *, digits: int) -> str:
    if v is None or not math.isfinite(v):
        return "n/a"
    return f"{v:.{digits}f}%NL"


def _fmt_net_dir(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        iv = int(round(v))
        return f"+{iv}" if iv >= 0 else str(iv)
    return f"+{v:g}" if v >= 0 else f"{v:g}"


def format_diversification(
    facts: dict[str, Any],
    *,
    board_line: str | None = None,
) -> str:
    """Multi-line fact string. Measurement only."""
    lines: list[str] = []
    names = int(facts.get("names") or 0)
    lines.append(f"book names={names}.")

    for row in facts.get("rows") or []:
        if not isinstance(row, dict):
            continue
        su = _sym(row.get("symbol"))
        if not su:
            continue
        side = str(row.get("side") or _side(float(row.get("net_dir") or 0)))
        bits = [f"{su} {side}"]
        stk = float(row.get("stk") or 0)
        opt = float(row.get("opt") or 0)
        if abs(stk) >= 1e-12:
            bits.append(f"stk={int(round(stk)) if abs(stk - round(stk)) < 1e-9 else stk:g}")
        if abs(opt) >= 1e-12:
            bits.append(f"opt={int(round(opt)) if abs(opt - round(opt)) < 1e-9 else opt:g}")
        mv = float(row.get("mv") or 0)
        bits.append(f"mv={_fmt_usd(mv)}")
        bits.append(_fmt_pct(row.get("pct_nl"), digits=2))
        bits.append(f"net_dir={_fmt_net_dir(float(row.get('net_dir') or 0))}")
        lines.append(" ".join(bits) + ".")

    cash = float(facts.get("undeployed_usd") or 0)
    undeployed_pct = facts.get("undeployed_pct_nl")
    lines.append(f"undeployed {_fmt_usd(cash)} {_fmt_pct(undeployed_pct, digits=1)}.")

    top = facts.get("top_symbol")
    if top:
        lines.append(f"top={_sym(top)} {_fmt_pct(facts.get('top_pct_nl'), digits=2)}.")

    hhi = facts.get("hhi")
    n_eff = facts.get("n_eff")
    if hhi is not None and n_eff is not None:
        lines.append(f"H={float(hhi):.3f} N_eff={float(n_eff):.2f}.")
    else:
        lines.append("H=n/a N_eff=n/a.")

    beta_map = facts.get("betas")
    if isinstance(beta_map, dict):
        for su in sorted(beta_map):
            bv = _finite(beta_map[su])
            if bv is None:
                continue
            lines.append(f"beta {su}={bv:.2f}.")

    heat = str(facts.get("heat") or "no pair")
    lines.append(f"heat={heat}.")

    if board_line:
        lines.append(str(board_line))

    return "\n".join(lines)
