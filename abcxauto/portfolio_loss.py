"""KEEP-5A: portfolio USD defined-max-loss firewall.

Deterministic sum: defined max-loss(open lots) + working new-risk
+ candidate ≤ hard ``portfolio_cap_usd``. Over-cap new risk refuses.
Closers / closing_position / unprotected last-stop always allowed —
even at or over the cap. Unreadable max-loss on a counted row fail-closes
new risk (never invent $0). Mid / mark / last are not max-loss evidence.

No IBKR import. Unit-testable with plain dicts.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

PORTFOLIO_CAP_USD_DEFAULT = 800.0
PORTFOLIO_CAP_KEY = "portfolio_cap_usd"
PORTFOLIO_CAP_ENV = "ABCXAUTO_PORTFOLIO_CAP_USD"

REASON_PORTFOLIO_USD = "portfolio_usd_max_loss"
REASON_PORTFOLIO_USD_UNREADABLE = "portfolio_usd_unreadable"
REASON_PORTFOLIO_USD_CAP = "portfolio_usd_cap_unreadable"

_UNSET = object()

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


def defined_max_loss_usd(row: Any) -> float | None:
    """Prefer structure (width − credit). Else explicit defined max-loss.

    Mid / marketValue / last are never evidence. None = unreadable.
    """
    if not isinstance(row, dict):
        return None
    structured = _structure_max_loss_usd(row)
    if structured is not None:
        return structured
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
) -> tuple[float | None, bool]:
    """Return (sum, unreadable). skip_exits drops working last-stop / exit."""
    total = 0.0
    for row in rows:
        if skip_exits and is_working_exit_or_last_stop(row):
            continue
        qty = _qty_of(_params_of(row))
        if qty == 0:
            continue
        loss = defined_max_loss_usd(row)
        if loss is None:
            return None, True
        total += float(loss)
    return total, False


def resolve_portfolio_cap_usd(
    raw: Any = _UNSET,
    *,
    present: bool | None = None,
) -> dict[str, Any]:
    """Resolve the hard USD cap.

    Unset → default 800 (gate ON). Present + finite ≥ 0 → use it, clamped
    so it cannot raise above 800. Present + garbage / non-finite →
    unreadable (fail-closed for new risk). Absent=off is rejected.
    """
    if raw is _UNSET:
        saw = False if present is None else bool(present)
        value = None
    else:
        saw = True if present is None else bool(present)
        value = raw
    if not saw:
        return {
            "ok": True,
            "cap": PORTFOLIO_CAP_USD_DEFAULT,
            "unreadable": False,
            "defaulted": True,
        }
    if value in (None, ""):
        return {
            "ok": False,
            "cap": None,
            "unreadable": True,
            "defaulted": False,
        }
    n = _finite(value)
    if n is None or n < 0:
        return {
            "ok": False,
            "cap": None,
            "unreadable": True,
            "defaulted": False,
        }
    cap = min(float(n), PORTFOLIO_CAP_USD_DEFAULT)
    return {
        "ok": True,
        "cap": cap,
        "unreadable": False,
        "defaulted": False,
    }


def coerce_portfolio_cap_usd(value: Any) -> float:
    """Operator persist path: finite ≥ 0, cannot raise above the default."""
    resolved = resolve_portfolio_cap_usd(value, present=True)
    if not resolved["ok"] or resolved["cap"] is None:
        raise ValueError("portfolio_cap_usd unreadable")
    return float(resolved["cap"])


def load_portfolio_cap_raw() -> tuple[Any, bool]:
    """Raw operator-disk / env value. present=True even when garbage.

    File key wins. Env is used only when the file omits the key. Missing
    both → (unset, False) so the gate defaults ON at $800.
    """
    try:
        from abcxauto.config import risk_settings_path

        path = risk_settings_path()
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and PORTFOLIO_CAP_KEY in raw:
                return raw.get(PORTFOLIO_CAP_KEY), True
    except Exception:
        logger.debug("portfolio cap file read failed", exc_info=True)
    if PORTFOLIO_CAP_ENV in os.environ:
        return os.environ.get(PORTFOLIO_CAP_ENV), True
    return _UNSET, False


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
    portfolio_cap_usd: Any = _UNSET,
    cap_present: bool | None = None,
) -> dict[str, Any]:
    """Pure firewall verdict. Plain dicts only.

    ``portfolio_max_loss_usd`` = Σ open + Σ working new-risk + candidate.
    Closers always ``allow``. Unreadable counted max-loss refuse new risk.
    """
    cap_info = resolve_portfolio_cap_usd(portfolio_cap_usd, present=cap_present)
    closer = is_portfolio_usd_closer(candidate)
    blob = {
        "allow": True,
        "portfolio_usd_refused": False,
        "portfolio_max_loss_usd": None,
        "portfolio_cap_usd": cap_info.get("cap"),
        "reason": "",
        "reason_code": "",
        "open_usd": None,
        "working_usd": None,
        "candidate_usd": None,
        "unreadable": False,
        "closer": closer,
        "cap_unreadable": bool(cap_info.get("unreadable")),
    }
    if closer:
        return blob

    if cap_info.get("unreadable") or not cap_info.get("ok"):
        blob["allow"] = False
        blob["portfolio_usd_refused"] = True
        blob["unreadable"] = True
        blob["reason_code"] = REASON_PORTFOLIO_USD_CAP
        blob["reason"] = "portfolio_cap_usd unreadable — fail-closed"
        return blob

    cap = float(cap_info["cap"])
    open_sum, open_bad = _sum_defined(_dict_rows(open_lots), skip_exits=False)
    work_sum, work_bad = _sum_defined(_dict_rows(working), skip_exits=True)
    cand_loss = defined_max_loss_usd(candidate) if isinstance(candidate, dict) else None
    cand_bad = cand_loss is None

    blob["open_usd"] = open_sum
    blob["working_usd"] = work_sum
    blob["candidate_usd"] = cand_loss

    if open_bad or work_bad or cand_bad:
        blob["allow"] = False
        blob["portfolio_usd_refused"] = True
        blob["unreadable"] = True
        blob["reason_code"] = REASON_PORTFOLIO_USD_UNREADABLE
        blob["reason"] = "portfolio defined max-loss unreadable — fail-closed"
        return blob

    total = float(open_sum or 0.0) + float(work_sum or 0.0) + float(cand_loss or 0.0)
    blob["portfolio_max_loss_usd"] = total
    blob["portfolio_cap_usd"] = cap
    if total > cap:
        blob["allow"] = False
        blob["portfolio_usd_refused"] = True
        blob["reason_code"] = REASON_PORTFOLIO_USD
        blob["reason"] = f"{REASON_PORTFOLIO_USD} {total} > {cap}"
        return blob
    return blob


def stamp_portfolio_usd(out: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """Copy the journal / block-blob fields onto a preview or place result."""
    out["portfolio_max_loss_usd"] = check.get("portfolio_max_loss_usd")
    out["portfolio_cap_usd"] = check.get("portfolio_cap_usd")
    out["portfolio_usd_refused"] = bool(check.get("portfolio_usd_refused"))
    return out


def portfolio_usd_block_blob(check: dict[str, Any], act: Any = None) -> dict[str, Any]:
    """Fail-closed place / preview-pass-as-place refuse. Never writes."""
    strat = _strategy_of(act) or "blocked"
    reason = str(check.get("reason") or check.get("reason_code") or REASON_PORTFOLIO_USD)
    code = str(check.get("reason_code") or REASON_PORTFOLIO_USD)
    out = {
        "status": "blocked",
        "reason_code": code,
        "note": reason,
        "strategy": strat,
        "would_refuse": [code],
        "pass": False,
        "refuse": True,
    }
    stamp_portfolio_usd(out, check)
    return out


def live_portfolio_usd_check(
    act: Any,
    world: Any = None,
    snap: dict[str, Any] | None = None,
    *,
    open_lots: Any = None,
    working: Any = None,
    portfolio_cap_usd: Any = _UNSET,
    cap_present: bool | None = None,
) -> dict[str, Any]:
    """Check with book from context. Loads operator cap when unset."""
    lots, orders = book_rows_from_context(act, world=world, snap=snap)
    if open_lots is not None:
        lots = _dict_rows(open_lots)
    if working is not None:
        orders = _dict_rows(working)
    raw = portfolio_cap_usd
    present = cap_present
    if raw is _UNSET and isinstance(act, dict) and PORTFOLIO_CAP_KEY in act:
        raw = act.get(PORTFOLIO_CAP_KEY)
        present = True
    if raw is _UNSET:
        raw, present = load_portfolio_cap_raw()
    return portfolio_usd_check(
        open_lots=lots,
        working=orders,
        candidate=act,
        portfolio_cap_usd=raw,
        cap_present=present,
    )


def portfolio_usd_place_block(
    act: Any,
    world: Any = None,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """None if place may proceed. A dict is fail-closed — never write."""
    check = live_portfolio_usd_check(act, world=world, snap=snap)
    if not check.get("portfolio_usd_refused"):
        if isinstance(act, dict):
            stamp_portfolio_usd(act, check)
        return None
    blob = portfolio_usd_block_blob(check, act)
    _journal_portfolio_usd(act, check, source="place")
    return blob


def _journal_portfolio_usd(act: Any, check: dict[str, Any], *, source: str) -> None:
    try:
        from abcxauto.memory import get_journal

        params = _params_of(act)
        journal = get_journal()
        blob = {
            PORTFOLIO_CAP_KEY: check.get("portfolio_cap_usd"),
            "portfolio_max_loss_usd": check.get("portfolio_max_loss_usd"),
            "portfolio_usd_refused": bool(check.get("portfolio_usd_refused")),
        }
        merged = dict(params)
        merged.update(blob)
        pid = journal.record_proposal(
            source=f"portfolio_usd:{source}",
            strategy=_strategy_of(act),
            symbol=str(params.get("symbol") or ""),
            direction=str(params.get("direction") or params.get("action") or ""),
            quantity=params.get("quantity"),
            params=merged,
            validation_ok=not check.get("portfolio_usd_refused"),
            validation_reason=str(check.get("reason") or check.get("reason_code") or ""),
        )
        if pid is not None:
            journal.record_gate_decision(
                pid,
                not check.get("portfolio_usd_refused"),
                str(check.get("reason") or check.get("reason_code") or ""),
            )
    except Exception:
        logger.debug("portfolio usd journal failed", exc_info=True)


__all__ = [
    "PORTFOLIO_CAP_ENV",
    "PORTFOLIO_CAP_KEY",
    "PORTFOLIO_CAP_USD_DEFAULT",
    "REASON_PORTFOLIO_USD",
    "REASON_PORTFOLIO_USD_CAP",
    "REASON_PORTFOLIO_USD_UNREADABLE",
    "book_rows_from_context",
    "coerce_portfolio_cap_usd",
    "defined_max_loss_usd",
    "is_new_risk_ticket",
    "is_portfolio_usd_closer",
    "is_working_exit_or_last_stop",
    "live_portfolio_usd_check",
    "load_portfolio_cap_raw",
    "portfolio_usd_block_blob",
    "portfolio_usd_check",
    "portfolio_usd_place_block",
    "resolve_portfolio_cap_usd",
    "stamp_portfolio_usd",
]
