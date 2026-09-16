"""Portfolio defined-max-loss math. Display only — never a place refuse.

Deterministic sum: defined max-loss(open lots) + working new-risk
+ candidate. Closers / closing_position / unprotected last-stop stay free.
Mid / mark / last are not max-loss evidence.

No IBKR import. Unit-testable with plain dicts.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Same new-risk set as agent_loop._NEW_RISK — copied so this module stays
# free of agent_loop / send / IBKR imports.
_NEW_RISK = frozenset({
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "butterfly",
    "straddle",
    "strangle",
    "calendar_spread",
    "diagonal_spread",
    "buy_option",
    "cash_secured_put",
    "ratio_spread",
    "jade_lizard",
    "bracket",
    "market_bracket",
})

_EXIT_OR_MANAGE = frozenset({
    "oca",
    "modify_stop",
    "modify_target",
    "cancel_order",
    "close_option",
    "trailing_stop",
    "trailing_stop_limit",
    "roll_option",
    "limit_order",
    "market_order",
    "stop_order",
    "stop_limit",
    "flatten",
    "flatten_all",
})

_EXIT_ROLES = frozenset({
    "exit",
    "last_stop",
    "last-stop",
    "cover",
    "protect",
    "stop",
    "close",
    "closing",
})

_STOP_TYPES = frozenset({"STP", "STP LMT", "TRAIL", "TRAIL LIMIT", "STOP", "STOP LIMIT"})

# Never treat these as defined max-loss.
_MARK_KEYS = frozenset({
    "mid",
    "mark",
    "last",
    "close",
    "bid",
    "ask",
    "lastPrice",
    "last_price",
    "marketValue",
    "market_value",
    "unrealizedPNL",
    "unrealized_pnl",
    "unrealizedPnl",
    "upnl",
    "uPnL",
})

_EXPLICIT_LOSS_KEYS = (
    "defined_max_loss",
    "max_loss_usd",
    "max_loss",
)


def _finite(raw: Any) -> float | None:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _strategy_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    params = row.get("params")
    if isinstance(params, dict):
        for key in ("strategy", "action", "type"):
            text = str(params.get(key) or "").strip().lower()
            if text:
                return text
    return str(row.get("strategy") or row.get("action") or row.get("type") or "").strip().lower()


def _params_of(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    params = row.get("params")
    if isinstance(params, dict):
        return dict(params)
    return dict(row)


def _qty_of(params: dict[str, Any]) -> int | None:
    raw = params.get("quantity")
    if raw in (None, ""):
        raw = params.get("qty") or params.get("shares") or params.get("contracts")
    if raw in (None, ""):
        raw = params.get("position")
    n = _finite(raw)
    if n is None:
        return None
    if abs(n) < 1e-9:
        return 0
    return int(abs(n)) if abs(n) >= 1 else None


def _row_signed_qty(row: dict[str, Any]) -> float:
    for key in ("quantity", "position", "qty"):
        n = _finite(row.get(key))
        if n is not None:
            return float(n)
    return 0.0


def _sec_type(row: dict[str, Any]) -> str:
    return str(
        row.get("secType") or row.get("sec_type") or row.get("sec") or ""
    ).strip().upper()


def _opt_exp_key(row: dict[str, Any]) -> str:
    raw = str(
        row.get("expiration") or row.get("lastTradeDateOrContractMonth") or ""
    ).replace("-", "")
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    if len(raw) == 6 and raw.isdigit():
        return "20" + raw
    return raw


def _is_opt_leg(row: dict[str, Any]) -> bool:
    sec = _sec_type(row)
    return sec.startswith("OPT") or sec == "FOP"


def _leg_premium_per_share(row: dict[str, Any]) -> float | None:
    raw = row.get("avg_cost")
    if raw is None:
        raw = row.get("avgCost") or row.get("averageCost")
    n = _finite(raw)
    if n is None:
        return None
    sec = _sec_type(row)
    if sec.startswith("OPT") and abs(n) >= 5.0:
        mkt = _finite(row.get("market_price") or row.get("marketPrice"))
        if mkt is None or abs(n) > abs(mkt) * 3:
            return abs(n) / 100.0
    return abs(n)


def _vertical_max_loss_from_legs(long_leg: dict[str, Any], short_leg: dict[str, Any]) -> float | None:
    qty_l = abs(_row_signed_qty(long_leg))
    qty_s = abs(_row_signed_qty(short_leg))
    qty = min(int(qty_l), int(qty_s))
    if qty <= 0:
        return None
    ls = _finite(long_leg.get("strike"))
    ss = _finite(short_leg.get("strike"))
    if ls is None or ss is None:
        return None
    width = abs(float(ss) - float(ls))
    long_p = _leg_premium_per_share(long_leg)
    short_p = _leg_premium_per_share(short_leg)
    if long_p is not None and short_p is not None:
        net = float(long_p) - float(short_p)
        if net >= 0:
            return net * 100.0 * qty
        credit = abs(net)
        return max(0.0, (width - credit) * 100.0 * qty)
    return width * 100.0 * qty


def _vertical_partner_leg(
    leg: dict[str, Any], pool: list[dict[str, Any]], used: set[int]
) -> dict[str, Any] | None:
    qty = _row_signed_qty(leg)
    if abs(qty) < 1e-9:
        return None
    sym = str(leg.get("symbol") or "").upper()
    exp = _opt_exp_key(leg)
    right = str(leg.get("right") or "")[:1].upper()
    strike = _finite(leg.get("strike"))
    if not sym or not exp or not right or strike is None:
        return None
    want = -1.0 if qty > 0 else 1.0
    best: dict[str, Any] | None = None
    best_dist: float | None = None
    best_idx: int | None = None
    for idx, p in enumerate(pool):
        if idx in used or not _is_opt_leg(p):
            continue
        pq = _row_signed_qty(p)
        if pq * want <= 0:
            continue
        if str(p.get("symbol") or "").upper() != sym:
            continue
        if _opt_exp_key(p) != exp or str(p.get("right") or "")[:1].upper() != right:
            continue
        ps = _finite(p.get("strike"))
        if ps is None or abs(float(ps) - float(strike)) < 1e-9:
            continue
        dist = abs(float(ps) - float(strike))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = p
            best_idx = idx
    if best is not None and best_idx is not None:
        used.add(best_idx)
    return best


def _synthetic_vertical_row(
    long_leg: dict[str, Any], short_leg: dict[str, Any]
) -> dict[str, Any]:
    qty = min(abs(int(_row_signed_qty(long_leg))), abs(int(_row_signed_qty(short_leg))))
    loss = _vertical_max_loss_from_legs(long_leg, short_leg)
    row: dict[str, Any] = {"params": {"quantity": qty}}
    if loss is not None:
        row["max_loss"] = loss
    return row


def _bag_has_strike_geometry(row: dict[str, Any]) -> bool:
    merged = dict(row)
    merged.update(_params_of(row))
    return (
        _finite(merged.get("long_strike")) is not None
        and _finite(merged.get("short_strike")) is not None
    )


def _normalize_loss_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Pair bare OPT legs into verticals. Skip unreadable BAG shells."""
    out: list[dict[str, Any]] = []
    skipped = False
    opt_pool: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _is_opt_leg(row) and abs(_row_signed_qty(row)) >= 1e-9:
            if _explicit_max_loss_usd(row) is not None:
                out.append(row)
                continue
            if _inferred_vertical_max_loss(row) is not None:
                out.append(row)
                continue
            opt_pool.append(row)
            continue
        sec = _sec_type(row)
        if sec == "BAG" and not _bag_has_strike_geometry(row):
            skipped = True
            continue
        out.append(row)
    used: set[int] = set()
    for idx, leg in enumerate(opt_pool):
        if idx in used:
            continue
        partner = _vertical_partner_leg(leg, opt_pool, used)
        if partner is None:
            skipped = True
            continue
        used.add(idx)
        long_leg, short_leg = (leg, partner) if _row_signed_qty(leg) > 0 else (partner, leg)
        out.append(_synthetic_vertical_row(long_leg, short_leg))
    return out, skipped


def is_new_risk_ticket(row: Any) -> bool:
    """True for a new structure. closing_position / exits are not new risk."""
    params = _params_of(row)
    if params.get("closing_position") is True:
        return False
    if params.get("last_stop") is True or params.get("is_last_stop") is True:
        return False
    if isinstance(row, dict):
        if row.get("closing_position") is True:
            return False
        if row.get("last_stop") is True or row.get("is_last_stop") is True:
            return False
    return _strategy_of(row) in _NEW_RISK


def is_portfolio_usd_closer(row: Any) -> bool:
    """closing_position / exit / unprotected last-stop — skip USD refuse."""
    return not is_new_risk_ticket(row)


def is_working_exit_or_last_stop(row: Any) -> bool:
    """Working exit / last-stop — exclude from the working new-risk sum."""
    if not isinstance(row, dict):
        return False
    params = _params_of(row)
    if params.get("closing_position") is True or row.get("closing_position") is True:
        return True
    if (
        params.get("last_stop") is True
        or params.get("is_last_stop") is True
        or row.get("last_stop") is True
        or row.get("is_last_stop") is True
    ):
        return True
    role = str(row.get("role") or params.get("role") or "").strip().lower()
    if role in _EXIT_ROLES:
        return True
    strat = _strategy_of(row)
    if strat in _EXIT_OR_MANAGE:
        return True
    ot = str(
        row.get("order_type") or row.get("orderType") or params.get("order_type") or ""
    ).strip().upper()
    if ot in _STOP_TYPES:
        return True
    return False


def _width(params: dict[str, Any], *keys: str) -> float | None:
    vals = [_finite(params.get(k)) for k in keys]
    if any(v is None for v in vals):
        return None
    return abs(float(vals[0]) - float(vals[1]))  # type: ignore[arg-type]


def _structure_max_loss_usd(row: Any) -> float | None:
    """Structure-defined dollars. None if the geometry cannot be read.

    Vertical: (width − credit) × 100 × qty. Mid / mark / last never used.
    """
    params = _params_of(row)
    qty = _qty_of(params)
    if qty == 0:
        return 0.0
    if qty is None:
        qty = 1
    limit = _finite(params.get("limit_price"))
    if limit is None:
        limit = _finite(params.get("credit"))
    if limit is None:
        limit = _finite(params.get("net_credit"))
    if limit is not None:
        limit = abs(limit)
    strat = _strategy_of(row)

    if strat == "vertical_spread":
        width = _width(params, "long_strike", "short_strike")
        if width is None:
            return None
        if limit is not None and limit < width:
            return (width - limit) * 100.0 * qty
        if limit is not None:
            return limit * 100.0 * qty
        return width * 100.0 * qty
    if strat == "iron_condor":
        put_w = _width(params, "put_short_strike", "put_long_strike")
        call_w = _width(params, "call_long_strike", "call_short_strike")
        if put_w is None or call_w is None:
            return None
        width = min(put_w, call_w)
        if limit is not None:
            return max(0.0, (width - limit) * 100.0 * qty)
        return width * 100.0 * qty
    if strat == "iron_butterfly":
        wing = _finite(params.get("wing_width"))
        if wing is None:
            return None
        if limit is not None:
            return max(0.0, (abs(wing) - limit) * 100.0 * qty)
        return abs(wing) * 100.0 * qty
    if strat == "butterfly":
        wing = _finite(params.get("wing_width"))
        if wing is None:
            wing = _width(params, "lower_strike", "upper_strike")
            if wing is not None:
                wing = wing / 2.0
        if wing is None:
            return None
        if limit is not None:
            return max(0.0, (abs(wing) - limit) * 100.0 * qty)
        return abs(wing) * 100.0 * qty
    if strat in ("bracket", "market_bracket"):
        stop = _finite(params.get("stop_price"))
        entry = _finite(params.get("entry_price")) or _finite(params.get("price_hint"))
        if stop is not None and entry is not None:
            return abs(entry - stop) * qty
        target = _finite(params.get("target_price"))
        if stop is not None and target is not None:
            return abs(target - stop) * qty
        return None
    if strat in ("buy_option", "cash_secured_put", "covered_call"):
        if limit is not None:
            return limit * 100.0 * qty
        return None
    return None


def _explicit_max_loss_usd(row: Any) -> float | None:
    """Declared defined max-loss. Ignores mid/mark keys."""
    if not isinstance(row, dict):
        return None
    params = _params_of(row)
    for src in (row, params):
        if not isinstance(src, dict):
            continue
        for key in _EXPLICIT_LOSS_KEYS:
            if key not in src:
                continue
            n = _finite(src.get(key))
            if n is None:
                continue
            return abs(n)
    return None


def _inferred_vertical_max_loss(row: dict[str, Any]) -> float | None:
    merged = dict(row)
    merged.update(_params_of(row))
    ls = _finite(merged.get("long_strike"))
    ss = _finite(merged.get("short_strike"))
    if ls is None or ss is None:
        return None
    limit = _finite(
        merged.get("limit_price")
        or merged.get("lmt")
        or merged.get("lmt_price")
        or merged.get("credit")
    )
    qty = _qty_of(merged)
    if qty is None:
        qty = 1
    fake = {
        "strategy": "vertical_spread",
        "params": {
            "long_strike": ls,
            "short_strike": ss,
            "limit_price": limit,
            "quantity": qty,
        },
    }
    return _structure_max_loss_usd(fake)


def defined_max_loss_usd(row: Any) -> float | None:
    """Prefer structure (width − credit). Else explicit defined max-loss.

    Mid / marketValue / last are never evidence. None = unreadable.
    """
    if not isinstance(row, dict):
        return None
    structured = _structure_max_loss_usd(row)
    if structured is not None:
        return structured
    inferred = _inferred_vertical_max_loss(row)
    if inferred is not None:
        return inferred
    return _explicit_max_loss_usd(row)


def _dict_rows(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(item)
    return out


def _sum_defined(
    rows: list[dict[str, Any]],
    *,
    skip_exits: bool,
) -> tuple[float, bool]:
    """Return (sum, partial_unreadable). skip_exits drops working last-stop / exit."""
    normalized, skipped = _normalize_loss_rows(_dict_rows(rows))
    total = 0.0
    partial = skipped
    for row in normalized:
        if skip_exits and is_working_exit_or_last_stop(row):
            continue
        qty = _qty_of(_params_of(row))
        if qty == 0:
            continue
        loss = defined_max_loss_usd(row)
        if loss is None:
            partial = True
            continue
        total += float(loss)
    return total, partial


def book_rows_from_context(
    act: Any = None,
    world: Any = None,
    snap: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Open lots + working orders from fixture / send attachments. No IBKR."""
    snap_d = snap if isinstance(snap, dict) else {}
    act_d = act if isinstance(act, dict) else {}
    lots = (
        snap_d.get("lots")
        or act_d.get("_open_lots")
        or snap_d.get("positions")
        or act_d.get("_live_positions")
        or getattr(world, "positions", None)
        or []
    )
    # Structured dict lots win over wake label strings.
    if not any(isinstance(x, dict) for x in (lots or [])):
        labeled = snap_d.get("open_lots") or act_d.get("open_lots")
        if any(isinstance(x, dict) for x in (labeled or [])):
            lots = labeled
    working = (
        snap_d.get("open_orders")
        or act_d.get("_open_orders")
        or getattr(world, "open_orders", None)
        or []
    )
    return _dict_rows(lots), _dict_rows(working)


def portfolio_usd_check(
    *,
    open_lots: Any = None,
    working: Any = None,
    candidate: Any = None,
) -> dict[str, Any]:
    """Display sum only. Plain dicts. Never refuses.

    ``portfolio_max_loss_usd`` = Σ open + Σ working new-risk + candidate.
    Closers stay free. Unreadable counted max-loss is a fact, not a refuse.
    """
    closer = is_portfolio_usd_closer(candidate)
    blob = {
        "allow": True,
        "portfolio_max_loss_usd": None,
        "open_usd": None,
        "working_usd": None,
        "candidate_usd": None,
        "unreadable": False,
        "closer": closer,
    }
    if closer:
        return blob

    open_sum, open_bad = _sum_defined(_dict_rows(open_lots), skip_exits=False)
    work_sum, work_bad = _sum_defined(_dict_rows(working), skip_exits=True)
    cand_loss = defined_max_loss_usd(candidate) if isinstance(candidate, dict) else None
    cand_bad = cand_loss is None

    blob["open_usd"] = open_sum
    blob["working_usd"] = work_sum
    blob["candidate_usd"] = cand_loss

    if cand_bad:
        blob["unreadable"] = True
        return blob

    total = float(open_sum) + float(work_sum) + float(cand_loss or 0.0)
    blob["portfolio_max_loss_usd"] = total
    if open_bad or work_bad:
        blob["unreadable"] = True
    return blob


def stamp_portfolio_max_loss(out: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """Copy aggregate defined-risk onto a preview/place blob."""
    out["portfolio_max_loss_usd"] = check.get("portfolio_max_loss_usd")
    return out


def live_portfolio_usd_check(
    act: Any,
    world: Any = None,
    snap: dict[str, Any] | None = None,
    *,
    open_lots: Any = None,
    working: Any = None,
) -> dict[str, Any]:
    """Check with book from context."""
    lots, orders = book_rows_from_context(act, world=world, snap=snap)
    if open_lots is not None:
        lots = _dict_rows(open_lots)
    if working is not None:
        orders = _dict_rows(working)
    return portfolio_usd_check(
        open_lots=lots,
        working=orders,
        candidate=act,
    )


__all__ = [
    "book_rows_from_context",
    "defined_max_loss_usd",
    "is_new_risk_ticket",
    "is_portfolio_usd_closer",
    "is_working_exit_or_last_stop",
    "live_portfolio_usd_check",
    "portfolio_usd_check",
    "stamp_portfolio_max_loss",
]
