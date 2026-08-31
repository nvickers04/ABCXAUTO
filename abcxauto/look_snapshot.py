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


def check_ticket_numbers(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Reject when a claimed last / IV / credit / width is not in this look's cache."""
    claims = ticket_claims(strategy, params)
    if not claims:
        return True, "ok", ""
    by_sym = snapshot_bags(snap)
    sym = str((params or {}).get("symbol") or "").upper().strip()
    bag = by_sym.get(sym) if sym else None
    missing: list[str] = []
    for kind, field, val in claims:
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
