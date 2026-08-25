"""Clerk: simple entry idea → protected structure, and protection vs the book.

Grok picks symbol and side. Code fills missing stop / target / size from the
live IBKR quote and the risk floor. Prices Grok already set are never rewritten.

The second half of the module is the reverse question: does a working exit
still cover an open lot? A protective order that outlives its position is not
untidy, it is a naked entry waiting for a print — a stale SELL on a flat book
sells stock the account does not own. ``orphaned_protection_rows`` answers
"provably flat" conservatively (anything it cannot identify counts as still
covering), and ``last_stop_block_reason`` is the shared last-stop rule so the
cancel gate and the reconciler can never disagree about what is load-bearing.
"""

from __future__ import annotations

from typing import Any

from abcxauto.structure_grade import posture_stop_bands, session_usable

_NAKED_OPEN = frozenset({"market_order", "limit_order", "stop_order"})
_PROTECT_STRATS = frozenset({"market_bracket", "bracket", "oca"})
_DEFAULT_STOP_PCT = 0.01


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


def _px(value: float) -> float:
    return round(float(value) + 1e-9, 2)


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


def _stk_qty(positions: list, symbol: str) -> int | None:
    want = str(symbol or "").upper()
    if not want:
        return None
    for p in positions or []:
        if str(p.get("symbol") or "").upper() != want:
            continue
        sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        try:
            qty = abs(int(float(p.get("quantity") or p.get("position") or 0)))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            return qty
    return None


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
    """Fill omitted stop / target / qty. Never overwrite Grok's numbers.

    When this look already has today's session range, a missing stop is the
    opening low (LONG) / high (SHORT) and a missing target is the 30% retrace
    if it sits on the right side of live. That is the written card, not a 1% band.
    """
    if not isinstance(act, dict):
        return []
    strat = str(act.get("strategy") or act.get("action") or "").strip().lower()
    if strat not in _PROTECT_STRATS:
        return []
    params = _params(act)
    direction = _direction(params)
    if direction not in ("LONG", "SHORT"):
        return []
    params["direction"] = direction
    try:
        quote = float(quote_last) if quote_last is not None else 0.0
    except (TypeError, ValueError):
        quote = 0.0
    if quote <= 0:
        return []

    filled: list[str] = []
    lo, hi = posture_stop_bands(posture)
    stop_pct = min(hi, max(lo, _DEFAULT_STOP_PCT))
    stop_dist = quote * stop_pct
    tape = session if session_usable(session) else None
    invent_bands = True
    if tape is None:
        try:
            from abcxauto.lab_playbook import live_card_needs_session

            invent_bands = not live_card_needs_session()
        except Exception:
            invent_bands = True

    if _missing(params, "stop_price"):
        if tape and direction == "LONG" and tape.get("low") not in (None, ""):
            params["stop_price"] = _px(tape["low"])
            filled.append("stop_price")
        elif tape and direction == "SHORT" and tape.get("high") not in (None, ""):
            params["stop_price"] = _px(tape["high"])
            filled.append("stop_price")
        elif invent_bands and direction == "LONG":
            params["stop_price"] = _px(quote - stop_dist)
            filled.append("stop_price")
        elif invent_bands:
            params["stop_price"] = _px(quote + stop_dist)
            filled.append("stop_price")
    if _missing(params, "target_price"):
        retrace = None
        if tape:
            try:
                from abcxauto.lab_playbook import session_target

                row = dict(tape)
                if quote > 0:
                    row["last"] = quote
                retrace = session_target(row, direction)
            except Exception:
                retrace = None
        if direction == "LONG" and retrace is not None and retrace > quote:
            params["target_price"] = _px(retrace)
            filled.append("target_price")
        elif direction == "SHORT" and retrace is not None and retrace < quote:
            params["target_price"] = _px(retrace)
            filled.append("target_price")
        elif invent_bands and direction == "LONG":
            params["target_price"] = _px(quote + stop_dist)
            filled.append("target_price")
        elif invent_bands:
            params["target_price"] = _px(quote - stop_dist)
            filled.append("target_price")
    if strat == "bracket" and _missing(params, "entry_price"):
        params["entry_price"] = _px(quote)
        filled.append("entry_price")
    if _missing(params, "price_hint"):
        params["price_hint"] = _px(quote)
        filled.append("price_hint")

    if _missing(params, "quantity"):
        qty = None
        if strat == "oca":
            qty = _stk_qty(positions or [], str(params.get("symbol") or ""))
        if qty is None:
            qty = _size_from_risk(
                quote=quote,
                stop=params.get("stop_price"),
                equity=equity,
                cfg=cfg,
            )
        if qty and qty > 0:
            params["quantity"] = int(qty)
            filled.append("quantity")

    if filled:
        act["_protection_filled"] = filled
    return filled


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
    """Reason to refuse a cancel that would strip the only stop on a live lot.

    Shared by the ``cancel_order`` gate and by the orphan sweep so the two can
    never disagree. Returns None when the cancel is allowed (including when the
    order is unknown — the gateway owns that error).
    """
    from abcxauto.broker.order_types import is_stop_order

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
    if not symbol or not is_stop_order(_order_type_of(target)):
        return None

    held = 0.0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if _sec_bucket(p) != "STK":
            continue
        if str(p.get("symbol") or "").upper() != symbol:
            continue
        held = _signed_qty(p)
        break
    if abs(held) < 1e-9:
        return None

    for o in orders:
        if not isinstance(o, dict) or _order_id_of(o) == oid:
            continue
        if str(o.get("symbol") or "").upper() != symbol:
            continue
        if _sec_bucket(o) != "STK":
            continue
        if is_stop_order(_order_type_of(o)):
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
        risk_pct = float(getattr(cfg, "max_risk_per_trade_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        risk_pct = 1.0
    try:
        pos_pct = float(getattr(cfg, "max_position_pct", 20.0) or 20.0)
    except (TypeError, ValueError):
        pos_pct = 20.0
    risk_qty = int((eq * (risk_pct / 100.0)) / dist)
    cap_qty = int((eq * (pos_pct / 100.0)) / quote)
    qty = min(risk_qty, cap_qty)
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
