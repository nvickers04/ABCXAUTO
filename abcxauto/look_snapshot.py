"""This-look quote / option_quote / book numbers for the send gate.

Grok's ticket last / IV / credit / width must appear in those tool results
from THIS look. Unverifiable is a kill, not a pass. Not a verifier agent.
"""

from __future__ import annotations

import math
from typing import Any

REASON_CODE = "stale_or_invented_number"
LOOK_TOOLS = ("quote", "option_quote", "book")
_SNAP_KEY = "_look_tool_snapshot"

_PRINT_KEYS = frozenset(
    {
        "last",
        "bid",
        "ask",
        "mid",
        "mark",
        "price",
        "mkt",
        "market_price",
        "marketprice",
        "lastprice",
    }
)
_IV_KEYS = frozenset(
    {"iv", "implied_vol", "impliedvolatility", "implied_volatility", "atm_iv"}
)
_CREDIT_KEYS = frozenset({"credit", "net_credit", "premium", "net_premium"})
_WIDTH_KEYS = frozenset({"width", "wing_width", "wingwidth"})
_STRIKE_KEYS = frozenset(
    {
        "strike",
        "long_strike",
        "short_strike",
        "put_long_strike",
        "put_short_strike",
        "call_short_strike",
        "call_long_strike",
        "center_strike",
        "lower_strike",
        "middle_strike",
        "upper_strike",
        "near_strike",
        "far_strike",
        "put_strike",
        "call_strike",
    }
)
_TICKET_LAST = ("last", "price_hint", "entry_price")
_TICKET_IV = ("iv", "implied_vol", "impliedVolatility", "implied_volatility")
_TICKET_CREDIT = ("credit", "net_credit", "premium", "net_premium")
_TICKET_WIDTH = ("width", "wing_width")
_SKIP_WALK = frozenset(
    {
        "mda",
        "score_windows",
        "levers",
        "path",
        "day",
        "scan_hits",
        "news_items",
        "option_facts",
    }
)

try:
    from abcxauto.strategy_params import OPTION_STRATEGIES
except Exception:  # pragma: no cover
    OPTION_STRATEGIES = frozenset()


def begin_look(snap: dict[str, Any] | None) -> dict[str, Any]:
    """Empty this-look cache. Call at look start and on a live book poke."""
    bag: dict[str, Any] = {"quote": [], "option_quote": [], "book": None}
    if isinstance(snap, dict):
        snap[_SNAP_KEY] = bag
    return bag


def _store(snap: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snap, dict):
        return {"quote": [], "option_quote": [], "book": None}
    bag = snap.get(_SNAP_KEY)
    if not isinstance(bag, dict):
        bag = begin_look(snap)
    return bag


def record_look_tool(snap: dict[str, Any] | None, name: str, payload: Any) -> None:
    """Keep the last quote / option_quote / book result from this look."""
    tool = str(name or "").strip().lower()
    if tool not in LOOK_TOOLS or not isinstance(payload, dict):
        return
    if payload.get("error") and not _payload_has_prints(payload):
        return
    bag = _store(snap)
    if tool == "book":
        bag["book"] = payload
        return
    rows = bag.get(tool)
    if not isinstance(rows, list):
        rows = []
        bag[tool] = rows
    rows.append(payload)
    if len(rows) > 8:
        del rows[:-8]


def _payload_has_prints(payload: dict[str, Any]) -> bool:
    if payload.get("last") is not None or payload.get("mid") is not None:
        return True
    ibkr = payload.get("ibkr")
    if isinstance(ibkr, dict) and (
        ibkr.get("last") is not None or ibkr.get("mid") is not None
    ):
        return True
    quotes = payload.get("quotes")
    return isinstance(quotes, list) and bool(quotes)


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _canon(v: float) -> int:
    return int(round(v * 10000.0))


def _add_num(into: set[int], raw: Any) -> None:
    v = _finite(raw)
    if v is None:
        return
    into.add(_canon(v))


def _sym_of(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("underlying") or "").upper().strip()


class _Bags:
    __slots__ = ("prints", "ivs", "widths", "strikes")

    def __init__(self) -> None:
        self.prints: set[int] = set()
        self.ivs: set[int] = set()
        self.widths: set[int] = set()
        self.strikes: list[float] = []

    def add_iv(self, raw: Any) -> None:
        v = _finite(raw)
        if v is None or v <= 0:
            return
        self.ivs.add(_canon(v))
        if v <= 4.0:
            self.ivs.add(_canon(v * 100.0))
        else:
            self.ivs.add(_canon(v / 100.0))

    def seal_widths(self) -> None:
        seen = [s for s in self.strikes if s is not None]
        for i, a in enumerate(seen):
            for b in seen[i + 1 :]:
                d = abs(float(a) - float(b))
                if d > 1e-9:
                    self.widths.add(_canon(d))


def _bag_for(by_sym: dict[str, _Bags], sym: str) -> _Bags:
    key = str(sym or "").upper().strip() or "*"
    bag = by_sym.get(key)
    if bag is None:
        bag = _Bags()
        by_sym[key] = bag
    return bag


def _walk_row(row: Any, bags: _Bags, *, skip_mda: bool = True) -> None:
    if isinstance(row, list):
        for item in row:
            _walk_row(item, bags, skip_mda=skip_mda)
        return
    if not isinstance(row, dict):
        return
    for key, val in row.items():
        kl = str(key).lower()
        if skip_mda and kl in _SKIP_WALK:
            continue
        if kl in _PRINT_KEYS:
            _add_num(bags.prints, val)
            continue
        if kl in _IV_KEYS:
            bags.add_iv(val)
            continue
        if kl in _CREDIT_KEYS:
            _add_num(bags.prints, val)
            continue
        if kl in _WIDTH_KEYS:
            _add_num(bags.widths, val)
            continue
        if kl in _STRIKE_KEYS:
            sv = _finite(val)
            if sv is not None:
                bags.strikes.append(sv)
            continue
        if isinstance(val, (dict, list)):
            _walk_row(val, bags, skip_mda=skip_mda)


def _harvest_quote_map(qmap: Any, by_sym: dict[str, _Bags]) -> None:
    if not isinstance(qmap, dict):
        return
    for sym, raw in qmap.items():
        bag = _bag_for(by_sym, str(sym))
        if isinstance(raw, dict):
            _walk_row(raw, bag)
        else:
            _add_num(bag.prints, raw)


def _harvest_payload(kind: str, payload: Any, by_sym: dict[str, _Bags]) -> None:
    if not isinstance(payload, dict):
        return
    if kind == "book":
        _harvest_quote_map(payload.get("ibkr_live_quotes"), by_sym)
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        _harvest_quote_map(world.get("ibkr_live_quotes"), by_sym)
        for p in list(world.get("positions") or []) + list(payload.get("positions") or []):
            if not isinstance(p, dict):
                continue
            bag = _bag_for(by_sym, _sym_of(p))
            _walk_row(p, bag)
        return
    rows = []
    if isinstance(payload.get("quotes"), list):
        rows.extend(payload["quotes"])
    else:
        rows.append(payload)
    for row in rows:
        if not isinstance(row, dict):
            continue
        bag = _bag_for(by_sym, _sym_of(row))
        ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else None
        if ibkr is not None:
            _walk_row(ibkr, bag)
            _walk_row({k: v for k, v in row.items() if k != "mda"}, bag)
        else:
            _walk_row(row, bag)


def snapshot_bags(snap: dict[str, Any] | None) -> dict[str, _Bags]:
    store = _store(snap) if isinstance(snap, dict) and _SNAP_KEY in snap else {}
    by_sym: dict[str, _Bags] = {}
    for row in store.get("quote") or []:
        _harvest_payload("quote", row, by_sym)
    for row in store.get("option_quote") or []:
        _harvest_payload("option_quote", row, by_sym)
    book = store.get("book")
    if isinstance(book, dict):
        _harvest_payload("book", book, by_sym)
    for bag in by_sym.values():
        bag.seal_widths()
    return by_sym


def _claimed(params: dict[str, Any], keys: tuple[str, ...]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for key in keys:
        if key not in params:
            continue
        v = _finite(params.get(key))
        if v is None:
            continue
        out.append((key, v))
    return out


def ticket_claims(strategy: str, params: dict[str, Any] | None) -> list[tuple[str, str, float]]:
    """(kind, field, value) Grok put on the ticket. Empty → this gate does not fire."""
    p = params if isinstance(params, dict) else {}
    claims: list[tuple[str, str, float]] = []
    for field, val in _claimed(p, _TICKET_LAST):
        claims.append(("last", field, val))
    for field, val in _claimed(p, _TICKET_IV):
        claims.append(("iv", field, val))
    for field, val in _claimed(p, _TICKET_CREDIT):
        claims.append(("credit", field, val))
    strat = str(strategy or "").strip().lower()
    if strat in OPTION_STRATEGIES:
        for field, val in _claimed(p, ("limit_price",)):
            claims.append(("credit", field, val))
    for field, val in _claimed(p, _TICKET_WIDTH):
        claims.append(("width", field, val))
    return claims


def _in_pool(pool: set[int], value: float) -> bool:
    c = _canon(value)
    if c in pool:
        return True
    # A half-cent tick still counts as the same print.
    return (c - 5) in pool or (c + 5) in pool or (c - 1) in pool or (c + 1) in pool


_COMBO_LIMIT_STRATEGIES = frozenset({"vertical_spread"})
# Marketable crossing slack scales with spread width and natural ask; per-unit only.
_COMBO_LIMIT_TICK = 0.05
_COMBO_LIMIT_CEILING = 0.20
_COMBO_LIMIT_WIDTH_PCT = 0.04
_COMBO_LIMIT_ASK_PCT = 0.10


def _combo_marketable_slack(ctx: dict[str, Any]) -> float:
    """Per-contract slack for paying/receiving through the combo bid/ask."""
    width = max(float(ctx.get("width") or 0.0), 0.0)
    ask = max(float(ctx.get("ask") or 0.0), 0.0)
    width_slack = _COMBO_LIMIT_WIDTH_PCT * width
    ask_slack = _COMBO_LIMIT_ASK_PCT * ask if ask > 0 else 0.0
    band = min(_COMBO_LIMIT_CEILING, max(width_slack, ask_slack))
    return max(_COMBO_LIMIT_TICK, band)


def _norm_expiration(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else digits


def _norm_right(raw: Any) -> str:
    text = str(raw or "C").strip().upper()
    return text[0] if text else "C"


def _leg_key(
    sym: str, expiration: str, strike: float, right: str
) -> tuple[str, str, int, str]:
    return (
        str(sym or "").upper().strip(),
        _norm_expiration(expiration),
        _canon(strike),
        _norm_right(right),
    )


def _leg_prints(row: dict[str, Any]) -> dict[str, Any]:
    ibkr = row.get("ibkr")
    if isinstance(ibkr, dict) and (
        ibkr.get("bid") is not None or ibkr.get("ask") is not None
    ):
        return ibkr
    return row


def _iter_option_quote_rows(snap: dict[str, Any] | None) -> list[dict[str, Any]]:
    store = _store(snap) if isinstance(snap, dict) and _SNAP_KEY in snap else {}
    out: list[dict[str, Any]] = []
    for payload in store.get("option_quote") or []:
        if not isinstance(payload, dict):
            continue
        quotes = payload.get("quotes")
        if isinstance(quotes, list):
            for row in quotes:
                if isinstance(row, dict):
                    out.append(row)
        else:
            out.append(payload)
    return out


def _find_leg_quote(
    snap: dict[str, Any] | None,
    *,
    symbol: str,
    expiration: str,
    strike: float,
    right: str,
) -> dict[str, Any] | None:
    want = _leg_key(symbol, expiration, strike, right)
    for row in _iter_option_quote_rows(snap):
        sk = _finite(row.get("strike"))
        if sk is None:
            continue
        got = _leg_key(
            _sym_of(row) or symbol,
            str(row.get("expiration") or expiration),
            sk,
            str(row.get("right") or right),
        )
        if got == want:
            return _leg_prints(row)
    return None


def _vertical_is_credit(params: dict[str, Any]) -> bool:
    right = _norm_right(params.get("right"))
    long_s = _finite(params.get("long_strike"))
    short_s = _finite(params.get("short_strike"))
    if long_s is None or short_s is None:
        return False
    return (right == "C" and long_s > short_s) or (right == "P" and long_s < short_s)


def _combo_quote_from_legs(
    long_leg: dict[str, Any],
    short_leg: dict[str, Any],
    *,
    is_credit: bool,
) -> dict[str, float | None]:
    long_bid = _finite(long_leg.get("bid"))
    long_ask = _finite(long_leg.get("ask"))
    short_bid = _finite(short_leg.get("bid"))
    short_ask = _finite(short_leg.get("ask"))
    if None in (long_bid, long_ask, short_bid, short_ask):
        return {"bid": None, "ask": None, "mid": None}
    if is_credit:
        bid = short_bid - long_ask
        ask = short_ask - long_bid
    else:
        bid = long_bid - short_ask
        ask = long_ask - short_bid
    mid = (bid + ask) / 2.0
    return {"bid": bid, "ask": ask, "mid": mid}


def _combo_limit_refusal(
    *,
    limit: float,
    combo_label: str,
    bid: float,
    ask: float,
    mid: float,
) -> str:
    return (
        f"{REASON_CODE}: limit_price={limit} {combo_label} "
        f"bid={round(bid, 4)} ask={round(ask, 4)} mid={round(mid, 4)} "
        "not in this look's quote/option_quote/book"
    )


def _vertical_combo_context(
    params: dict[str, Any],
    snap: dict[str, Any] | None,
) -> dict[str, Any] | None:
    sym = str(params.get("symbol") or "").upper().strip()
    exp = str(params.get("expiration") or "")
    long_s = _finite(params.get("long_strike"))
    short_s = _finite(params.get("short_strike"))
    right = _norm_right(params.get("right"))
    if not sym or not exp or long_s is None or short_s is None:
        return None
    long_q = _find_leg_quote(
        snap, symbol=sym, expiration=exp, strike=long_s, right=right
    )
    short_q = _find_leg_quote(
        snap, symbol=sym, expiration=exp, strike=short_s, right=right
    )
    if long_q is None or short_q is None:
        return None
    is_credit = _vertical_is_credit(params)
    combo = _combo_quote_from_legs(long_q, short_q, is_credit=is_credit)
    bid = combo.get("bid")
    ask = combo.get("ask")
    mid = combo.get("mid")
    if bid is None or ask is None or mid is None:
        return None
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "width": abs(long_s - short_s),
        "combo_label": "combo_credit" if is_credit else "combo_debit",
        "is_credit": is_credit,
    }


def _evaluate_vertical_combo_limit(
    limit: float,
    params: dict[str, Any],
    ctx: dict[str, Any],
) -> tuple[bool, str]:
    bid = float(ctx["bid"])
    ask = float(ctx["ask"])
    mid = float(ctx["mid"])
    width = float(ctx["width"])
    combo_label = str(ctx["combo_label"])
    if limit > width + 1e-9:
        return False, _combo_limit_refusal(
            limit=limit,
            combo_label=combo_label,
            bid=bid,
            ask=ask,
            mid=mid,
        )
    is_credit = bool(ctx.get("is_credit"))
    closing = bool(params.get("closing_position"))
    opening_sell = is_credit and not closing or (not is_credit and closing)
    slack = _combo_marketable_slack(ctx)
    if opening_sell:
        if limit + 1e-9 < bid - slack:
            return False, _combo_limit_refusal(
                limit=limit,
                combo_label=combo_label,
                bid=bid,
                ask=ask,
                mid=mid,
            )
    elif limit > ask + slack + 1e-9:
        return False, _combo_limit_refusal(
            limit=limit,
            combo_label=combo_label,
            bid=bid,
            ask=ask,
            mid=mid,
        )
    return True, ""


def _combo_ticket_specified(params: dict[str, Any]) -> bool:
    """Ticket names a 2-leg vertical. Incomplete tickets stay on verbatim."""
    return (
        bool(str(params.get("symbol") or "").strip())
        and bool(str(params.get("expiration") or "").strip())
        and _finite(params.get("long_strike")) is not None
        and _finite(params.get("short_strike")) is not None
    )


def _combo_legs_missing_refusal(
    limit: float,
    params: dict[str, Any],
    snap: dict[str, Any] | None,
) -> str:
    sym = str(params.get("symbol") or "").upper().strip()
    exp = str(params.get("expiration") or "")
    right = _norm_right(params.get("right"))
    long_s = _finite(params.get("long_strike"))
    short_s = _finite(params.get("short_strike"))
    if long_s is not None and short_s is not None:
        legs = f" {sym} {long_s:g}{right}/{short_s:g}{right}" if sym else ""
        long_q = _find_leg_quote(
            snap, symbol=sym, expiration=exp, strike=long_s, right=right
        )
        short_q = _find_leg_quote(
            snap, symbol=sym, expiration=exp, strike=short_s, right=right
        )
        if long_q is not None and short_q is not None:
            return (
                f"{REASON_CODE}: limit_price={limit} option_quote bid/ask "
                f"both legs{legs} this look's quote/option_quote/book"
            )
    else:
        legs = f" {sym}" if sym else ""
    return (
        f"{REASON_CODE}: limit_price={limit} option_quote both legs{legs} "
        "this look's quote/option_quote/book"
    )


def check_combo_limit_geometry(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Combo limit verdict. Prefer check_ticket_numbers (unified gate)."""
    strat = str(strategy or "").strip().lower()
    if strat not in _COMBO_LIMIT_STRATEGIES:
        return True, "ok", ""
    p = params if isinstance(params, dict) else {}
    limit = _finite(p.get("limit_price"))
    if limit is None or not _combo_ticket_specified(p):
        return True, "ok", ""
    ctx = _vertical_combo_context(p, snap)
    if ctx is None:
        return False, REASON_CODE, _combo_legs_missing_refusal(limit, p, snap)
    ok, msg = _evaluate_vertical_combo_limit(limit, p, ctx)
    if ok:
        return True, "ok", ""
    return False, REASON_CODE, msg


def check_ticket_numbers(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Reject when a claimed last / IV / credit / width is not in this look's cache.

    For vertical spreads, limit_price is judged against derived combo bid/ask —
    never a single-leg last. Missing both-leg quotes fail closed (a long-leg
    print used as a combo debit is how IBKR 202 kills the ticket).
    """
    claims = ticket_claims(strategy, params)
    if not claims:
        return True, "ok", ""
    strat = str(strategy or "").strip().lower()
    p = params if isinstance(params, dict) else {}
    combo_limit_ok = False
    if strat in _COMBO_LIMIT_STRATEGIES:
        limit = _finite(p.get("limit_price"))
        if limit is not None and _combo_ticket_specified(p):
            ctx = _vertical_combo_context(p, snap)
            if ctx is None:
                return False, REASON_CODE, _combo_legs_missing_refusal(limit, p, snap)
            ok_combo, combo_msg = _evaluate_vertical_combo_limit(limit, p, ctx)
            if not ok_combo:
                return False, REASON_CODE, combo_msg
            combo_limit_ok = True
    by_sym = snapshot_bags(snap)
    sym = str(p.get("symbol") or "").upper().strip()
    bag = by_sym.get(sym) if sym else None
    missing: list[str] = []
    for kind, field, val in claims:
        if field == "limit_price" and combo_limit_ok:
            continue
        if bag is None:
            pool: set[int] = set()
        elif kind == "iv":
            pool = bag.ivs
        elif kind == "width":
            pool = bag.widths
        else:
            pool = bag.prints
        if not pool or not _in_pool(pool, val):
            missing.append(f"{field}={val}")
    if not missing:
        return True, "ok", ""
    note = (
        f"{REASON_CODE}: {', '.join(missing)} not in this look's "
        "quote/option_quote/book"
    )
    return False, REASON_CODE, note


_COMBO_NOTIONAL_STRATEGIES = frozenset({"vertical_spread"})


def _mid_from_leg_quote(row: dict[str, Any]) -> float | None:
    mid = _finite(row.get("mid"))
    if mid is not None:
        return mid
    bid = _finite(row.get("bid"))
    ask = _finite(row.get("ask"))
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    for key in ("last", "mark", "price"):
        px = _finite(row.get(key))
        if px is not None:
            return px
    return None


def notional_premium_from_look(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> float | None:
    """Option premium per contract from this look's quote/option_quote/book.

    Returns abs(premium) suitable for ×100×qty notional. None when unpriced.
    """
    p = params if isinstance(params, dict) else {}
    strat = str(strategy or "").strip().lower()
    if strat not in OPTION_STRATEGIES:
        return None
    if strat in _COMBO_NOTIONAL_STRATEGIES:
        ctx = _vertical_combo_context(p, snap)
        if ctx is None:
            return None
        mid = _finite(ctx.get("mid"))
        if mid is None:
            return None
        return abs(mid)
    strike = _finite(p.get("strike"))
    exp = str(p.get("expiration") or "")
    sym = str(p.get("symbol") or "").upper().strip()
    right = _norm_right(p.get("right"))
    if not sym or not exp or strike is None:
        return None
    row = _find_leg_quote(
        snap, symbol=sym, expiration=exp, strike=strike, right=right
    )
    if row is None:
        return None
    mid = _mid_from_leg_quote(row)
    return abs(mid) if mid is not None else None


def size_notional_refusal_hint(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> str:
    """Actionable suffix: missing input + tool that supplies it."""
    p = params if isinstance(params, dict) else {}
    strat = str(strategy or "").strip().lower()
    sym = str(p.get("symbol") or "").upper().strip() or "?"
    if strat in _COMBO_NOTIONAL_STRATEGIES:
        long_s = _finite(p.get("long_strike"))
        short_s = _finite(p.get("short_strike"))
        right = _norm_right(p.get("right"))
        legs = ""
        if long_s is not None and short_s is not None:
            legs = f" {long_s:g}{right}/{short_s:g}{right}"
        ctx = _vertical_combo_context(p, snap)
        if ctx is None:
            return (
                f"limit_price missing — option_quote both legs ({sym}{legs}) this look"
            )
        return "limit_price missing — add limit_price from this look's option_quote"
    if strat in OPTION_STRATEGIES:
        strike = _finite(p.get("strike"))
        right = _norm_right(p.get("right"))
        leg = f" {strike:g}{right}" if strike is not None else ""
        if notional_premium_from_look(strat, p, snap) is None:
            return f"limit_price missing — option_quote ({sym}{leg}) this look"
        return "limit_price missing — add limit_price from this look's option_quote"
    if strat in ("bracket", "market_bracket"):
        return f"entry_price missing — quote ({sym}) this look"
    return "price missing — quote this look"
