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
# Derived combo nets use one equity-option minTick (broker default 0.01).
# Identity matching stays at _in_pool (+/-1 or +/-5 canon = $0.0001-$0.0005).
DERIVE_COMBO_TICK = 0.01
_PROTECT_STRATS = frozenset({"bracket", "market_bracket", "oca"})
_MDA_SOURCES = frozenset({"mda", "marketdata", "market_data"})

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
    if sec == "BAG":
        return False
    if sec in {"OPT", "FOP"} or sec.startswith("OPT"):
        return True
    if row.get("strike") not in (None, "") and (
        row.get("right") or row.get("expiration") or row.get("expiry")
    ):
        return True
    return False


def _bag_pair(row: dict[str, Any]) -> tuple[int, int] | None:
    a = _finite(row.get("long_strike"))
    b = _finite(row.get("short_strike"))
    if a is None or b is None:
        return None
    pair = tuple(sorted((_canon(a), _canon(b))))
    if pair[0] == pair[1]:
        return None
    return pair  # type: ignore[return-value]


def _row_is_bag(row: dict[str, Any]) -> bool:
    sec = str(
        row.get("sec") or row.get("secType") or row.get("sec_type") or ""
    ).upper()
    if sec == "BAG":
        return True
    if sec in {"OPT", "FOP", "STK"} or sec.startswith("OPT"):
        return False
    return _bag_pair(row) is not None


def _inst_key(kind: str, row: dict[str, Any], default_sym: str = "") -> tuple:
    sym = _sym_of(row) or str(default_sym or "").upper().strip()
    if kind != "quote" and _row_is_bag(row):
        return (
            "BAG",
            sym,
            _norm_exp(row.get("expiration") or row.get("expiry")),
            _norm_right(row.get("right")),
            _bag_pair(row),
        )
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
    if len(key) < 5 or key[0] not in {"OPT", "BAG"} or key[1] != sym:
        return False
    exp, right, strike = key[2], key[3], key[4]
    if exps and exp and exp not in exps:
        return False
    if rights and right and right not in rights:
        return False
    if key[0] == "BAG":
        pair = strike
        if strikes and pair is not None and not set(pair).issubset(strikes):
            return False
        return True
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


def _combo_strikes(params: dict[str, Any] | None) -> tuple[float, float] | None:
    p = params if isinstance(params, dict) else {}
    long_k = _finite(p.get("long_strike"))
    short_k = _finite(p.get("short_strike"))
    if long_k is None or short_k is None:
        return None
    if _canon(long_k) == _canon(short_k):
        return None
    return long_k, short_k


def _is_combo_ticket(params: dict[str, Any] | None) -> bool:
    return _combo_strikes(params) is not None


def _ibkr_leg_px(row: dict[str, Any]) -> dict[str, float] | None:
    """IBKR bid/ask/mid/last from one option_quote row. Never MDA."""
    if not isinstance(row, dict) or _row_is_bag(row):
        return None
    ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else None
    src = ibkr if ibkr is not None else row
    source = str(src.get("source") or row.get("source") or "").lower()
    freshness = str(src.get("freshness") or row.get("freshness") or "").lower()
    if source in _MDA_SOURCES or "delay" in freshness:
        return None
    if ibkr is None and source and source != "ibkr":
        return None
    out: dict[str, float] = {}
    for key in ("bid", "ask", "mid", "last"):
        fv = _finite(src.get(key))
        if fv is not None:
            out[key] = fv
    if not out:
        return None
    return out


def _collect_ibkr_legs(
    snap: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> dict[int, dict[str, float]]:
    """this-look option_quote IBKR prints keyed by strike canon."""
    pair = _combo_strikes(params)
    if not pair:
        return {}
    p = params if isinstance(params, dict) else {}
    sym = str(p.get("symbol") or "").upper().strip()
    exp = _norm_exp(p.get("expiration") or p.get("expiry"))
    right = _norm_right(p.get("right"))
    want = {_canon(pair[0]), _canon(pair[1])}
    found: dict[int, dict[str, float]] = {}
    store = _store(snap) if isinstance(snap, dict) else {
        "quote": [], "option_quote": [], "book": None
    }
    for payload in store.get("option_quote") or []:
        if not isinstance(payload, dict):
            continue
        rows = (
            payload["quotes"]
            if isinstance(payload.get("quotes"), list)
            else [payload]
        )
        for row in rows:
            if not isinstance(row, dict) or _row_is_bag(row):
                continue
            strike = _finite(row.get("strike"))
            if strike is None:
                continue
            ck = _canon(strike)
            if ck not in want:
                continue
            row_sym = _sym_of(row)
            if row_sym and sym and row_sym != sym:
                continue
            row_exp = _norm_exp(row.get("expiration") or row.get("expiry"))
            if exp and row_exp and row_exp != exp:
                continue
            row_right = _norm_right(row.get("right"))
            if right and row_right and row_right != right:
                continue
            px = _ibkr_leg_px(row)
            if px is None:
                continue
            found[ck] = px
    return found


def _derived_combo_nets(
    legs: dict[int, dict[str, float]],
    long_k: float,
    short_k: float,
) -> dict[str, float]:
    long_px = legs.get(_canon(long_k))
    short_px = legs.get(_canon(short_k))
    if not long_px or not short_px:
        return {}
    lb, la = long_px.get("bid"), long_px.get("ask")
    sb, sa = short_px.get("bid"), short_px.get("ask")
    lm, sm = long_px.get("mid"), short_px.get("mid")
    if lm is None and lb is not None and la is not None:
        lm = (lb + la) / 2.0
    if sm is None and sb is not None and sa is not None:
        sm = (sb + sa) / 2.0
    out: dict[str, float] = {"width": abs(float(long_k) - float(short_k))}
    if lb is not None and sa is not None:
        out["bid"] = lb - sa
    if la is not None and sb is not None:
        out["ask"] = la - sb
    if lm is not None and sm is not None:
        out["mid"] = lm - sm
    return out


def _combo_bag_prints(
    by_inst: dict[tuple, _Bags],
    params: dict[str, Any] | None,
) -> set[int]:
    pair = _combo_strikes(params)
    if not pair:
        return set()
    p = params if isinstance(params, dict) else {}
    sym = str(p.get("symbol") or "").upper().strip()
    exp = _norm_exp(p.get("expiration") or p.get("expiry"))
    right = _norm_right(p.get("right"))
    want = tuple(sorted((_canon(pair[0]), _canon(pair[1]))))
    prints: set[int] = set()
    for key, bag in by_inst.items():
        if key[0] != "BAG" or key[1] != sym:
            continue
        if exp and key[2] and key[2] != exp:
            continue
        if right and key[3] and key[3] != right:
            continue
        if key[4] != want:
            continue
        prints |= bag.prints
    return prints


def _in_derived_combo_range(val: float, derived: dict[str, float]) -> bool:
    if not derived:
        return False
    exact: set[int] = set()
    for key in ("bid", "ask", "mid", "width"):
        fv = derived.get(key)
        if fv is None:
            continue
        exact.add(_canon(fv))
        exact.add(_canon(abs(fv)))
    if exact and _in_pool(exact, val):
        return True
    sides = [derived[k] for k in ("bid", "ask") if k in derived]
    if len(sides) < 2:
        return False
    lo = min(abs(sides[0]), abs(sides[1]))
    hi = max(abs(sides[0]), abs(sides[1]))
    tick = DERIVE_COMBO_TICK
    av = abs(val)
    if lo - tick - 1e-12 <= av <= hi + tick + 1e-12:
        return True
    slo, shi = min(sides[0], sides[1]), max(sides[0], sides[1])
    return slo - tick - 1e-12 <= val <= shi + tick + 1e-12


def _combo_credit_ok(
    val: float,
    by_inst: dict[tuple, _Bags],
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> bool:
    bag_prints = _combo_bag_prints(by_inst, params)
    if bag_prints and (_in_pool(bag_prints, val) or _in_pool(bag_prints, abs(val))):
        return True
    pair = _combo_strikes(params)
    if not pair:
        return False
    derived = _derived_combo_nets(_collect_ibkr_legs(snap, params), pair[0], pair[1])
    return _in_derived_combo_range(val, derived)


def _combo_refusal_extra(
    snap: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> str:
    if not _is_combo_ticket(params):
        return ""
    pair = _combo_strikes(params)
    derived = (
        _derived_combo_nets(_collect_ibkr_legs(snap, params), pair[0], pair[1])
        if pair
        else {}
    )
    bid, ask = derived.get("bid"), derived.get("ask")
    if bid is not None and ask is not None:
        lo, hi = min(abs(bid), abs(ask)), max(abs(bid), abs(ask))
        market = f"derived_combo bid={lo:g} ask={hi:g}"
    elif bid is not None or ask is not None:
        side = bid if bid is not None else ask
        market = f"derived_combo bid={abs(side):g} ask="
    else:
        market = "derived_combo bid= ask="
    return (
        f"{market}; call option_quote with long_strike and short_strike "
        f"for a live BAG net"
    )


def check_ticket_numbers(
    strategy: str,
    params: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> tuple[bool, str, str]:
    """Reject when a claimed last / IV / credit / width / stop is not in this look."""
    claims = ticket_claims(strategy, params)
    p = params if isinstance(params, dict) else {}
    combo = _is_combo_ticket(p)
    combo_needs_limit = (
        combo
        and p.get("closing_position") is not True
        and _finite(p.get("limit_price")) is None
    )
    if not claims and not combo_needs_limit:
        return True, "ok", ""
    by_inst = snapshot_bags(snap)
    bag = _bags_for_ticket(by_inst, strategy, params)
    direction = str(p.get("direction") or "").upper()
    missing: list[str] = []
    if combo_needs_limit:
        missing.append("limit_price")
    for kind, field, val in claims:
        if kind == "credit" and combo:
            if _combo_credit_ok(val, by_inst, p, snap):
                continue
            missing.append(f"{field}={val}")
            continue
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
    if p.get("closing_position") is True:
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
    extra = _combo_refusal_extra(snap, p) if combo else ""
    if extra:
        note = f"{note}; {extra}"
    return False, REASON_CODE, note
