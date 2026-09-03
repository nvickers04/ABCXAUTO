"""Clerk: simple entry idea → protected structure, and protection vs the book.

Grok owns the ticket. Code must not invent omitted stop / target / entry /
price_hint / quantity from last, quote, 1% posture bands, or session high/low.
``fill_missing_protection`` is a standing no-op so a thin send is refused.

The second half of the module is the reverse question: does a working exit
still cover an open lot? A protective order that outlives its position is not
untidy, it is a naked entry waiting for a print — a stale SELL on a flat book
sells stock the account does not own. ``orphaned_protection_rows`` answers
"provably flat" conservatively (anything it cannot identify counts as still
covering), and ``last_stop_block_reason`` is the shared last-stop rule so the
cancel gate and the reconciler can never disagree about what is load-bearing.
A last-stop is exit-side size that covers the lot; a crumb or a wrong-side
leftover after flatten is not.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_NAKED_OPEN = frozenset({"market_order", "limit_order", "stop_order"})
# Same slack as trade_plan stacked-stop cover and the protection report.
_COVER_QTY_SLACK = 0.51
_FILL_SIDES = {"BUY": "BUY", "BOT": "BUY", "SELL": "SELL", "SLD": "SELL"}
_STOP_TYPES = frozenset({"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"})

# IBKR 10147: "OrderId that needs to be cancelled is not found / does not exist".
# 2026-09-03 09:08 paper 7497: nest retried NVDA STP oid 4 every cycle; 72× 10147
# starved the look (0 model calls, send_calls=1). Once gone, never re-queue.
PERMANENT_CANCEL_GONE_CODES = frozenset({10147})
# Transient cancel failures (disconnect, timeout, TWS busy) retry this many
# times per order_id, then that id is skipped for the rest of the process.
TRANSIENT_CANCEL_RETRY_LIMIT = 3
# Hard cap so a new error code cannot eat the look: after this many cancel
# *attempts* or this many seconds of protection maintenance, remaining
# orphans wait and GROK think runs.
MAX_PROTECTION_CANCELS_PER_LOOK = 8
MAX_PROTECTION_CANCEL_BUDGET_S = 2.0

_cancel_gone_oids: set[int] = set()
_cancel_gone_logged: set[int] = set()
_transient_cancel_fails: dict[int, int] = {}
_transient_logged: set[int] = set()
_look_cancel_attempts = 0
_look_budget_t0: float | None = None
_look_budget_logged = False
_look_budget_active = False


def reset_cancel_guard_for_tests() -> None:
    """Drop process-life cancel state. Tests only."""
    global _look_cancel_attempts, _look_budget_t0, _look_budget_logged
    global _look_budget_active
    _cancel_gone_oids.clear()
    _cancel_gone_logged.clear()
    _transient_cancel_fails.clear()
    _transient_logged.clear()
    _look_cancel_attempts = 0
    _look_budget_t0 = None
    _look_budget_logged = False
    _look_budget_active = False


def _as_oid(order_id: Any) -> int | None:
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    return oid if oid > 0 else None


def _error_text_means_gone(blob: str) -> bool:
    b = str(blob or "").lower()
    if "10147" in b:
        return True
    if "needs to be cancelled is not found" in b:
        return True
    if "not found" in b or "does not exist" in b or "no such order" in b:
        return True
    return False


def ibkr_error_means_cancel_gone(code: Any, message: str = "") -> bool:
    """True for 10147 and equivalent permanent 'order gone' cancel errors.

    Must not match unrelated IBKR 'not found' texts (e.g. 10168 market data).
    """
    try:
        c = int(code)
    except (TypeError, ValueError):
        c = -1
    if c in PERMANENT_CANCEL_GONE_CODES:
        return True
    blob = str(message or "").lower()
    if "10147" in blob or "needs to be cancelled is not found" in blob:
        return True
    if c == 10148:
        return any(
            s in blob
            for s in ("filled", "cancelled", "canceled", "inactive", "apicancelled")
        )
    return False


def cancel_result_means_gone(result: Any) -> bool:
    """True when a cancel_order return value means the id is gone at IBKR."""
    if not isinstance(result, dict):
        return False
    if result.get("order_gone") or result.get("already_gone"):
        return True
    if ibkr_error_means_cancel_gone(
        result.get("error_code") or result.get("code"),
        str(result.get("error") or result.get("message") or ""),
    ):
        return True
    return _error_text_means_gone(str(result.get("error") or ""))


def note_cancel_gone(
    order_id: Any, *, code: Any = None, detail: str = ""
) -> None:
    """Mark ``order_id`` dead for this process. Log once, loudly."""
    oid = _as_oid(order_id)
    if oid is None:
        return
    _cancel_gone_oids.add(oid)
    _transient_cancel_fails.pop(oid, None)
    if oid in _cancel_gone_logged:
        return
    _cancel_gone_logged.add(oid)
    why = detail or (f"IBKR {code}" if code is not None else "order not found")
    logger.error(
        "orphan-protection: order_id=%s is gone at IBKR (%s) — "
        "will not cancel again this process",
        oid,
        why,
    )


def note_cancel_transient_fail(order_id: Any, *, detail: str = "") -> int:
    """Count a non-gone cancel failure. At the bound, skip this id this process."""
    oid = _as_oid(order_id)
    if oid is None:
        return 0
    n = int(_transient_cancel_fails.get(oid, 0)) + 1
    _transient_cancel_fails[oid] = n
    if n >= TRANSIENT_CANCEL_RETRY_LIMIT and oid not in _transient_logged:
        _transient_logged.add(oid)
        logger.error(
            "orphan-protection: order_id=%s cancel failed %s times (%s) — "
            "stop retrying this process (transient bound=%s)",
            oid,
            n,
            detail or "transient",
            TRANSIENT_CANCEL_RETRY_LIMIT,
        )
    else:
        logger.warning(
            "orphan-protection: cancel %s failed (%s/%s): %s",
            oid,
            n,
            TRANSIENT_CANCEL_RETRY_LIMIT,
            detail or "transient",
        )
    return n


def note_cancel_ok(order_id: Any) -> None:
    oid = _as_oid(order_id)
    if oid is None:
        return
    _transient_cancel_fails.pop(oid, None)


def cancel_oid_is_blocked(order_id: Any) -> bool:
    """True when this process must not send another cancel for ``order_id``."""
    oid = _as_oid(order_id)
    if oid is None:
        return False
    if oid in _cancel_gone_oids:
        return True
    return int(_transient_cancel_fails.get(oid, 0)) >= TRANSIENT_CANCEL_RETRY_LIMIT


def begin_look_protection_budget(*, reset: bool = True) -> None:
    """Start (or inherit) the per-look cancel window."""
    global _look_cancel_attempts, _look_budget_t0, _look_budget_logged
    global _look_budget_active
    if _look_budget_active and not reset:
        return
    _look_cancel_attempts = 0
    _look_budget_t0 = None
    _look_budget_logged = False
    _look_budget_active = True


def end_look_protection_budget() -> None:
    global _look_budget_active
    _look_budget_active = False


def _log_budget_exhausted() -> None:
    global _look_budget_logged
    if _look_budget_logged:
        return
    _look_budget_logged = True
    elapsed = 0.0
    if _look_budget_t0 is not None:
        elapsed = time.monotonic() - _look_budget_t0
    logger.warning(
        "protection maintenance budget exhausted this look: %s cancel attempts "
        "/ %.2fs (cap %s / %.1fs) — GROK think runs; remaining orphans wait",
        _look_cancel_attempts,
        elapsed,
        MAX_PROTECTION_CANCELS_PER_LOOK,
        MAX_PROTECTION_CANCEL_BUDGET_S,
    )


def claim_protection_cancel(order_id: Any) -> bool:
    """Reserve one nest cancel. False → skip (blocked, or look budget gone)."""
    global _look_cancel_attempts, _look_budget_t0
    oid = _as_oid(order_id)
    if oid is None or cancel_oid_is_blocked(oid):
        return False
    if not _look_budget_active:
        begin_look_protection_budget()
    now = time.monotonic()
    if _look_budget_t0 is None:
        _look_budget_t0 = now
        elapsed = 0.0
    else:
        elapsed = now - _look_budget_t0
    # First attempt always goes; after that the wall-clock cap applies so a
    # 0s test budget still allows one cancel then yields to GROK think.
    if _look_cancel_attempts >= MAX_PROTECTION_CANCELS_PER_LOOK:
        _log_budget_exhausted()
        return False
    if _look_cancel_attempts > 0 and elapsed >= MAX_PROTECTION_CANCEL_BUDGET_S:
        _log_budget_exhausted()
        return False
    _look_cancel_attempts += 1
    return True


def _params(act: dict) -> dict[str, Any]:
    raw = act.get("params")
    if not isinstance(raw, dict):
        act["params"] = {}
        return act["params"]
    return raw


def _missing(params: dict[str, Any], key: str) -> bool:
    v = params.get(key)
    if v is None or v == "":
        return True
    if key == "quantity":
        try:
            return int(float(v)) <= 0
        except (TypeError, ValueError):
            return True
    return False


def _direction(params: dict[str, Any]) -> str:
    d = str(params.get("direction") or "").upper()
    if d in ("LONG", "SHORT"):
        return d
    action = str(params.get("action") or "").upper()
    if action == "BUY":
        return "LONG"
    if action == "SELL":
        return "SHORT"
    return ""


def _is_exit(act: dict, positions: list) -> bool:
    params = act.get("params") if isinstance(act.get("params"), dict) else {}
    if params.get("closing_position") is True:
        return True
    if act.get("target_conId") or params.get("conId") or params.get("con_id"):
        return True
    return False


def promote_naked_entry(act: dict, positions: list | None = None) -> bool:
    """Opening stock tickets become a bracket. Exits stay exits."""
    if not isinstance(act, dict):
        return False
    strat = str(act.get("strategy") or act.get("action") or "").strip().lower()
    if strat not in _NAKED_OPEN:
        return False
    if _is_exit(act, positions or []):
        return False
    params = _params(act)
    if not params.get("symbol") and act.get("symbol"):
        params["symbol"] = act["symbol"]
    if not str(params.get("symbol") or "").strip():
        return False
    direction = _direction(params)
    if not direction:
        return False
    params["direction"] = direction
    if strat == "limit_order" and params.get("limit_price") not in (None, ""):
        act["strategy"] = act["action"] = "bracket"
        if _missing(params, "entry_price"):
            params["entry_price"] = params["limit_price"]
    else:
        act["strategy"] = act["action"] = "market_bracket"
    return True


def fill_missing_protection(
    act: dict,
    *,
    quote_last: float | None,
    equity: float | None = None,
    posture: str = "balanced",
    cfg: Any = None,
    positions: list | None = None,
    session: Any = None,
) -> list[str]:
    """Grok owns the ticket. Omitted fields stay omitted.

    Never invent stop_price / target_price / entry_price / price_hint /
    quantity from last, quote, 1% posture bands, or session high/low.
    Send refuses a thin ticket; this function does not complete one.
    """
    _ = (act, quote_last, equity, posture, cfg, positions, session)
    return []


def _order_id_of(order: dict) -> int | None:
    raw = order.get("order_id")
    if raw is None:
        raw = order.get("orderId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _order_type_of(row: dict) -> str:
    return str(row.get("order_type") or row.get("orderType") or "").upper()


def _sec_bucket(row: dict) -> str:
    """STK / OPT / BAG bucket. Missing secType reads as STK, same as the book."""
    sec = str(
        row.get("sec_type") or row.get("secType") or row.get("sec") or "STK"
    ).upper()
    if sec in ("", "STK", "ETF"):
        return "STK"
    if sec.startswith("OPT") or sec == "FOP":
        return "OPT"
    return sec


def _con_id_of(row: dict) -> str:
    for key in ("conId", "con_id"):
        v = row.get(key)
        if v not in (None, "", 0, "0"):
            return str(v)
    return ""


def _signed_qty(row: dict) -> float:
    raw = row.get("quantity")
    if raw is None:
        raw = row.get("position")
    if raw is None:
        raw = row.get("totalQuantity")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_side(row: dict) -> str:
    """BUY/SELL, including IBKR fill aliases BOT/SLD. Unknown stays empty."""
    return _FILL_SIDES.get(
        str(row.get("action") or row.get("side") or "").strip().upper(), ""
    )


def _is_stk_stop(order: dict) -> bool:
    return _sec_bucket(order) == "STK" and _order_type_of(order) in _STOP_TYPES


def _held_stk_signed(positions: list | None, symbol: str) -> float:
    """Net STK/ETF quantity for symbol. 0 if none."""
    want = str(symbol or "").upper()
    if not want:
        return 0.0
    held = 0.0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if _sec_bucket(p) != "STK":
            continue
        if str(p.get("symbol") or "").upper() != want:
            continue
        held += _signed_qty(p)
    return held


def _stop_covers_held(order: dict, symbol: str, held: float) -> bool:
    """True when this working stop is exit-side and covers held STK qty."""
    if not isinstance(order, dict) or abs(held) < 1e-9:
        return False
    if str(order.get("symbol") or "").upper() != symbol:
        return False
    if not _is_stk_stop(order):
        return False
    side = _order_side(order)
    want = "SELL" if held > 0 else "BUY"
    if side and side != want:
        return False
    return abs(_signed_qty(order)) + 1e-9 >= abs(held) - _COVER_QTY_SLACK


def _contract_key(row: dict) -> tuple[Any, ...] | None:
    """Comparable identity, or None when the row cannot be pinned to a contract."""
    sym = str(row.get("symbol") or "").upper()
    if not sym:
        return None
    bucket = _sec_bucket(row)
    if bucket == "STK":
        return ("STK", sym)
    if bucket != "OPT":
        return None
    exp = str(
        row.get("expiration")
        or row.get("lastTradeDateOrContractMonth")
        or row.get("expiry")
        or ""
    ).replace("-", "")[-6:]
    right = str(row.get("right") or row.get("option_right") or "")[:1].upper()
    try:
        strike_s = f"{float(row.get('strike')):.4f}"
    except (TypeError, ValueError):
        strike_s = ""
    if not exp or not right or not strike_s:
        return None
    return ("OPT", sym, exp, right, strike_s)


def _open_lot_index(
    positions: list | None,
) -> tuple[set[str], set[tuple[Any, ...]], set[str]]:
    """conIds, contract keys, and symbols of lots we could not identify."""
    con_ids: set[str] = set()
    keys: set[tuple[Any, ...]] = set()
    opaque: set[str] = set()
    for p in positions or []:
        if not isinstance(p, dict):
            opaque.add("*")
            continue
        if abs(_signed_qty(p)) < 1e-9:
            continue
        cid = _con_id_of(p)
        if cid:
            con_ids.add(cid)
        key = _contract_key(p)
        if key is None:
            opaque.add(str(p.get("symbol") or "").upper() or "*")
        else:
            keys.add(key)
    return con_ids, keys, opaque


def order_covers_open_lot(order: dict, positions: list | None) -> bool:
    """True unless the contract this order trades is *provably* flat.

    Deliberately biased toward "still covering": an unreadable book, a combo,
    a lot we cannot fingerprint, or an order we cannot pin to a contract all
    answer True. Cancelling live protection is far worse than leaving a stale
    ticket for the next snapshot.
    """
    if positions is None or not isinstance(order, dict):
        return True
    sym = str(order.get("symbol") or "").upper()
    if not sym:
        return True
    if _sec_bucket(order) not in ("STK", "OPT"):
        return True
    con_ids, keys, opaque = _open_lot_index(positions)
    if "*" in opaque or sym in opaque:
        return True
    cid = _con_id_of(order)
    if cid and cid in con_ids:
        return True
    key = _contract_key(order)
    if key is None:
        return True
    return key in keys


def _is_bracket_child(order: dict) -> bool:
    """OCA group / parent id — proof the ticket was placed as protection."""
    for key in ("oca_group", "ocaGroup"):
        if str(order.get(key) or "").strip():
            return True
    for key in ("parent_id", "parentId"):
        try:
            if int(order.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def protective_role(order: dict) -> str:
    """``stop`` | ``bracket_leg`` | ``""`` — shapes that only exist as protection.

    Bare stops qualify because ``stop_order`` / ``trailing_stop`` are exit-only
    strategies here. A limit needs OCA/parent evidence: an unattached LMT is
    just as likely to be a resting entry.
    """
    if not isinstance(order, dict):
        return ""
    otype = _order_type_of(order)
    if not otype:
        return ""
    if any(hint in otype for hint in ("STP", "TRAIL", "STOP")):
        return "stop"
    if otype in ("LMT", "LIMIT", "LOC") and _is_bracket_child(order):
        return "bracket_leg"
    return ""


def _awaits_a_working_parent(order: dict, working_ids: set[int]) -> bool:
    """A child of an order that is still working protects a fill yet to come.

    ABCXAUTO places bracket protection only after the entry fills, so this is
    the operator's own TWS bracket: the lot is flat because the parent has not
    filled, and cancelling the child would leave that entry to fill naked.
    """
    for key in ("parent_id", "parentId"):
        try:
            parent = int(order.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if parent and parent in working_ids:
            return True
    return False


def orphaned_protection_rows(
    positions: list | None,
    open_orders: list | None,
    *,
    symbols: Any = None,
    actions: Any = None,
) -> list[dict[str, Any]]:
    """Working protective orders whose position is gone. Facts, one per id."""
    want = (
        {str(s).upper() for s in symbols if str(s or "").strip()}
        if symbols is not None
        else None
    )
    want_actions = (
        {str(a).upper() for a in actions if str(a or "").strip()}
        if actions is not None
        else None
    )
    working_ids = {
        oid
        for oid in (
            _order_id_of(o) for o in (open_orders or []) if isinstance(o, dict)
        )
        if oid is not None
    }
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for o in open_orders or []:
        if not isinstance(o, dict):
            continue
        sym = str(o.get("symbol") or "").upper()
        if want is not None and sym not in want:
            continue
        action = str(o.get("action") or o.get("side") or "").upper()
        if want_actions is not None and action not in want_actions:
            continue
        role = protective_role(o)
        if not role:
            continue
        if _awaits_a_working_parent(o, working_ids):
            continue
        if order_covers_open_lot(o, positions):
            continue
        oid = _order_id_of(o)
        if oid is None or oid in seen:
            continue
        if cancel_oid_is_blocked(oid):
            continue
        seen.add(oid)
        rows.append({
            "order_id": oid,
            "symbol": sym,
            "sec": _sec_bucket(o),
            "type": _order_type_of(o),
            "action": action,
            "quantity": abs(_signed_qty(o)),
            "role": role,
        })
    return rows


def orphaned_protection_ids(
    positions: list | None,
    open_orders: list | None,
    *,
    symbols: Any = None,
    actions: Any = None,
) -> list[int]:
    return [
        int(r["order_id"])
        for r in orphaned_protection_rows(
            positions, open_orders, symbols=symbols, actions=actions
        )
    ]


def last_stop_block_reason(
    order_id: Any,
    open_orders: list | None,
    positions: list | None,
) -> str | None:
    """Reason to refuse a cancel that would strip the covering last-stop.

    Shared by the ``cancel_order`` gate and by the orphan sweep so the two can
    never disagree. A last-stop is an exit-side STP/TRAIL whose qty covers the
    held STK lot (0.51 slack). A crumb or a wrong-side leftover — including
    after a flatten that flipped the book — is not cover and may be cancelled.
    Returns None when the cancel is allowed (including when the order is
    unknown — the gateway owns that error).
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    orders = open_orders or []
    target = None
    for o in orders:
        if isinstance(o, dict) and _order_id_of(o) == oid:
            target = o
            break
    if target is None:
        return None

    symbol = str(target.get("symbol") or "").upper()
    if not symbol or not _is_stk_stop(target):
        return None

    held = _held_stk_signed(positions, symbol)
    if abs(held) < 1e-9:
        return None
    # Not load-bearing (crumb, wrong side, flatten-flip leftover).
    if not _stop_covers_held(target, symbol, held):
        return None
    if any(
        _stop_covers_held(o, symbol, held)
        for o in orders
        if isinstance(o, dict) and _order_id_of(o) != oid
    ):
        return None

    return (
        f"cancel_order rejected: order {oid} is the only working stop "
        f"protecting open {symbol} position (qty={int(held)}). "
        "First place replacement protection (oca / stop_order) "
        "or use modify_stop to move the existing stop."
    )


def _size_from_risk(
    *,
    quote: float,
    stop: Any,
    equity: float | None,
    cfg: Any,
) -> int:
    try:
        eq = float(equity or 0)
        stop_px = float(stop)
    except (TypeError, ValueError):
        return 0
    if eq <= 0 or quote <= 0 or stop_px <= 0:
        return 0
    dist = abs(quote - stop_px)
    if dist <= 0:
        return 0
    try:
        risk_pct = float(getattr(cfg, "max_risk_per_trade_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        risk_pct = 0.0
    try:
        pos_pct = float(getattr(cfg, "max_position_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pos_pct = 0.0
    # 0 = off: skip that ceiling. Do not invent 1% / 20% old_size_defaults.
    caps: list[int] = []
    if risk_pct > 0:
        caps.append(int((eq * (risk_pct / 100.0)) / dist))
    if pos_pct > 0:
        caps.append(int((eq * (pos_pct / 100.0)) / quote))
    if not caps:
        return 0
    qty = min(caps)
    return qty if qty >= 1 else 0


def size_if_stop(
    *,
    last: Any,
    stop: Any,
    equity: Any,
    cfg: Any = None,
) -> dict[str, Any]:
    """Shares that fit the live knobs if the stop is ``stop``. Not a ticket."""
    try:
        last_f = float(last)
        stop_f = float(stop)
        eq = float(equity)
    except (TypeError, ValueError):
        return {}
    if last_f <= 0 or stop_f <= 0 or eq <= 0:
        return {}
    dist = abs(last_f - stop_f)
    if dist <= 0:
        return {}
    if cfg is None:
        try:
            from abcxauto.config import get_config

            cfg = get_config()
        except Exception:
            return {}
    qty = _size_from_risk(quote=last_f, stop=stop_f, equity=eq, cfg=cfg)
    if qty < 1:
        return {}
    return {
        "qty": qty,
        "stop": round(stop_f, 4),
        "last": round(last_f, 4),
        "risk_per_share": round(dist, 4),
        "risk_usd": round(qty * dist, 2),
    }
