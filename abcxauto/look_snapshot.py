"""This-look quote / option_quote / book numbers for the send gate.

Grok's ticket last / IV / credit / width must appear in those tool results
from THIS look. Unverifiable is a kill, not a pass. Not a verifier agent.

Prints are scoped to the instrument they came from (STK vs OPT, and for
options expiration / right / strike). An option limit cannot verify against
an unrelated stock last.

Protection geometry (new-risk bracket ``stop_price`` / ``target_price``):
exits and ``closing_position`` stay unblocked. A stop/target is verified if
it is (a) a this-look STK print for that symbol, (b) a this-look session
pin (``session_range`` high / low / open / retrace_*), or (c) derived from
this look's IBKR last for that symbol — LONG: stop < last < target; SHORT:
target < last < stop. No last and no pin is invented, and is a kill.
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
_LIVE_LAST_KEYS = frozenset(
    {"last", "price", "lastprice", "mkt", "market_price", "marketprice"}
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
_PUT_STRIKE_KEYS = frozenset(
    {"put_long_strike", "put_short_strike", "put_strike"}
)
_CALL_STRIKE_KEYS = frozenset(
    {"call_long_strike", "call_short_strike", "call_strike"}
)
_TICKET_LAST = ("last", "price_hint", "entry_price")
_TICKET_IV = ("iv", "implied_vol", "impliedVolatility", "implied_volatility")
_TICKET_CREDIT = ("credit", "net_credit", "premium", "net_premium")
_TICKET_WIDTH = ("width", "wing_width")
_TICKET_STOP = ("stop_price",)
_TICKET_TARGET = ("target_price",)
_PROTECT_STRATS = frozenset({"bracket", "market_bracket"})
_SESSION_LEVEL_KEYS = frozenset({"high", "low", "open"})
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


def _norm_exp(raw: Any) -> str:
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return digits


def _norm_right(raw: Any) -> str:
    token = str(raw or "").strip().upper()
    if token[:1] in ("C", "P"):
        return token[:1]
    if token in ("CALL", "PUT"):
        return "C" if token == "CALL" else "P"
    return ""


def _is_opt_row(row: dict[str, Any], kind: str) -> bool:
    if kind == "option_quote":
        return True
    if kind == "quote":
        return False
    sec = str(
        row.get("sec") or row.get("sec_type") or row.get("secType") or ""
    ).upper()
    return sec in ("OPT", "FOP")


def _opt_key(row: dict[str, Any]) -> tuple[str, str, int | None]:
    strike = _finite(row.get("strike"))
    return (
        _norm_exp(
            row.get("expiration")
            or row.get("expiry")
            or row.get("lastTradeDateOrContractMonth")
        ),
        _norm_right(row.get("right")),
        _canon(strike) if strike is not None else None,
    )


class _Bags:
    __slots__ = ("prints", "ivs", "widths", "strikes", "live_lasts")

    def __init__(self) -> None:
        self.prints: set[int] = set()
        self.ivs: set[int] = set()
        self.widths: set[int] = set()
        self.strikes: list[float] = []
        self.live_lasts: list[float] = []

    def add_live_last(self, raw: Any) -> None:
        v = _finite(raw)
        if v is None or v <= 0:
            return
        self.live_lasts.append(v)

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


class _Inst:
    __slots__ = ("stk", "opt")

    def __init__(self) -> None:
        self.stk = _Bags()
        self.opt: dict[tuple[str, str, int | None], _Bags] = {}


def _inst_for(by_sym: dict[str, _Inst], sym: str) -> _Inst:
    key = str(sym or "").upper().strip() or "*"
    inst = by_sym.get(key)
    if inst is None:
        inst = _Inst()
        by_sym[key] = inst
    return inst


def _stk_bag(by_sym: dict[str, _Inst], sym: str) -> _Bags:
    return _inst_for(by_sym, sym).stk


def _opt_bag(by_sym: dict[str, _Inst], sym: str, row: dict[str, Any]) -> _Bags:
    inst = _inst_for(by_sym, sym)
    key = _opt_key(row)
    bag = inst.opt.get(key)
    if bag is None:
        bag = _Bags()
        inst.opt[key] = bag
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
            if kl in _LIVE_LAST_KEYS:
                bags.add_live_last(val)
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


def _harvest_quote_map(qmap: Any, by_sym: dict[str, _Inst]) -> None:
    if not isinstance(qmap, dict):
        return
    for sym, raw in qmap.items():
        bag = _stk_bag(by_sym, str(sym))
        if isinstance(raw, dict):
            _walk_row(raw, bag)
        else:
            _add_num(bag.prints, raw)
            bag.add_live_last(raw)


def _walk_quote_row(row: dict[str, Any], bags: _Bags) -> None:
    ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else None
    if ibkr is not None:
        _walk_row(ibkr, bags)
        _walk_row({k: v for k, v in row.items() if k not in ("mda", "ibkr")}, bags)
        return
    _walk_row(row, bags)


def _harvest_payload(kind: str, payload: Any, by_sym: dict[str, _Inst]) -> None:
    if not isinstance(payload, dict):
        return
    if kind == "book":
        _harvest_quote_map(payload.get("ibkr_live_quotes"), by_sym)
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        _harvest_quote_map(world.get("ibkr_live_quotes"), by_sym)
        for p in list(world.get("positions") or []) + list(payload.get("positions") or []):
            if not isinstance(p, dict):
                continue
            if _is_opt_row(p, "book"):
                _walk_quote_row(p, _opt_bag(by_sym, _sym_of(p), p))
            else:
                _walk_quote_row(p, _stk_bag(by_sym, _sym_of(p)))
        return
    rows = []
    if isinstance(payload.get("quotes"), list):
        rows.extend(payload["quotes"])
    else:
        rows.append(payload)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _is_opt_row(row, kind):
            _walk_quote_row(row, _opt_bag(by_sym, _sym_of(row), row))
        else:
            _walk_quote_row(row, _stk_bag(by_sym, _sym_of(row)))


def _harvest_snap_live(snap: dict[str, Any], by_sym: dict[str, _Inst]) -> None:
    """This-look IBKR tape on the snap (not MDA scan). STK last only."""
    qmap = snap.get("ibkr_live_quotes")
    if isinstance(qmap, dict):
        _harvest_quote_map(qmap, by_sym)
    if snap.get("ibkr_live_last") is None:
        return
    sym = str(snap.get("ibkr_live_symbol") or "").upper().strip()
    if not sym:
        return
    bag = _stk_bag(by_sym, sym)
    _add_num(bag.prints, snap.get("ibkr_live_last"))
    bag.add_live_last(snap.get("ibkr_live_last"))


def snapshot_bags(snap: dict[str, Any] | None) -> dict[str, _Inst]:
    store = _store(snap) if isinstance(snap, dict) and _SNAP_KEY in snap else {}
    by_sym: dict[str, _Inst] = {}
    for row in store.get("quote") or []:
        _harvest_payload("quote", row, by_sym)
    for row in store.get("option_quote") or []:
        _harvest_payload("option_quote", row, by_sym)
    book = store.get("book")
    if isinstance(book, dict):
        _harvest_payload("book", book, by_sym)
    if isinstance(snap, dict):
        _harvest_snap_live(snap, by_sym)
    for inst in by_sym.values():
        inst.stk.seal_widths()
        for bag in inst.opt.values():
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


def _protection_geometry_applies(strategy: str, params: dict[str, Any]) -> bool:
    """New-risk brackets only. Exits / last-stop manage stay unblocked."""
    if str(strategy or "").strip().lower() not in _PROTECT_STRATS:
        return False
    return params.get("closing_position") is not True


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
    if _protection_geometry_applies(strat, p):
        for field, val in _claimed(p, _TICKET_STOP):
            claims.append(("stop", field, val))
        for field, val in _claimed(p, _TICKET_TARGET):
            claims.append(("target", field, val))
    return claims


def _in_pool(pool: set[int], value: float) -> bool:
    c = _canon(value)
    if c in pool:
        return True
    # A half-cent tick still counts as the same print.
    return (c - 5) in pool or (c + 5) in pool or (c - 1) in pool or (c + 1) in pool


def _ticket_strikes(params: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for key in _STRIKE_KEYS:
        v = _finite(params.get(key))
        if v is not None:
            out.add(_canon(v))
    return out


def _ticket_rights(params: dict[str, Any]) -> set[str]:
    rights: set[str] = set()
    one = _norm_right(params.get("right"))
    if one:
        rights.add(one)
    if any(params.get(k) not in (None, "") for k in _PUT_STRIKE_KEYS):
        rights.add("P")
    if any(params.get(k) not in (None, "") for k in _CALL_STRIKE_KEYS):
        rights.add("C")
    return rights


def _ticket_exps(params: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("expiration", "expiry", "near_expiration", "far_expiration"):
        exp = _norm_exp(params.get(key))
        if exp:
            out.add(exp)
    return out


def _option_bags_for_ticket(inst: _Inst, params: dict[str, Any]) -> list[_Bags]:
    """Option bags that match ticket legs. No identity → all option bags.

    Wildcard rows (no expiry/right/strike on the quote) stay eligible so a
    bare option_quote still binds credit. A named contract never sees STK.
    """
    if not inst.opt:
        return []
    exps = _ticket_exps(params)
    rights = _ticket_rights(params)
    strikes = _ticket_strikes(params)
    if not exps and not rights and not strikes:
        return list(inst.opt.values())
    out: list[_Bags] = []
    for (exp, right, strike), bag in inst.opt.items():
        if exps and exp and exp not in exps:
            continue
        if rights and right and right not in rights:
            continue
        if strikes and strike is not None and strike not in strikes:
            continue
        out.append(bag)
    return out


def _merge_prints(bags: list[_Bags]) -> set[int]:
    out: set[int] = set()
    for bag in bags:
        out |= bag.prints
    return out


def _merge_ivs(bags: list[_Bags]) -> set[int]:
    out: set[int] = set()
    for bag in bags:
        out |= bag.ivs
    return out


def _option_widths(bags: list[_Bags]) -> set[int]:
    widths: set[int] = set()
    strikes: list[float] = []
    for bag in bags:
        widths |= bag.widths
        strikes.extend(bag.strikes)
    for i, a in enumerate(strikes):
        for b in strikes[i + 1 :]:
            d = abs(float(a) - float(b))
            if d > 1e-9:
                widths.add(_canon(d))
    return widths


def _session_levels(snap: dict[str, Any] | None, symbol: str) -> set[int]:
    if not isinstance(snap, dict) or not symbol:
        return set()
    store = snap.get("session_range")
    if not isinstance(store, dict):
        return set()
    row = store.get(symbol)
    if not isinstance(row, dict):
        return set()
    levels: set[int] = set()
    for key, val in row.items():
        kl = str(key).lower()
        if kl in _SESSION_LEVEL_KEYS or kl.startswith("retrace"):
            _add_num(levels, val)
    return levels


def _derived_from_last(
    kind: str,
    value: float,
    lasts: list[float],
    direction: str,
) -> bool:
    if not lasts:
        return False
    lo = min(lasts)
    hi = max(lasts)
    side = str(direction or "").upper()
    if side == "LONG":
        if kind == "stop":
            return value < lo
        if kind == "target":
            return value > hi
        return False
    if side == "SHORT":
        if kind == "stop":
            return value > hi
        if kind == "target":
            return value < lo
        return False
    return False


def _protection_verified(
    kind: str,
    value: float,
    inst: _Inst | None,
    params: dict[str, Any],
    snap: dict[str, Any] | None,
    symbol: str,
) -> bool:
    stk = inst.stk if inst is not None else _Bags()
    if stk.prints and _in_pool(stk.prints, value):
        return True
    session = _session_levels(snap, symbol)
    if session and _in_pool(session, value):
        return True
    lasts = list(stk.live_lasts)
    for _field, claimed in _claimed(params, _TICKET_LAST):
        if stk.prints and _in_pool(stk.prints, claimed):
            lasts.append(claimed)
    return _derived_from_last(kind, value, lasts, str(params.get("direction") or ""))


def _pool_for_claim(
    kind: str,
    inst: _Inst | None,
    params: dict[str, Any],
) -> set[int]:
    if inst is None:
        return set()
    if kind == "last":
        return inst.stk.prints
    bags = _option_bags_for_ticket(inst, params)
    if kind == "iv":
        return _merge_ivs(bags)
    if kind == "width":
        return _option_widths(bags)
    return _merge_prints(bags)


def check_ticket_numbers(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Reject when a claimed last / IV / credit / width / stop is not in this look."""
    claims = ticket_claims(strategy, params)
    if not claims:
        return True, "ok", ""
    by_sym = snapshot_bags(snap)
    p = params if isinstance(params, dict) else {}
    sym = str(p.get("symbol") or "").upper().strip()
    inst = by_sym.get(sym) if sym else None
    missing: list[str] = []
    for kind, field, val in claims:
        if kind in ("stop", "target"):
            if not _protection_verified(kind, val, inst, p, snap, sym):
                missing.append(f"{field}={val}")
            continue
        pool = _pool_for_claim(kind, inst, p)
        if not pool or not _in_pool(pool, val):
            missing.append(f"{field}={val}")
    if not missing:
        return True, "ok", ""
    note = (
        f"{REASON_CODE}: {', '.join(missing)} not in this look's "
        "quote/option_quote/book"
    )
    return False, REASON_CODE, note
