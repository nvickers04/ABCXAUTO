"""PCS vertical_spread (puts) fill-λ instrumentation.

Append-only journal events for λ_implied, conservative score marks, and
Arm v0 manage hits (tp50 | stop_1x | dte21). Does not send, look, flatten,
or change Risk gates. Geometry is IBKR BAG / legs — never MDA.

Mid / λ=0 is never fill evidence. Paper 7497 path only.
"""

from __future__ import annotations

import inspect
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PCS_CARD = "pcs-skew"
PCS_STRATEGY = "vertical_spread"
PCS_RIGHT = "P"

LAMBDA_DECLARED = 0.5
LAMBDA_STRESS = 1.0

MANAGE_PT_FRAC = 0.50
MANAGE_STOP_MULT = 1.0  # stop_1x — not 2× fill credit
MANAGE_DTE = 21

QUOTE_IBKR_BAG = "ibkr_bag"
QUOTE_IBKR_LEGS = "ibkr_legs_sum"
QUOTE_UNAVAILABLE = "unavailable"
QUOTE_SOURCES = frozenset({QUOTE_IBKR_BAG, QUOTE_IBKR_LEGS, QUOTE_UNAVAILABLE})

EVENT_QUOTE_SNAP = "pcs_quote_snap"
EVENT_TICKET_SUBMIT = "pcs_ticket_submit"
EVENT_ORDER_UPDATE = "pcs_order_update"
EVENT_FILL = "pcs_fill"
EVENT_COMMISSION = "pcs_commission"
EVENT_SCORE_MARK = "pcs_score_mark"
EVENT_MANAGE_CHECK = "pcs_manage_check"
EVENT_LIFECYCLE_END = "pcs_lifecycle_end"

EVENTS_ORDERED = (
    EVENT_QUOTE_SNAP,
    EVENT_TICKET_SUBMIT,
    EVENT_ORDER_UPDATE,
    EVENT_FILL,
    EVENT_COMMISSION,
    EVENT_SCORE_MARK,
    EVENT_MANAGE_CHECK,
    EVENT_LIFECYCLE_END,
)

MANAGE_RULES = ("tp50", "stop_1x", "dte21")

_NY = ZoneInfo("America/New_York")
_PX_EPS = 1e-9
_BUY = frozenset({"BUY", "BOT"})
_SELL = frozenset({"SELL", "SLD"})


def _finite(raw: Any) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return val


def _px(raw: Any) -> float | None:
    val = _finite(raw)
    if val is None or val <= 0:
        return None
    return val


def _near(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= _PX_EPS


def _mapping(params: Any) -> dict[str, Any]:
    if isinstance(params, Mapping):
        return dict(params)
    dump = getattr(params, "model_dump", None)
    if callable(dump):
        try:
            blob = dump(exclude_none=False)
        except TypeError:
            blob = dump()
        if isinstance(blob, dict):
            return dict(blob)
    out: dict[str, Any] = {}
    if params is None:
        return out
    for key in (
        "symbol",
        "expiration",
        "long_strike",
        "short_strike",
        "right",
        "quantity",
        "limit_price",
        "order_type",
        "closing_position",
        "card",
    ):
        if hasattr(params, key):
            out[key] = getattr(params, key)
    return out


def _card_of(params: Any = None, card: Any = None) -> str:
    raw = card
    dumped = _mapping(params)
    if raw in (None, ""):
        raw = dumped.get("card")
    return str(raw or "").strip().lower()


def is_pcs_ticket(strategy: Any = "", params: Any = None, card: Any = None) -> bool:
    """vertical_spread + right=P + card=pcs-skew. Not CSP. Not close_option."""
    if str(strategy or "").strip().lower() != PCS_STRATEGY:
        return False
    dumped = _mapping(params)
    right = str(dumped.get("right") or "")[:1].upper()
    if right != PCS_RIGHT:
        return False
    return _card_of(dumped, card) == PCS_CARD


def ticket_side(params: Any = None) -> str:
    dumped = _mapping(params)
    return "close" if dumped.get("closing_position") is True else "open"


def geometry_key(params: Any = None) -> str | None:
    dumped = _mapping(params)
    symbol = str(dumped.get("symbol") or "").strip().upper()
    exp = str(dumped.get("expiration") or "").strip()
    right = str(dumped.get("right") or PCS_RIGHT)[:1].upper() or PCS_RIGHT
    long_k = _px(dumped.get("long_strike"))
    short_k = _px(dumped.get("short_strike"))
    if not symbol or len(exp) != 8 or long_k is None or short_k is None:
        return None
    return f"{symbol}|{exp}|{long_k:g}|{short_k:g}|{right}"


def wing_width(params: Any = None) -> float | None:
    dumped = _mapping(params)
    long_k = _px(dumped.get("long_strike"))
    short_k = _px(dumped.get("short_strike"))
    if long_k is None or short_k is None:
        return None
    return abs(short_k - long_k)


def mid_of(bid: Any = None, ask: Any = None) -> float | None:
    """NBBO mid only. last is not mid — never graduate last as M."""
    b = _px(bid)
    a = _px(ask)
    if b is None or a is None or a <= b:
        return None
    return round((b + a) / 2.0, 6)


def half_spread_of(bid: Any = None, ask: Any = None) -> float | None:
    """S = (ask − bid) / 2. None when the book is missing or inverted."""
    b = _px(bid)
    a = _px(ask)
    if b is None or a is None:
        return None
    width = a - b
    if width <= _PX_EPS:
        return None
    return round(width / 2.0, 6)


def quote_source_of(
    *,
    bag: Mapping[str, Any] | None = None,
    legs_ok: bool = False,
) -> str:
    blob = bag if isinstance(bag, Mapping) else {}
    if mid_of(blob.get("bid"), blob.get("ask")) is not None:
        return QUOTE_IBKR_BAG
    if legs_ok:
        return QUOTE_IBKR_LEGS
    return QUOTE_UNAVAILABLE


def lambda_implied_open(m: Any, c_fill: Any, s: Any) -> float | None:
    """λ_implied = (M − C_fill) / S. Null if S is missing or zero."""
    mid = _finite(m)
    credit = _finite(c_fill)
    half = _finite(s)
    if mid is None or credit is None or half is None or abs(half) <= _PX_EPS:
        return None
    return round((mid - credit) / half, 6)


def c_score(
    c_actual: Any,
    m: Any,
    s: Any,
    *,
    lam: float = LAMBDA_DECLARED,
) -> float | None:
    """Open: C_score = min(C_actual, M − λ×S). None without a real book."""
    actual = _finite(c_actual)
    mid = _finite(m)
    half = _finite(s)
    if actual is None or mid is None or half is None:
        return None
    return round(min(actual, mid - float(lam) * half), 6)


def d_score(
    d_actual: Any,
    m_close: Any,
    s_close: Any,
    *,
    lam: float = LAMBDA_DECLARED,
) -> float | None:
    """Close: D_score = max(D_actual, M_close + λ×S_close)."""
    actual = _finite(d_actual)
    mid = _finite(m_close)
    half = _finite(s_close)
    if actual is None or mid is None or half is None:
        return None
    return round(max(actual, mid + float(lam) * half), 6)


def mid_or_lambda0_fill(
    fill_price: Any = None,
    mid: Any = None,
    lambda_implied: Any = None,
) -> bool:
    """True when the print is mid / λ=0 — forbidden as fill evidence."""
    if _near(_finite(fill_price), _finite(mid)):
        return True
    implied = _finite(lambda_implied)
    return implied is not None and abs(implied) <= _PX_EPS


def kill_evidence_valid(
    *,
    quote_source: Any = QUOTE_UNAVAILABLE,
    mid: Any = None,
    has_real_fill: bool = False,
    fill_price: Any = None,
    lambda_implied: Any = None,
) -> bool:
    """Kill evidence stays invalid until a real non-mid fill.

    unavailable / mid-null cannot graduate. A later IBKR fill can, unless
    that fill is mid or λ=0.
    """
    src = str(quote_source or QUOTE_UNAVAILABLE).strip().lower()
    if not has_real_fill:
        return False
    if mid_or_lambda0_fill(fill_price, mid, lambda_implied):
        return False
    if _px(fill_price) is None:
        return False
    if src == QUOTE_UNAVAILABLE and _finite(mid) is None:
        # Real fill later still counts when the book never printed.
        return True
    return True


def include_in_pnl_mean(*, filled: bool, has_real_fill: bool) -> bool:
    """Unfilled tickets stay out of the P&L mean."""
    return bool(filled and has_real_fill)


def model_cost_allocation_ok(_filled: bool = False) -> bool:
    """Unfilled still allows a model_cost allocation hook if one is present."""
    return True


def manage_levels(fill_credit: Any) -> dict[str, float | None]:
    credit = _px(fill_credit)
    return {
        "tp50": None if credit is None else round(MANAGE_PT_FRAC * credit, 6),
        "stop_1x": None if credit is None else round(MANAGE_STOP_MULT * credit, 6),
        "dte21": float(MANAGE_DTE),
    }


def manage_hits(
    *,
    debit_mark: Any = None,
    fill_credit: Any = None,
    dte: Any = None,
) -> tuple[str | None, list[str]]:
    """First hit of {tp50, stop_1x, dte21} in that listed order. Not stop_2x."""
    hits: list[str] = []
    credit = _px(fill_credit)
    debit = _finite(debit_mark)
    if credit is not None and debit is not None:
        if debit <= MANAGE_PT_FRAC * credit + _PX_EPS:
            hits.append("tp50")
        if debit + _PX_EPS >= MANAGE_STOP_MULT * credit:
            hits.append("stop_1x")
    try:
        days = int(dte) if dte is not None else None
    except (TypeError, ValueError):
        days = None
    if days is not None and days <= MANAGE_DTE:
        hits.append("dte21")
    first = None
    for name in MANAGE_RULES:
        if name in hits:
            first = name
            break
    return first, hits


def dte_days(expiration: Any, today: date | None = None) -> int | None:
    raw = str(expiration or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        exp = date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None
    if today is None:
        today = datetime.now(timezone.utc).astimezone(_NY).date()
    return (exp - today).days


def legs_sum_credit_quote(
    long_leg: Mapping[str, Any] | None,
    short_leg: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Credit book from IBKR wing quotes: bid=short.bid−long.ask."""
    long_q = long_leg if isinstance(long_leg, Mapping) else {}
    short_q = short_leg if isinstance(short_leg, Mapping) else {}
    long_bid = _px(long_q.get("bid"))
    long_ask = _px(long_q.get("ask"))
    short_bid = _px(short_q.get("bid"))
    short_ask = _px(short_q.get("ask"))
    if None in (long_bid, long_ask, short_bid, short_ask):
        return {
            "quote_source": QUOTE_UNAVAILABLE,
            "bid": None,
            "ask": None,
            "mid": None,
            "half_spread": None,
            "last": None,
        }
    bid = short_bid - long_ask
    ask = short_ask - long_bid
    mid = mid_of(bid, ask)
    half = half_spread_of(bid, ask)
    last = None
    short_last = _px(short_q.get("last"))
    long_last = _px(long_q.get("last"))
    if short_last is not None and long_last is not None:
        last = short_last - long_last
    src = QUOTE_IBKR_LEGS if mid is not None else QUOTE_UNAVAILABLE
    return {
        "quote_source": src,
        "bid": None if bid is None else round(bid, 6),
        "ask": None if ask is None else round(ask, 6),
        "mid": mid,
        "half_spread": half,
        "last": None if last is None else round(last, 6),
        "long_leg": {"bid": long_bid, "ask": long_ask, "last": long_last},
        "short_leg": {"bid": short_bid, "ask": short_ask, "last": short_last},
    }


def normalize_pcs_quote(
    raw: Mapping[str, Any] | None,
    *,
    source: str,
) -> dict[str, Any]:
    blob = raw if isinstance(raw, Mapping) else {}
    bid = _px(blob.get("bid"))
    ask = _px(blob.get("ask"))
    mid = mid_of(bid, ask)
    half = half_spread_of(bid, ask)
    last = _px(blob.get("last"))
    src = source if mid is not None else QUOTE_UNAVAILABLE
    return {
        "quote_source": src,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "half_spread": half,
        "last": last,
    }


def net_combo_fill(fills: list[Any] | None) -> dict[str, Any] | None:
    """Net BAG / paired OPT prints on one order. Mid/last rows are not fills."""
    rows = [f for f in (fills or []) if isinstance(f, Mapping)]
    if not rows:
        return None
    bag = [
        r
        for r in rows
        if str(r.get("sec_type") or r.get("secType") or r.get("sec") or "").upper()
        == "BAG"
        and _px(r.get("price") or r.get("avg_fill_price") or r.get("fill_price"))
        is not None
    ]
    if bag:
        row = bag[0]
        px = _px(row.get("price") or row.get("avg_fill_price") or row.get("fill_price"))
        side = str(row.get("side") or row.get("action") or "").upper()
        comm = 0.0
        for r in rows:
            c = _finite(r.get("commission"))
            if c is not None:
                comm += abs(c)
        if px is None:
            return None
        credit = px if side in _SELL else None
        debit = px if side in _BUY else None
        return {
            "fill_price": px,
            "credit": credit,
            "debit": debit,
            "commission": comm if comm > 0 else None,
            "exec_id": row.get("exec_id"),
            "order_id": row.get("order_id"),
            "has_real_fill": True,
        }
    bought = 0.0
    sold = 0.0
    n_buy = 0
    n_sell = 0
    comm = 0.0
    exec_ids: list[str] = []
    order_id = None
    for row in rows:
        px = _px(row.get("price") or row.get("avg_fill_price") or row.get("fill_price"))
        if px is None:
            continue
        side = str(row.get("side") or row.get("action") or "").upper()
        qty = _finite(row.get("quantity") or row.get("qty") or row.get("shares")) or 1.0
        if side in _SELL:
            sold += px * abs(qty)
            n_sell += 1
        elif side in _BUY:
            bought += px * abs(qty)
            n_buy += 1
        else:
            continue
        c = _finite(row.get("commission"))
        if c is not None:
            comm += abs(c)
        eid = row.get("exec_id")
        if eid not in (None, ""):
            exec_ids.append(str(eid))
        if order_id is None:
            order_id = row.get("order_id")
    if n_buy < 1 or n_sell < 1:
        if n_sell == 1 and n_buy == 0:
            credit = sold
            return {
                "fill_price": round(credit, 6),
                "credit": round(credit, 6),
                "debit": None,
                "commission": comm if comm > 0 else None,
                "exec_id": exec_ids[0] if exec_ids else None,
                "order_id": order_id,
                "has_real_fill": True,
            }
        if n_buy == 1 and n_sell == 0:
            debit = bought
            return {
                "fill_price": round(debit, 6),
                "credit": None,
                "debit": round(debit, 6),
                "commission": comm if comm > 0 else None,
                "exec_id": exec_ids[0] if exec_ids else None,
                "order_id": order_id,
                "has_real_fill": True,
            }
        return None
    credit = sold - bought
    return {
        "fill_price": round(abs(credit), 6),
        "credit": round(credit, 6) if credit > 0 else None,
        "debit": round(-credit, 6) if credit < 0 else (round(credit, 6) if credit == 0 else None),
        "commission": comm if comm > 0 else None,
        "exec_id": "+".join(exec_ids) if exec_ids else None,
        "order_id": order_id,
        "has_real_fill": True,
    }


def score_mark_payload(
    *,
    side: str,
    quote: Mapping[str, Any] | None,
    fill: Mapping[str, Any] | None,
) -> dict[str, Any]:
    q = quote if isinstance(quote, Mapping) else {}
    f = fill if isinstance(fill, Mapping) else {}
    mid = _finite(q.get("mid"))
    if mid is None:
        mid = mid_of(q.get("bid"), q.get("ask"))
    half = _finite(q.get("half_spread"))
    if half is None:
        half = half_spread_of(q.get("bid"), q.get("ask"))
    src = str(q.get("quote_source") or QUOTE_UNAVAILABLE)
    has_fill = bool(f.get("has_real_fill"))
    if side == "close":
        actual = _finite(f.get("debit") if f.get("debit") is not None else f.get("fill_price"))
        implied = None
        if mid is not None and actual is not None and half is not None:
            # Close λ uses the same (M − px) / S shape on the debit print.
            implied = lambda_implied_open(mid, actual, half)
        declared = d_score(actual, mid, half, lam=LAMBDA_DECLARED)
        stress = d_score(actual, mid, half, lam=LAMBDA_STRESS)
        evidence = kill_evidence_valid(
            quote_source=src,
            mid=mid,
            has_real_fill=has_fill,
            fill_price=actual,
            lambda_implied=implied,
        )
        return {
            "side": "close",
            "quote_source": src if src in QUOTE_SOURCES else QUOTE_UNAVAILABLE,
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "mid": mid,
            "half_spread": half,
            "last": q.get("last"),
            "fill_price": actual,
            "debit": actual,
            "credit": None,
            "lambda_declared": LAMBDA_DECLARED,
            "lambda_implied": implied,
            "lambda_stress": LAMBDA_STRESS,
            "c_score": None,
            "d_score": declared,
            "c_score_stress": None,
            "d_score_stress": stress,
            "evidence_valid": evidence,
            "include_in_pnl_mean": include_in_pnl_mean(
                filled=has_fill, has_real_fill=has_fill
            ),
            "model_cost_ok": model_cost_allocation_ok(has_fill),
        }
    actual = _finite(f.get("credit") if f.get("credit") is not None else f.get("fill_price"))
    implied = lambda_implied_open(mid, actual, half)
    declared = c_score(actual, mid, half, lam=LAMBDA_DECLARED)
    stress = c_score(actual, mid, half, lam=LAMBDA_STRESS)
    evidence = kill_evidence_valid(
        quote_source=src,
        mid=mid,
        has_real_fill=has_fill,
        fill_price=actual,
        lambda_implied=implied,
    )
    return {
        "side": "open",
        "quote_source": src if src in QUOTE_SOURCES else QUOTE_UNAVAILABLE,
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "mid": mid,
        "half_spread": half,
        "last": q.get("last"),
        "fill_price": actual,
        "credit": actual,
        "debit": None,
        "lambda_declared": LAMBDA_DECLARED,
        "lambda_implied": implied,
        "lambda_stress": LAMBDA_STRESS,
        "c_score": declared,
        "d_score": None,
        "c_score_stress": stress,
        "d_score_stress": None,
        "evidence_valid": evidence,
        "include_in_pnl_mean": include_in_pnl_mean(
            filled=has_fill, has_real_fill=has_fill
        ),
        "model_cost_ok": model_cost_allocation_ok(has_fill),
    }


def pnl_mean_samples(events: list[Any] | None) -> list[float]:
    """Conservative open/close marks that are allowed in the P&L mean."""
    out: list[float] = []
    for raw in events or []:
        if not isinstance(raw, Mapping):
            continue
        if not raw.get("include_in_pnl_mean"):
            continue
        if not raw.get("evidence_valid"):
            continue
        side = str(raw.get("side") or "")
        if side == "close":
            val = _finite(raw.get("d_score"))
            if val is not None:
                credit = _finite(raw.get("open_credit"))
                if credit is not None:
                    out.append(round(credit - val, 6))
            continue
        val = _finite(raw.get("c_score"))
        if val is not None:
            out.append(val)
    return out


def _ticket_fields(proposal: Any) -> dict[str, Any]:
    params = getattr(proposal, "params", proposal)
    dumped = _mapping(params)
    card = getattr(proposal, "card", None) if proposal is not None else None
    if card in (None, ""):
        card = dumped.get("card")
    dumped["card"] = str(card or "") or dumped.get("card")
    return dumped


async def capture_pcs_vertical_quote(
    connector: Any,
    params: Any = None,
) -> dict[str, Any]:
    """IBKR BAG quote, else legs sum. Never MDA. Fail closed to unavailable."""
    dumped = _mapping(params)
    empty = {
        "quote_source": QUOTE_UNAVAILABLE,
        "bid": None,
        "ask": None,
        "mid": None,
        "half_spread": None,
        "last": None,
    }
    if connector is None:
        return empty
    symbol = str(dumped.get("symbol") or "").strip().upper()
    exp = str(dumped.get("expiration") or "").strip()
    right = str(dumped.get("right") or PCS_RIGHT)[:1].upper() or PCS_RIGHT
    long_k = dumped.get("long_strike")
    short_k = dumped.get("short_strike")
    if not symbol or not exp or long_k in (None, "") or short_k in (None, ""):
        return empty

    bag_fn = getattr(connector, "get_live_vertical_bag_quote", None)
    if callable(bag_fn):
        try:
            raw = bag_fn(symbol, exp, long_k, short_k, right)
            if inspect.isawaitable(raw):
                raw = await raw
            if isinstance(raw, dict) and not raw.get("error"):
                norm = normalize_pcs_quote(raw, source=QUOTE_IBKR_BAG)
                if norm["quote_source"] == QUOTE_IBKR_BAG:
                    return norm
        except Exception:
            logger.debug("pcs BAG quote failed", exc_info=True)

    opt_fn = getattr(connector, "get_live_option_quote", None)
    if callable(opt_fn):
        try:
            long_raw = opt_fn(symbol, exp, long_k, right)
            short_raw = opt_fn(symbol, exp, short_k, right)
            if inspect.isawaitable(long_raw):
                long_raw = await long_raw
            if inspect.isawaitable(short_raw):
                short_raw = await short_raw
            if isinstance(long_raw, dict) and isinstance(short_raw, dict):
                if not long_raw.get("error") and not short_raw.get("error"):
                    summed = legs_sum_credit_quote(long_raw, short_raw)
                    if summed.get("quote_source") == QUOTE_IBKR_LEGS:
                        return summed
        except Exception:
            logger.debug("pcs legs-sum quote failed", exc_info=True)
    return empty


def _lifecycle_token(proposal_id: Any, dumped: Mapping[str, Any]) -> str:
    if proposal_id not in (None, ""):
        return str(proposal_id)
    geo = geometry_key(dumped) or "unknown"
    return geo


def _dedupe(event: str, lifecycle_id: str, extra: str = "") -> str:
    return "|".join(p for p in (event, lifecycle_id, extra) if p != "")


def journal_pcs_pre_send(
    journal: Any,
    proposal: Any,
    quote: Mapping[str, Any] | None,
    *,
    proposal_id: Any = None,
) -> str | None:
    """Record pcs_quote_snap. Returns lifecycle_id or None if not a PCS ticket."""
    dumped = _ticket_fields(proposal)
    strategy = str(getattr(proposal, "strategy", "") or dumped.get("strategy") or "")
    if not is_pcs_ticket(strategy, dumped, card=dumped.get("card")):
        return None
    if journal is None:
        return None
    side = ticket_side(dumped)
    geo = geometry_key(dumped)
    lifecycle_id = None
    if side == "close" and geo:
        finder = getattr(journal, "pcs_open_lifecycle_id", None)
        if callable(finder):
            try:
                lifecycle_id = finder(geo)
            except Exception:
                lifecycle_id = None
    if not lifecycle_id:
        lifecycle_id = f"pcs:{geo or 'na'}:{_lifecycle_token(proposal_id, dumped)}"
    q = quote if isinstance(quote, Mapping) else {}
    src = str(q.get("quote_source") or "")
    if src not in QUOTE_SOURCES:
        q = normalize_pcs_quote(q, source=QUOTE_UNAVAILABLE)
        src = q["quote_source"]
    mid = q.get("mid") if "mid" in q else mid_of(q.get("bid"), q.get("ask"))
    half = q.get("half_spread") if "half_spread" in q else half_spread_of(q.get("bid"), q.get("ask"))
    payload = {
        "event": EVENT_QUOTE_SNAP,
        "lifecycle_id": lifecycle_id,
        "dedupe_key": _dedupe(EVENT_QUOTE_SNAP, lifecycle_id, side),
        "symbol": str(dumped.get("symbol") or "").upper(),
        "strategy": PCS_STRATEGY,
        "card": PCS_CARD,
        "side": side,
        "quote_source": src,
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "mid": mid,
        "half_spread": half,
        "last": q.get("last"),
        "geometry_key": geo,
        "width": wing_width(dumped),
        "expiration": dumped.get("expiration"),
        "long_strike": dumped.get("long_strike"),
        "short_strike": dumped.get("short_strike"),
        "right": PCS_RIGHT,
        "limit_price": dumped.get("limit_price"),
        "quantity": dumped.get("quantity"),
        "evidence_valid": kill_evidence_valid(
            quote_source=src, mid=mid, has_real_fill=False
        ),
        "include_in_pnl_mean": False,
        "model_cost_ok": model_cost_allocation_ok(False),
    }
    journal.record_pcs_event(payload)
    return lifecycle_id


def journal_pcs_post_send(
    journal: Any,
    proposal: Any,
    quote: Mapping[str, Any] | None,
    result: Any,
    ok: bool,
    *,
    proposal_id: Any = None,
    lifecycle_id: str | None = None,
) -> str | None:
    """Record ticket_submit / order_update / immediate fill + score mark."""
    dumped = _ticket_fields(proposal)
    strategy = str(getattr(proposal, "strategy", "") or "")
    if not is_pcs_ticket(strategy, dumped, card=dumped.get("card")):
        return None
    if journal is None:
        return None
    side = ticket_side(dumped)
    geo = geometry_key(dumped)
    lid = lifecycle_id
    if not lid:
        lid = journal_pcs_pre_send(
            journal, proposal, quote, proposal_id=proposal_id
        )
    if not lid:
        return None
    blob = result if isinstance(result, Mapping) else {}
    order_id = blob.get("order_id") or blob.get("orderId")
    try:
        order_id = int(order_id) if order_id not in (None, "") else None
    except (TypeError, ValueError):
        order_id = None
    status = "submitted" if ok else "rejected"
    if not ok:
        status = str(blob.get("status") or "rejected").lower()
    journal.record_pcs_event(
        {
            "event": EVENT_TICKET_SUBMIT,
            "lifecycle_id": lid,
            "dedupe_key": _dedupe(EVENT_TICKET_SUBMIT, lid, side),
            "order_id": order_id,
            "symbol": str(dumped.get("symbol") or "").upper(),
            "strategy": PCS_STRATEGY,
            "card": PCS_CARD,
            "side": side,
            "geometry_key": geo,
            "ok": bool(ok),
            "limit_price": dumped.get("limit_price") or blob.get("limit_price"),
            "include_in_pnl_mean": False,
            "model_cost_ok": model_cost_allocation_ok(False),
        }
    )
    journal.record_pcs_event(
        {
            "event": EVENT_ORDER_UPDATE,
            "lifecycle_id": lid,
            "dedupe_key": _dedupe(EVENT_ORDER_UPDATE, lid, f"{order_id or 0}:{status}"),
            "order_id": order_id,
            "symbol": str(dumped.get("symbol") or "").upper(),
            "strategy": PCS_STRATEGY,
            "card": PCS_CARD,
            "side": side,
            "status": status,
            "include_in_pnl_mean": False,
        }
    )
    fill_px = None
    for key in ("avg_fill_price", "fill_price"):
        fill_px = _px(blob.get(key))
        if fill_px is not None:
            break
    filled_flag = blob.get("filled") is True or str(blob.get("status") or "").lower() in (
        "filled",
        "complete",
        "completed",
    )
    if fill_px is not None or filled_flag:
        fill = {
            "fill_price": fill_px,
            "credit": fill_px if side == "open" else None,
            "debit": fill_px if side == "close" else None,
            "has_real_fill": fill_px is not None,
            "order_id": order_id,
            "exec_id": blob.get("exec_id") or (f"send:{order_id}" if order_id else None),
        }
        _record_fill_and_score(journal, lid, side, quote, fill, dumped, geo)
    if not ok:
        journal.record_pcs_event(
            {
                "event": EVENT_LIFECYCLE_END,
                "lifecycle_id": lid,
                "dedupe_key": _dedupe(EVENT_LIFECYCLE_END, lid),
                "order_id": order_id,
                "symbol": str(dumped.get("symbol") or "").upper(),
                "strategy": PCS_STRATEGY,
                "card": PCS_CARD,
                "side": side,
                "reason": "unfilled_or_rejected",
                "include_in_pnl_mean": False,
                "model_cost_ok": model_cost_allocation_ok(False),
                "geometry_key": geo,
            }
        )
    return lid


def _record_fill_and_score(
    journal: Any,
    lifecycle_id: str,
    side: str,
    quote: Mapping[str, Any] | None,
    fill: Mapping[str, Any],
    dumped: Mapping[str, Any],
    geo: str | None,
) -> None:
    exec_id = fill.get("exec_id")
    journal.record_pcs_event(
        {
            "event": EVENT_FILL,
            "lifecycle_id": lifecycle_id,
            "dedupe_key": _dedupe(EVENT_FILL, lifecycle_id, str(exec_id or side)),
            "order_id": fill.get("order_id"),
            "exec_id": exec_id,
            "symbol": str(dumped.get("symbol") or "").upper(),
            "strategy": PCS_STRATEGY,
            "card": PCS_CARD,
            "side": side,
            "fill_price": fill.get("fill_price"),
            "credit": fill.get("credit"),
            "debit": fill.get("debit"),
            "geometry_key": geo,
            "has_real_fill": True,
            "include_in_pnl_mean": False,
        }
    )
    comm = _finite(fill.get("commission"))
    if comm is not None:
        journal.record_pcs_event(
            {
                "event": EVENT_COMMISSION,
                "lifecycle_id": lifecycle_id,
                "dedupe_key": _dedupe(EVENT_COMMISSION, lifecycle_id, str(exec_id or side)),
                "order_id": fill.get("order_id"),
                "exec_id": exec_id,
                "symbol": str(dumped.get("symbol") or "").upper(),
                "strategy": PCS_STRATEGY,
                "card": PCS_CARD,
                "side": side,
                "commission": abs(comm),
                "include_in_pnl_mean": False,
            }
        )
    q = quote
    if not (isinstance(q, Mapping) and q.get("quote_source") in QUOTE_SOURCES):
        q = _last_quote_snap(journal, lifecycle_id, side)
    mark = score_mark_payload(side=side, quote=q, fill=fill)
    mark.update(
        {
            "event": EVENT_SCORE_MARK,
            "lifecycle_id": lifecycle_id,
            "dedupe_key": _dedupe(EVENT_SCORE_MARK, lifecycle_id, side),
            "order_id": fill.get("order_id"),
            "exec_id": exec_id,
            "symbol": str(dumped.get("symbol") or "").upper(),
            "strategy": PCS_STRATEGY,
            "card": PCS_CARD,
            "geometry_key": geo,
        }
    )
    journal.record_pcs_event(mark)
    journal.record_pcs_event(
        {
            "event": EVENT_ORDER_UPDATE,
            "lifecycle_id": lifecycle_id,
            "dedupe_key": _dedupe(
                EVENT_ORDER_UPDATE, lifecycle_id, f"{fill.get('order_id') or 0}:filled"
            ),
            "order_id": fill.get("order_id"),
            "symbol": str(dumped.get("symbol") or "").upper(),
            "strategy": PCS_STRATEGY,
            "card": PCS_CARD,
            "side": side,
            "status": "filled",
            "include_in_pnl_mean": False,
        }
    )


def _last_quote_snap(journal: Any, lifecycle_id: str, side: str) -> dict[str, Any]:
    rows = []
    try:
        rows = journal.pcs_events(lifecycle_id=lifecycle_id, event=EVENT_QUOTE_SNAP)
    except Exception:
        rows = []
    for row in rows:
        if str(row.get("side") or "") == side:
            return row
    return rows[0] if rows else {"quote_source": QUOTE_UNAVAILABLE}


def _open_lifecycles(journal: Any) -> list[dict[str, Any]]:
    try:
        return list(journal.pcs_open_lifecycles() or [])
    except Exception:
        logger.debug("pcs open lifecycles failed", exc_info=True)
        return []


def _fills_for_order(fills: list[Any], order_id: Any) -> list[dict[str, Any]]:
    try:
        want = int(order_id)
    except (TypeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for raw in fills or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            oid = int(raw.get("order_id"))
        except (TypeError, ValueError):
            continue
        if oid == want:
            out.append(dict(raw))
    return out


def _params_from_lifecycle(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "expiration": row.get("expiration") or (row.get("payload") or {}).get("expiration"),
        "long_strike": row.get("long_strike")
        or (row.get("payload") or {}).get("long_strike"),
        "short_strike": row.get("short_strike")
        or (row.get("payload") or {}).get("short_strike"),
        "right": PCS_RIGHT,
        "card": PCS_CARD,
    }


def _lot_matches(positions: list[Any], dumped: Mapping[str, Any]) -> bool:
    symbol = str(dumped.get("symbol") or "").upper()
    exp = str(dumped.get("expiration") or "").strip()
    long_k = _px(dumped.get("long_strike"))
    short_k = _px(dumped.get("short_strike"))
    if not symbol or not exp or long_k is None or short_k is None:
        return False
    seen: set[float] = set()
    for pos in positions or []:
        if not isinstance(pos, Mapping):
            continue
        if str(pos.get("symbol") or "").upper() != symbol:
            continue
        sec = str(pos.get("secType") or pos.get("sec_type") or pos.get("sec") or "").upper()
        if sec not in ("OPT", "FOP"):
            continue
        if str(pos.get("right") or "")[:1].upper() != PCS_RIGHT:
            continue
        raw_exp = str(
            pos.get("expiration") or pos.get("lastTradeDateOrContractMonth") or ""
        ).strip()
        digits = "".join(ch for ch in raw_exp if ch.isdigit())
        if digits[:8] != exp:
            continue
        strike = _px(pos.get("strike"))
        qty = _finite(pos.get("quantity") or pos.get("qty") or pos.get("position"))
        if strike is None or qty is None or abs(qty) <= 1e-9:
            continue
        if _near(strike, long_k) or _near(strike, short_k):
            seen.add(round(strike, 4))
    return long_k is not None and short_k is not None and {
        round(long_k, 4),
        round(short_k, 4),
    }.issubset(seen)


def _send_mark_missed(journal: Any, order_id: Any) -> bool:
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return False
    fn = getattr(journal, "send_marks_by_order_id", None)
    if not callable(fn):
        return False
    try:
        by = fn() or {}
    except Exception:
        return False
    row = by.get(oid)
    if not isinstance(row, Mapping):
        return False
    return str(row.get("status") or "").strip().lower() == "missed"


def ingest_pcs_from_look(journal: Any, snap: Mapping[str, Any] | None) -> None:
    """Fills / commission / score / order_update / lifecycle_end from a look snap."""
    if journal is None:
        return
    bag = snap if isinstance(snap, Mapping) else {}
    fills = bag.get("fills") if isinstance(bag.get("fills"), list) else []
    orders = bag.get("open_orders") if isinstance(bag.get("open_orders"), list) else []
    positions = bag.get("positions") if isinstance(bag.get("positions"), list) else []
    open_oids: set[int] = set()
    for order in orders:
        if not isinstance(order, Mapping):
            continue
        raw = order.get("order_id") if order.get("order_id") is not None else order.get("orderId")
        try:
            open_oids.add(int(raw))
        except (TypeError, ValueError):
            continue
    for row in _open_lifecycles(journal):
        lid = str(row.get("lifecycle_id") or "")
        if not lid:
            continue
        oid = row.get("order_id")
        dumped = _params_from_lifecycle(row)
        geo = row.get("geometry_key") or geometry_key(dumped)
        side = str(row.get("side") or "open")
        if oid is not None:
            try:
                oid_i = int(oid)
            except (TypeError, ValueError):
                oid_i = None
            if oid_i is not None:
                status = "working" if oid_i in open_oids else None
                if status:
                    journal.record_pcs_event(
                        {
                            "event": EVENT_ORDER_UPDATE,
                            "lifecycle_id": lid,
                            "dedupe_key": _dedupe(
                                EVENT_ORDER_UPDATE, lid, f"{oid_i}:{status}"
                            ),
                            "order_id": oid_i,
                            "symbol": str(dumped.get("symbol") or "").upper(),
                            "strategy": PCS_STRATEGY,
                            "card": PCS_CARD,
                            "side": side,
                            "status": status,
                            "include_in_pnl_mean": False,
                        }
                    )
                matched = _fills_for_order(fills, oid_i)
                net = net_combo_fill(matched)
                if net and net.get("has_real_fill"):
                    q = _last_quote_snap(journal, lid, side)
                    _record_fill_and_score(journal, lid, side, q, net, dumped, geo)
        still = _lot_matches(positions, dumped)
        has_fill = False
        try:
            has_fill = bool(
                journal.pcs_events(lifecycle_id=lid, event=EVENT_FILL)
            )
        except Exception:
            has_fill = False
        working = False
        if oid is not None:
            try:
                working = int(oid) in open_oids
            except (TypeError, ValueError):
                working = False
        if not still and not working:
            if has_fill:
                reason = "filled_closed"
            elif _send_mark_missed(journal, oid):
                reason = "unfilled"
            else:
                # Stale look without the new BAG oid must not kill the lifecycle.
                continue
            journal.record_pcs_event(
                {
                    "event": EVENT_LIFECYCLE_END,
                    "lifecycle_id": lid,
                    "dedupe_key": _dedupe(EVENT_LIFECYCLE_END, lid),
                    "order_id": oid,
                    "symbol": str(dumped.get("symbol") or "").upper(),
                    "strategy": PCS_STRATEGY,
                    "card": PCS_CARD,
                    "side": side,
                    "reason": reason,
                    "include_in_pnl_mean": include_in_pnl_mean(
                        filled=has_fill, has_real_fill=has_fill
                    ),
                    "model_cost_ok": model_cost_allocation_ok(has_fill),
                    "geometry_key": geo,
                }
            )


def _last_open_credit(journal: Any, lifecycle_id: str) -> float | None:
    try:
        marks = journal.pcs_events(lifecycle_id=lifecycle_id, event=EVENT_SCORE_MARK)
    except Exception:
        marks = []
    for row in marks:
        if str(row.get("side") or "") == "open":
            return _px(row.get("credit") or row.get("fill_price") or row.get("c_score"))
    try:
        fills = journal.pcs_events(lifecycle_id=lifecycle_id, event=EVENT_FILL)
    except Exception:
        fills = []
    for row in fills:
        if str(row.get("side") or "") == "open":
            return _px(row.get("credit") or row.get("fill_price"))
    return None


def _last_manage_fingerprint(journal: Any, lifecycle_id: str) -> str | None:
    try:
        rows = journal.pcs_events(
            lifecycle_id=lifecycle_id, event=EVENT_MANAGE_CHECK, limit=1
        )
    except Exception:
        return None
    if not rows:
        return None
    return str((rows[0] or {}).get("fingerprint") or "")


async def refresh_pcs_manage(
    journal: Any,
    snap: Mapping[str, Any] | None,
    connector: Any = None,
) -> None:
    """While a PCS lot is open, snap IBKR mark vs Arm v0 manage levels."""
    if journal is None:
        return
    bag = snap if isinstance(snap, Mapping) else {}
    positions = bag.get("positions") if isinstance(bag.get("positions"), list) else []
    for row in _open_lifecycles(journal):
        lid = str(row.get("lifecycle_id") or "")
        dumped = _params_from_lifecycle(row)
        if not lid or not _lot_matches(positions, dumped):
            continue
        quote = {"quote_source": QUOTE_UNAVAILABLE}
        if connector is not None:
            try:
                quote = await capture_pcs_vertical_quote(connector, dumped)
            except Exception:
                logger.debug("pcs manage quote failed", exc_info=True)
                quote = {"quote_source": QUOTE_UNAVAILABLE}
        # Close mark is the debit to buy the credit spread back ≈ ask.
        debit = _px(quote.get("ask"))
        if debit is None:
            debit = _finite(quote.get("mid"))
        credit = _last_open_credit(journal, lid)
        dte = dte_days(dumped.get("expiration"))
        first, hits = manage_hits(debit_mark=debit, fill_credit=credit, dte=dte)
        levels = manage_levels(credit)
        half = quote.get("half_spread")
        ba_tax = None
        if _finite(half) is not None:
            ba_tax = round(2.0 * float(half), 6)
        elif _px(quote.get("bid")) is not None and _px(quote.get("ask")) is not None:
            ba_tax = round(float(quote["ask"]) - float(quote["bid"]), 6)
        fingerprint = f"{first}|{debit}|{dte}|{quote.get('quote_source')}|{ba_tax}"
        if fingerprint == _last_manage_fingerprint(journal, lid):
            continue
        journal.record_pcs_event(
            {
                "event": EVENT_MANAGE_CHECK,
                "lifecycle_id": lid,
                "order_id": row.get("order_id"),
                "symbol": str(dumped.get("symbol") or "").upper(),
                "strategy": PCS_STRATEGY,
                "card": PCS_CARD,
                "side": "open",
                "quote_source": quote.get("quote_source"),
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "mid": quote.get("mid"),
                "half_spread": quote.get("half_spread"),
                "last": quote.get("last"),
                "debit": debit,
                "credit": credit,
                "manage_rule": first,
                "manage_hits": hits,
                "manage_levels": levels,
                "ba_tax": ba_tax,
                "dte": dte,
                "fingerprint": fingerprint,
                "include_in_pnl_mean": False,
                "geometry_key": row.get("geometry_key") or geometry_key(dumped),
            }
        )


async def follow_pcs_after_act(
    act: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    snap: Mapping[str, Any] | None,
    connector: Any = None,
    journal: Any = None,
) -> None:
    """post_act hook: look ingest + manage refresh. Never a second send path."""
    if journal is None:
        try:
            from abcxauto.memory import get_journal

            journal = get_journal()
        except Exception:
            return
    ingest_pcs_from_look(journal, snap)
    await refresh_pcs_manage(journal, snap, connector)
    _ = (act, result)


async def journal_pcs_pre_send_async(
    journal: Any,
    connector: Any,
    proposal: Any,
    quote: Mapping[str, Any] | None,
    *,
    proposal_id: Any = None,
) -> str | None:
    """Refresh the IBKR book when capture_send_quote missed PCS geometry."""
    dumped = _ticket_fields(proposal)
    strategy = str(getattr(proposal, "strategy", "") or "")
    if not is_pcs_ticket(strategy, dumped, card=dumped.get("card")):
        return None
    q = quote if isinstance(quote, Mapping) else {}
    src = str(q.get("quote_source") or "")
    if src not in QUOTE_SOURCES:
        try:
            q = await capture_pcs_vertical_quote(connector, dumped)
        except Exception:
            logger.debug("pcs pre-send recapture failed", exc_info=True)
            q = {
                "quote_source": QUOTE_UNAVAILABLE,
                "bid": None,
                "ask": None,
                "mid": None,
                "half_spread": None,
                "last": None,
            }
    return journal_pcs_pre_send(journal, proposal, q, proposal_id=proposal_id)
