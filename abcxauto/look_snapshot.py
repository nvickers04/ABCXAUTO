"""This-look quote / option_quote / book numbers for the send gate.

Grok's ticket last / IV / credit / width must appear in those tool results
from THIS look. Unverifiable is a kill, not a pass. Not a verifier agent.

Prints are scoped to the instrument they came from (STK vs OPT, and for
options expiration / right / strike). A stock last cannot verify an option
limit, and the reverse.

Protection geometry (stop_price / target_price) is claimed on new-risk
bracket / market_bracket / oca only. Exits, closing_position, and manage
tickets do not fire those claims.

Rule chosen (SPEC lists last / IV / credit / width; stops are Grok-owned
and are not themselves prints):

* Fact match — the level appears in this look's instrument-scoped prints
  or geometry facts (book aux, session_range tape: low / high / open /
  last / retrace_*). Session tape is this-look fact, not MDA scan last.
* Last-relative derivation — a verified same-instrument IBKR last L
  exists and the level is last-relative: stops must sit within 25% of L
  (walk-away risk/trade ceiling). A farther pin must be an exact
  this-look fact (gap under the open). Targets have no distance cap.
  Legal side (stop below last on a LONG) is ``structure_grade``, not
  this gate — so an inverted but last-relative stop still reaches the
  geometry referee. ``last ± any invented offset`` beyond the ceiling
  is not derived.

Code does not invent a stop from last. An option ticket cannot derive
geometry from a stock last.
"""

from __future__ import annotations

import math
from typing import Any

REASON_CODE = "stale_or_invented_number"
LOOK_TOOLS = ("quote", "option_quote", "book")
_SNAP_KEY = "_look_tool_snapshot"

# Walk-away risk/trade ceiling. A last-relative stop farther than this
# is not derived from that last — it needs an exact this-look fact.
DERIVE_STOP_FRAC = 0.25
_PROTECT_STRATS = frozenset({"bracket", "market_bracket", "oca"})

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
_LAST_KEYS = frozenset(
    {
        "last",
        "price",
        "mkt",
        "market_price",
        "marketprice",
        "lastprice",
        "mid",
        "mark",
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
_GEOM_KEYS = frozenset(
    {
        "aux_price",
        "auxprice",
        "stop_price",
        "target_price",
        "new_stop_price",
        "low",
        "high",
        "session_low",
        "session_high",
        "retrace_30",
        "retrace30",
        "retrace",
        "vwap",
    }
)
_SESSION_GEOM_KEYS = (
    "low",
    "high",
    "open",
    "last",
    "retrace_30",
    "retrace30",
    "retrace",
    "vwap",
    "open_px",
    "session_low",
    "session_high",
)
_TICKET_LAST = ("last", "price_hint", "entry_price")
_TICKET_IV = ("iv", "implied_vol", "impliedVolatility", "implied_volatility")
_TICKET_CREDIT = ("credit", "net_credit", "premium", "net_premium")
_TICKET_WIDTH = ("width", "wing_width")
_TICKET_STOP = ("stop_price", "target_price")
_EXP_KEYS = ("expiration", "expiry", "near_expiration", "far_expiration")
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
        "ticket",
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
    return str(raw or "").replace("-", "").strip()[:8]


def _norm_right(raw: Any) -> str:
    s = str(raw or "").upper().strip()
    if s in {"C", "CALL"}:
        return "C"
    if s in {"P", "PUT"}:
        return "P"
    return s[:1] if s else ""


def _row_is_opt(row: dict[str, Any]) -> bool:
    sec = str(
        row.get("sec") or row.get("secType") or row.get("sec_type") or ""
    ).upper()
    if sec in {"OPT", "FOP"} or sec.startswith("OPT"):
        return True
    if row.get("strike") not in (None, "") and (
        row.get("right") or row.get("expiration") or row.get("expiry")
    ):
        return True
    return False


def _inst_key(kind: str, row: dict[str, Any], default_sym: str = "") -> tuple:
    sym = _sym_of(row) or str(default_sym or "").upper().strip()
    if kind == "option_quote" or (kind != "quote" and _row_is_opt(row)):
        strike = _finite(row.get("strike"))
        return (
            "OPT",
            sym,
            _norm_exp(row.get("expiration") or row.get("expiry")),
            _norm_right(row.get("right")),
            _canon(strike) if strike is not None else None,
        )
    return ("STK", sym)


class _Bags:
    __slots__ = ("prints", "ivs", "widths", "strikes", "geom", "lasts")

    def __init__(self) -> None:
        self.prints: set[int] = set()
        self.ivs: set[int] = set()
        self.widths: set[int] = set()
        self.strikes: list[float] = []
        self.geom: set[int] = set()
        self.lasts: list[float] = []

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


def _bag_for(by_inst: dict[tuple, _Bags], key: tuple) -> _Bags:
    bag = by_inst.get(key)
    if bag is None:
        bag = _Bags()
        by_inst[key] = bag
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
            if kl in _LAST_KEYS:
                fv = _finite(val)
                if fv is not None and fv > 0:
                    bags.lasts.append(fv)
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
        if kl in _GEOM_KEYS:
            _add_num(bags.geom, val)
            continue
        if isinstance(val, (dict, list)):
            _walk_row(val, bags, skip_mda=skip_mda)


def _harvest_quote_map(qmap: Any, by_inst: dict[tuple, _Bags]) -> None:
    if not isinstance(qmap, dict):
        return
    for sym, raw in qmap.items():
        bag = _bag_for(by_inst, ("STK", str(sym or "").upper().strip() or "*"))
        if isinstance(raw, dict):
            _walk_row(raw, bag)
        else:
            _add_num(bag.prints, raw)
            fv = _finite(raw)
            if fv is not None and fv > 0:
                bag.lasts.append(fv)


def _harvest_payload(kind: str, payload: Any, by_inst: dict[tuple, _Bags]) -> None:
    if not isinstance(payload, dict):
        return
    if kind == "book":
        _harvest_quote_map(payload.get("ibkr_live_quotes"), by_inst)
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        _harvest_quote_map(world.get("ibkr_live_quotes"), by_inst)
        for p in list(world.get("positions") or []) + list(payload.get("positions") or []):
            if not isinstance(p, dict):
                continue
            bag = _bag_for(by_inst, _inst_key("book", p))
            _walk_row(p, bag)
        return
    rows = []
    if isinstance(payload.get("quotes"), list):
        rows.extend(payload["quotes"])
    else:
        rows.append(payload)
    default_sym = _sym_of(payload)
    for row in rows:
        if not isinstance(row, dict):
            continue
        bag = _bag_for(by_inst, _inst_key(kind, row, default_sym))
        ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else None
        if ibkr is not None:
            _walk_row(ibkr, bag)
            _walk_row({k: v for k, v in row.items() if k != "mda"}, bag)
        else:
            _walk_row(row, bag)


def _harvest_session_range(raw: Any, by_inst: dict[tuple, _Bags]) -> None:
    """Session tape is geometry fact, not a last/credit print."""
    if not isinstance(raw, dict):
        return
    skip = {k.lower() for k in _SESSION_GEOM_KEYS}
    for sym, row in raw.items():
        if not isinstance(row, dict):
            continue
        if str(sym or "").lower() in skip:
            continue
        key = str(sym or "").upper().strip()
        if not key:
            continue
        bag = _bag_for(by_inst, ("STK", key))
        for field in _SESSION_GEOM_KEYS:
            _add_num(bag.geom, row.get(field))


def snapshot_bags(snap: dict[str, Any] | None) -> dict[tuple, _Bags]:
    store = _store(snap) if isinstance(snap, dict) and _SNAP_KEY in snap else {}
    by_inst: dict[tuple, _Bags] = {}
    for row in store.get("quote") or []:
        _harvest_payload("quote", row, by_inst)
    for row in store.get("option_quote") or []:
        _harvest_payload("option_quote", row, by_inst)
    book = store.get("book")
    if isinstance(book, dict):
        _harvest_payload("book", book, by_inst)
    if isinstance(snap, dict):
        _harvest_quote_map(snap.get("ibkr_live_quotes"), by_inst)
        _harvest_session_range(snap.get("session_range"), by_inst)
    for bag in by_inst.values():
        bag.seal_widths()
    return by_inst


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


def _protection_claims_apply(strategy: str, params: dict[str, Any]) -> bool:
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
    if _protection_claims_apply(strat, p):
        for field, val in _claimed(p, _TICKET_STOP):
            claims.append(("stop", field, val))
    return claims


def _in_pool(pool: set[int], value: float) -> bool:
    c = _canon(value)
    if c in pool:
        return True
    # A half-cent tick still counts as the same print.
    return (c - 5) in pool or (c + 5) in pool or (c - 1) in pool or (c + 1) in pool


def _ticket_is_option(strategy: str, params: dict[str, Any]) -> bool:
    if str(strategy or "").strip().lower() in OPTION_STRATEGIES:
        return True
    if params.get("expiration") or params.get("expiry") or params.get("right"):
        return True
    return params.get("strike") not in (None, "")


def _opt_filters(
    params: dict[str, Any],
) -> tuple[str, set[str], set[str], set[int]]:
    sym = str(params.get("symbol") or "").upper().strip()
    exps: set[str] = set()
    for key in _EXP_KEYS:
        exp = _norm_exp(params.get(key))
        if exp:
            exps.add(exp)
    rights: set[str] = set()
    right = _norm_right(params.get("right"))
    if right:
        rights.add(right)
    for key in params:
        kl = str(key).lower()
        if "strike" not in kl:
            continue
        if "put" in kl:
            rights.add("P")
        if "call" in kl:
            rights.add("C")
    strikes: set[int] = set()
    for key in _STRIKE_KEYS:
        v = _finite(params.get(key))
        if v is not None:
            strikes.add(_canon(v))
    return sym, exps, rights, strikes


def _opt_key_matches(
    key: tuple,
    sym: str,
    exps: set[str],
    rights: set[str],
    strikes: set[int],
) -> bool:
    if len(key) < 5 or key[0] != "OPT" or key[1] != sym:
        return False
    exp, right, strike = key[2], key[3], key[4]
    if exps and exp and exp not in exps:
        return False
    if rights and right and right not in rights:
        return False
    if strikes and strike is not None and strike not in strikes:
        return False
    return True


def _merge_bags(bags: list[_Bags]) -> _Bags:
    out = _Bags()
    for bag in bags:
        out.prints |= bag.prints
        out.ivs |= bag.ivs
        out.widths |= bag.widths
        out.strikes.extend(bag.strikes)
        out.geom |= bag.geom
        out.lasts.extend(bag.lasts)
    out.seal_widths()
    return out


def _bags_for_ticket(
    by_inst: dict[tuple, _Bags],
    strategy: str,
    params: dict[str, Any] | None,
) -> _Bags:
    p = params if isinstance(params, dict) else {}
    sym = str(p.get("symbol") or "").upper().strip()
    if not sym:
        return _Bags()
    if _ticket_is_option(strategy, p):
        _sym, exps, rights, strikes = _opt_filters(p)
        chosen = [
            bag
            for key, bag in by_inst.items()
            if _opt_key_matches(key, _sym or sym, exps, rights, strikes)
        ]
        return _merge_bags(chosen)
    return _merge_bags(
        [bag for key, bag in by_inst.items() if key[:2] == ("STK", sym)]
    )


def _derived_protection(
    level: float,
    lasts: list[float],
    field: str,
    direction: str,
) -> bool:
    """True when level is last-relative. Side is structure_grade's job."""
    _ = direction
    for last in lasts:
        if last <= 0:
            continue
        frac = abs(level - last) / last
        if field == "stop_price" and frac > DERIVE_STOP_FRAC + 1e-12:
            continue
        if field in {"stop_price", "target_price"}:
            return True
    return False


def check_ticket_numbers(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Reject when a claimed last / IV / credit / width / stop is not in this look."""
    claims = ticket_claims(strategy, params)
    if not claims:
        return True, "ok", ""
    by_inst = snapshot_bags(snap)
    bag = _bags_for_ticket(by_inst, strategy, params)
    direction = str((params or {}).get("direction") or "").upper()
    missing: list[str] = []
    for kind, field, val in claims:
        if kind == "iv":
            pool = bag.ivs
        elif kind == "width":
            pool = bag.widths
        elif kind == "stop":
            pool = bag.prints | bag.geom
        else:
            pool = bag.prints
        if pool and _in_pool(pool, val):
            continue
        if kind == "stop" and _derived_protection(val, bag.lasts, field, direction):
            continue
        missing.append(f"{field}={val}")
    if not missing:
        return True, "ok", ""
    if isinstance(params, dict) and params.get("closing_position") is True:
        flag = "closing_position=true"
    else:
        flag = "closing_position=false"
    if not bag.prints:
        detail = "legs missing from this look's cache"
    else:
        shown = ", ".join(
            f"{c / 10000.0:g}" for c in sorted(bag.prints)[:16]
        )
        detail = f"prints=[{shown}]"
    note = (
        f"{REASON_CODE}: {', '.join(missing)} not in this look's "
        f"quote/option_quote/book; {flag}; {detail}"
    )
    return False, REASON_CODE, note
