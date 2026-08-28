"""NBBO vs fill next to every send.

Paper 7497 often fills a marketable buy at last/mid. That print is inside the
spread; a card that is +EV only because of it is not a card. These fields are
how we later prove live marks ≠ paper marks. Graduation / conservative_pnl
stay elsewhere.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

SEND_MARK_FIELDS = (
    "ibkr_last",
    "bid",
    "ask",
    "sent_price",
    "fill_price",
    "signed_slippage",
    "spread_paid",
    "fill_label",
)

FILL_LABEL_WORKING = "working"
FILL_LABEL_MISSED = "missed"
FILL_LABEL_MID_INSIDE = "mid_inside_spread"
FILL_LABEL_INSIDE = "inside_spread"
FILL_LABEL_AT_BID = "at_bid"
FILL_LABEL_AT_ASK = "at_ask"
FILL_LABEL_OUTSIDE = "outside_spread"

STATUS_WORKING = "working"
STATUS_FILLED = "filled"
STATUS_MISSED = "missed"

_BUY = frozenset({"BUY", "BOT", "LONG"})
_SELL = frozenset({"SELL", "SLD", "SHORT"})


def finite_px(value: Any) -> Optional[float]:
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    if px != px or px <= 0:
        return None
    return px


def _mapping(params: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(params, Mapping):
        out = dict(params)
    else:
        dump = getattr(params, "model_dump", None)
        if callable(dump):
            try:
                blob = dump(exclude_none=False)
            except TypeError:
                blob = dump()
            if isinstance(blob, dict):
                out = dict(blob)
        elif params is not None:
            for key in (
                "symbol",
                "action",
                "side",
                "direction",
                "limit_price",
                "entry_price",
                "stop_price",
                "target_price",
                "price_hint",
                "expiration",
                "strike",
                "right",
                "card",
                "quantity",
            ):
                if hasattr(params, key):
                    out[key] = getattr(params, key)
        # card is Field(exclude=True) on tickets — still the scorecard name.
        if not out.get("card") and params is not None:
            card = getattr(params, "card", None)
            if card:
                out["card"] = card
    return out


def side_of(strategy: str = "", params: Any = None, result: Any = None) -> Optional[str]:
    """BUY or SELL. Direction LONG/SHORT and BOT/SLD map onto those."""
    for blob in (result, _mapping(params)):
        if not isinstance(blob, Mapping):
            continue
        for key in ("action", "side", "direction"):
            raw = str(blob.get(key) or "").strip().upper()
            if raw in _BUY:
                return "BUY"
            if raw in _SELL:
                return "SELL"
    return None


def sent_price_of(
    strategy: str = "",
    params: Any = None,
    quote: Optional[Mapping[str, Any]] = None,
) -> Optional[float]:
    """Price on the ticket, else IBKR last/mid for a marketable send."""
    dumped = _mapping(params)
    for key in ("limit_price", "entry_price"):
        px = finite_px(dumped.get(key))
        if px is not None:
            return round(px, 6)
    q = quote if isinstance(quote, Mapping) else {}
    for key in ("last", "mid"):
        px = finite_px(q.get(key))
        if px is not None:
            return round(px, 6)
    hint = finite_px(dumped.get("price_hint"))
    if hint is not None:
        return round(hint, 6)
    return None


def mid_of(bid: Any = None, ask: Any = None, last: Any = None) -> Optional[float]:
    b = finite_px(bid)
    a = finite_px(ask)
    if b is not None and a is not None and a > b:
        return round((b + a) / 2.0, 6)
    return finite_px(last)


def _near(left: float, right: float, *, width: Optional[float] = None) -> bool:
    tol = 1e-6
    if width is not None and width > 0:
        tol = max(tol, min(0.01, 0.25 * float(width)))
    else:
        tol = max(tol, 0.005)
    return abs(left - right) <= tol


def fill_label_of(
    *,
    bid: Any = None,
    ask: Any = None,
    fill_price: Any = None,
    status: str = "",
) -> str:
    """Where the print sat vs the dispatch NBBO.

    ``mid_inside_spread`` is the paper gift: filled at mid while bid < mid < ask.
    """
    st = str(status or "").strip().lower()
    fill = finite_px(fill_price)
    if fill is None:
        if st == STATUS_MISSED:
            return FILL_LABEL_MISSED
        return FILL_LABEL_WORKING
    b = finite_px(bid)
    a = finite_px(ask)
    if b is None or a is None or a <= b:
        return FILL_LABEL_OUTSIDE
    width = a - b
    eps = max(1e-6, min(0.005, 0.1 * width))
    if fill <= b + eps:
        return FILL_LABEL_AT_BID
    if fill >= a - eps:
        return FILL_LABEL_AT_ASK
    if b < fill < a:
        mid = (b + a) / 2.0
        if _near(fill, mid, width=width):
            return FILL_LABEL_MID_INSIDE
        return FILL_LABEL_INSIDE
    return FILL_LABEL_OUTSIDE


def signed_slippage_of(
    *,
    fill_price: Any,
    sent_price: Any,
    side: Any = None,
    mid: Any = None,
) -> Optional[float]:
    """Positive is adverse. BUY: fill − benchmark; SELL: benchmark − fill."""
    fill = finite_px(fill_price)
    bench = finite_px(sent_price)
    if bench is None:
        bench = finite_px(mid)
    if fill is None or bench is None:
        return None
    raw = fill - bench
    bit = str(side or "").strip().upper()
    if bit in _SELL:
        raw = -raw
    return round(raw, 6)


def spread_paid_of(
    *,
    fill_price: Any,
    bid: Any,
    ask: Any,
    side: Any = None,
) -> Optional[float]:
    """Dollars of the quoted spread consumed by the fill.

    BUY: fill − bid. SELL: ask − fill. A mid fill pays half the spread.
    """
    fill = finite_px(fill_price)
    b = finite_px(bid)
    a = finite_px(ask)
    if fill is None:
        return None
    bit = str(side or "").strip().upper()
    if bit in _SELL:
        if a is None:
            return None
        return round(a - fill, 6)
    if b is None:
        return None
    return round(fill - b, 6)


def quote_ticks(quote: Optional[Mapping[str, Any]]) -> dict[str, Optional[float]]:
    q = quote if isinstance(quote, Mapping) else {}
    last = finite_px(q.get("last"))
    bid = finite_px(q.get("bid"))
    ask = finite_px(q.get("ask"))
    mid = finite_px(q.get("mid")) or mid_of(bid, ask, last)
    if last is None:
        last = mid
    return {"ibkr_last": last, "bid": bid, "ask": ask, "mid": mid}


def fill_price_from_result(result: Any) -> Optional[float]:
    if not isinstance(result, Mapping):
        return None
    for key in ("avg_fill_price", "fill_price"):
        px = finite_px(result.get(key))
        if px is not None:
            return round(px, 6)
    filled = result.get("filled")
    status = str(result.get("fill_status") or result.get("status") or "").strip().lower()
    if filled is True or status in ("filled", "complete", "completed"):
        px = finite_px(result.get("entry_price"))
        if px is not None:
            return round(px, 6)
    return None


def primary_order_id(result: Any) -> Optional[int]:
    if not isinstance(result, Mapping):
        return None
    for key in ("bracket_order_id", "entry_order_id", "order_id", "orderId"):
        raw = result.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def public_marks(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Always include the send/fill fields, even when still working / missed."""
    src = row if isinstance(row, Mapping) else {}
    return {key: src.get(key) for key in SEND_MARK_FIELDS}


def compute_marks(
    quote: Optional[Mapping[str, Any]] = None,
    *,
    sent_price: Any = None,
    fill_price: Any = None,
    side: Any = None,
    status: str = "",
) -> dict[str, Any]:
    ticks = quote_ticks(quote)
    last = ticks["ibkr_last"]
    bid = ticks["bid"]
    ask = ticks["ask"]
    mid = ticks["mid"]
    sent = finite_px(sent_price)
    if sent is None:
        sent = last if last is not None else mid
    fill = finite_px(fill_price)
    st = str(status or "").strip().lower()
    if not st:
        st = STATUS_FILLED if fill is not None else STATUS_WORKING
    label = fill_label_of(bid=bid, ask=ask, fill_price=fill, status=st)
    return {
        "ibkr_last": last,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "sent_price": sent,
        "fill_price": fill,
        "signed_slippage": signed_slippage_of(
            fill_price=fill, sent_price=sent, side=side, mid=mid
        ),
        "spread_paid": spread_paid_of(
            fill_price=fill, bid=bid, ask=ask, side=side
        ),
        "fill_label": label,
        "status": st,
        "side": str(side).upper() if side else None,
    }


def build_dispatch_marks(
    *,
    strategy: str = "",
    params: Any = None,
    quote: Optional[Mapping[str, Any]] = None,
    result: Any = None,
    ok: bool = True,
) -> dict[str, Any]:
    side = side_of(strategy, params, result)
    sent = sent_price_of(strategy, params, quote)
    fill = fill_price_from_result(result) if ok else None
    if not ok:
        status = STATUS_MISSED
    elif fill is not None:
        status = STATUS_FILLED
    else:
        status = STATUS_WORKING
    marks = compute_marks(
        quote, sent_price=sent, fill_price=fill, side=side, status=status
    )
    dumped = _mapping(params)
    marks["strategy"] = str(strategy or "")[:60]
    marks["symbol"] = str(dumped.get("symbol") or "").upper()[:12]
    marks["card"] = str(dumped.get("card") or "")[:120]
    marks["side"] = side
    if isinstance(result, Mapping):
        oid = primary_order_id(result)
        if oid is not None:
            marks["order_id"] = oid
    return marks


def apply_fill_to_marks(
    marks: Mapping[str, Any],
    *,
    fill_price: Any,
    side: Any = None,
) -> dict[str, Any]:
    """Stamp a later fill onto dispatch-time NBBO / sent price."""
    quote = {
        "last": marks.get("ibkr_last"),
        "bid": marks.get("bid"),
        "ask": marks.get("ask"),
        "mid": marks.get("mid"),
    }
    bit = side or marks.get("side")
    out = compute_marks(
        quote,
        sent_price=marks.get("sent_price"),
        fill_price=fill_price,
        side=bit,
        status=STATUS_FILLED,
    )
    merged = dict(marks)
    merged.update(out)
    merged["side"] = bit
    merged["status"] = STATUS_FILLED
    return merged


def mark_missed(marks: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(marks)
    out["fill_price"] = None
    out["signed_slippage"] = None
    out["spread_paid"] = None
    out["fill_label"] = FILL_LABEL_MISSED
    out["status"] = STATUS_MISSED
    return out


async def capture_send_quote(connector: Any, proposal: Any) -> dict[str, Any]:
    """IBKR last/bid/ask at dispatch. Never MDA. Missing ticks → empty dict."""
    if connector is None or proposal is None:
        return {}
    params = getattr(proposal, "params", proposal)
    dumped = _mapping(params)
    symbol = str(dumped.get("symbol") or "").strip().upper()
    if not symbol:
        return {}
    exp = dumped.get("expiration")
    strike = dumped.get("strike")
    right = dumped.get("right")
    if exp not in (None, "") and strike not in (None, "") and right not in (None, ""):
        opt = getattr(connector, "get_live_option_quote", None)
        if callable(opt):
            try:
                raw = opt(symbol, exp, strike, right)
                if inspect.isawaitable(raw):
                    raw = await raw
                if isinstance(raw, dict) and not raw.get("error"):
                    if finite_px(raw.get("bid")) or finite_px(raw.get("last")):
                        return raw
            except Exception:
                logger.debug("send_marks option quote failed", exc_info=True)
    fn = getattr(connector, "get_live_quote", None)
    if not callable(fn):
        return {}
    try:
        try:
            raw = fn(symbol, fresh=False)
        except TypeError:
            raw = fn(symbol)
        if inspect.isawaitable(raw):
            raw = await raw
        if isinstance(raw, dict) and not raw.get("error"):
            return raw
    except Exception:
        logger.debug("send_marks quote failed symbol=%s", symbol, exc_info=True)
    return {}
