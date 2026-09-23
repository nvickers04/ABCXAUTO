"""Kill-window LOOK contract (thin prompt, not SYSTEM_PROMPT).

Hard clerk: F10 $15 hard / $10 preferred, paper 7497, named-card
defined-risk send, RTH no-xhigh. All tools every session; send stays
blocked outside RTH. No weekly AH look quota. Not looking.
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

MODE_OPEN = "open"
MODE_MANAGE = "manage"
MODE_ABORT = "abort"
MODE_RESEARCH = "research"

PCS_CARD = "pcs-skew"
PCS_STRATEGY = "vertical_spread"

# F10 dollars. Not raiseable Settings knobs. Preferred is an ops tripwire.
F10_HARD_USD = 15.0
F10_PREFERRED_USD = 10.0
WINDOW_MODEL_USD = 40.0
WINDOW_N = 20
# 0 = no weekly AH look quota. Hunt schemas are ~0.2¢; F10 + metering own cost.
AH_RESEARCH_LOOKS_PER_WEEK = 0
RESEARCH_PROMPT_TOKENS_MAX = 200_000
# Conservative look estimate. Used as est_this_look in the F10 sum.
EST_THIS_LOOK_USD = 0.35

REASON_F10 = "NO_SEND:f10"
REASON_DD = "NO_SEND:dd_fuse"
REASON_QTY0 = "NO_SEND:qty0_streak"
REASON_MODEL_COST = "NO_SEND:model_cost_cap"
REASON_STRUCTURE = "kill_look_structure"
REASON_NAMELESS = "kill_look_nameless"
REASON_RESEARCH_WEEK = "kill_look_research_week"
REASON_RESEARCH_PROMPT = "kill_look_research_prompt"
REASON_BRIEF_LOOP = "brief_loop_halted"
REASON_BRIEF_COST = "brief_model_cost_missing"
REASON_PORT = "kill_look_live_port"
REASON_BOOK_UNRELIABLE = "book_unreliable"
REASON_IBKR_DOWN = "ibkr_down"

LIVE_PORTS = frozenset({7496, 4001})

_XHIGH_RE = re.compile(r"-xhigh\b", re.IGNORECASE)


def kill_look_enabled(cfg: Any = None) -> bool:
    """Kill-window LOOK contract. Env wins. Unset → Config default True."""
    raw = (os.environ.get("ABCXAUTO_PCS_KILL_LOOK") or "").strip()
    if raw:
        return raw.lower() in ("1", "true", "yes", "on")
    if cfg is None:
        try:
            from abcxauto.config import get_config

            cfg = get_config()
        except Exception:
            return True
    if hasattr(cfg, "pcs_kill_look"):
        return bool(getattr(cfg, "pcs_kill_look"))
    return True


def kill_look_port_ok(cfg: Any = None) -> bool:
    """Paper 7497 / 4002 only. 7496 is off."""
    try:
        if cfg is None:
            from abcxauto.config import get_config

            cfg = get_config()
        port = int(getattr(cfg, "ibkr_port", 7497) or 7497)
    except (TypeError, ValueError):
        return False
    return port not in LIVE_PORTS


def rth_model_no_xhigh(model: str, *, enabled: bool | None = None) -> str:
    """RTH thin: strip xhigh. Empty stays empty so session_model fallback works."""
    token = str(model or "").strip()
    if not token:
        return token
    if enabled is False:
        return token
    if enabled is None and not kill_look_enabled():
        return token
    stripped = _XHIGH_RE.sub("", token).replace("--", "-").strip("-")
    from abcxauto.config import DEFAULT_MODEL

    return stripped or DEFAULT_MODEL


def _is_xhigh_param(key: str, value: Any) -> bool:
    """True when a param looks like a model-id xhigh leak (drop it in RTH).

    ``effort`` / ``reasoning_effort`` are operator knobs — keep them even when
    the value is xhigh so session_model_params("regular") can send max effort.
    """
    name = str(key or "").strip().lower()
    if name in {"effort", "reasoning_effort"}:
        return False
    return isinstance(value, str) and "xhigh" in value.lower()


def rth_params_no_xhigh(params: Any, *, enabled: bool | None = None) -> dict[str, Any]:
    """RTH thin: keep effort/reasoning_effort; drop other string xhigh leaks.

    Model-id suffix stripping stays in ``rth_model_no_xhigh``. F10 is unchanged.
    Invalid / empty maps fail-closed to ``{}``.
    """
    try:
        from abcxauto.config import coerce_model_params

        cleaned = coerce_model_params(params) if params else {}
    except (TypeError, ValueError):
        cleaned = {}
    if enabled is False:
        return cleaned
    if enabled is None and not kill_look_enabled():
        return cleaned
    return {k: v for k, v in cleaned.items() if not _is_xhigh_param(k, v)}


def kill_look_rth(session: str = "", *, enabled: bool | None = None) -> bool:
    if enabled is False:
        return False
    if enabled is None and not kill_look_enabled():
        return False
    try:
        from abcxauto.desk_mode import is_rth_session

        return bool(is_rth_session(session))
    except Exception:
        return str(session or "").strip().lower() == "regular"


def _qty_open(row: dict[str, Any]) -> bool:
    try:
        qty = float(row.get("quantity", row.get("position", row.get("qty", 0))) or 0)
    except (TypeError, ValueError):
        return False
    return abs(qty) >= 1e-9


def has_open_pcs_skew_lot(
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
) -> bool:
    """True when the book has a named pcs-skew (or put vertical BAG) lot."""
    for lab in open_lots or []:
        blob = str(lab or "").lower()
        if "pcs-skew" in blob or "pcs_skew" in blob:
            return True
    book = [p for p in (positions or []) if isinstance(p, dict) and _qty_open(p)]
    for row in book:
        card = str(row.get("card") or "").strip().lower()
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        if not card:
            card = str(params.get("card") or "").strip().lower()
        if card == PCS_CARD:
            return True
        strat = str(row.get("strategy") or params.get("strategy") or "").strip().lower()
        right = str(row.get("right") or params.get("right") or "")[:1].upper()
        if strat == PCS_STRATEGY and right == "P":
            return True
        sec = str(row.get("secType") or row.get("sec_type") or row.get("sec") or "").upper()
        if sec == "BAG" and right == "P":
            return True
    return False


def has_open_stk_lot(
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
) -> bool:
    """True when the book has an open STK lot (look-skip manage bypass)."""
    for lab in open_lots or []:
        # lot_ident style: "AVGO STK LONG 89"
        if re.search(r"\bSTK\b", str(lab or ""), flags=re.IGNORECASE):
            return True
    book = [p for p in (positions or []) if isinstance(p, dict) and _qty_open(p)]
    for row in book:
        # Missing secType reads as STK, same as the book.
        sec = str(
            row.get("secType") or row.get("sec_type") or row.get("sec") or "STK"
        ).upper()
        if sec == "STK":
            return True
    return False


def has_open_manage_lot(
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
) -> bool:
    """True when an open pcs-skew or STK lot keeps looks in MANAGE."""
    return has_open_pcs_skew_lot(positions, open_lots) or has_open_stk_lot(
        positions, open_lots
    )


def look_kill_mill(payload: dict[str, Any] | None, *, session: str = "") -> bool:
    """#165 synthesize mill only. Hunt-without-send is not a mill.

    ``session`` is unused; pro_engine still passes it and still RTH-gates
    the call. Same-chat TOOL-OR-SEND re-arm stays on zero-tool mill language.
    """
    _ = session
    try:
        from abcxauto.desk_mode import look_synthesize_mill

        return bool(look_synthesize_mill(payload))
    except Exception:
        logger.debug("look_synthesize_mill failed", exc_info=True)
        return False


def send_strategy_names(*, session: str = "") -> list[str] | None:
    """None = default enum. Kill RTH send offers clerk-legal defined-risk
    structures that already have an ORDER EXAMPLES schema and a place path.
    """
    if not kill_look_rth(session):
        return None
    return list(_kill_look_sendable_strategies())


def _params_of(act: dict[str, Any] | None) -> dict[str, Any]:
    row = act if isinstance(act, dict) else {}
    params = row.get("params")
    return dict(params) if isinstance(params, dict) else {}


def _is_closing(params: dict[str, Any]) -> bool:
    return params.get("closing_position") is True


def _abort_send_reason(abort_fuse: str = "") -> str:
    """Named scorecard fuses only. Unknown/"none" must not steal F10."""
    token = str(abort_fuse or "").strip()
    if token == "F10":
        return REASON_F10
    if token == "DD30":
        return REASON_DD
    if token == "QTY0_STREAK":
        return REASON_QTY0
    # Residual non-fuse abort (port / latch). Real F10 hard-trip alone owns f10.
    return REASON_PORT


def normalize_kill_look_card(card: Any = None) -> str:
    """Exact scorecard card label. Lower/strip. Empty / whitespace is nameless."""
    return str(card or "").strip().lower()


# Geometry the clerk must see on the ticket — never invent strikes / exp / qty.
_GEOM_FINITE = frozenset({
    "quantity",
    "contracts",
    "shares",
    "ratio",
    "long_strike",
    "short_strike",
    "strike",
    "put_long_strike",
    "put_short_strike",
    "call_short_strike",
    "call_long_strike",
    "center_strike",
    "wing_width",
    "put_strike",
    "call_strike",
    "lower_strike",
    "middle_strike",
    "upper_strike",
    "near_strike",
    "far_strike",
    "stop_price",
    "target_price",
    "entry_price",
})
_GEOM_TEXT = frozenset({
    "symbol",
    "expiration",
    "near_expiration",
    "far_expiration",
    "right",
})
_KILL_LOOK_FORBIDDEN = frozenset({"ratio_spread", "jade_lizard"})
_KILL_LOOK_SHORT_OK_IF_LONG = frozenset({"straddle", "strangle"})
_KILL_LOOK_STOCK_ENTRIES = frozenset({"bracket", "market_bracket", "oca"})
# Operator-facing send order. A name lands here only if ORDER EXAMPLES and
# STRATEGIES already teach a place path. close_option is an exit, not an open.
_KILL_LOOK_PREFERRED = (
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "butterfly",
    "calendar_spread",
    "diagonal_spread",
    "cash_secured_put",
    "covered_call",
    "protective_put",
    "collar",
    "roll_option",
    "buy_option",
    "straddle",
    "strangle",
    "bracket",
    "market_bracket",
    "oca",
)
# Not new risk. Must stay on the send enum or a cancel is emitted as a bracket.
_KILL_LOOK_MANAGE = (
    "cancel_order",
    "modify_stop",
    "modify_target",
)


def _kill_look_sendable_strategies() -> list[str]:
    """Defined-risk names with a schema example and a working place path."""
    from abcxauto.order_examples import NOT_TICKETS, ORDER_EXAMPLES
    from abcxauto.proposals import STRATEGIES
    from abcxauto.strategy_params import OPTION_STRATEGIES

    out: list[str] = []
    for name in _KILL_LOOK_PREFERRED:
        if name in NOT_TICKETS or name in _KILL_LOOK_FORBIDDEN:
            continue
        if name not in ORDER_EXAMPLES:
            continue
        entry = STRATEGIES.get(name)
        if not entry or not entry[1]:
            continue
        if name in OPTION_STRATEGIES or name in _KILL_LOOK_STOCK_ENTRIES:
            out.append(name)
    for name in _KILL_LOOK_MANAGE:
        if name in NOT_TICKETS:
            continue
        if name not in ORDER_EXAMPLES:
            continue
        entry = STRATEGIES.get(name)
        if not entry or not entry[1]:
            continue
        out.append(name)
    return out


def _finite_geom(raw: Any) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return val


def _clerk_legal_defined_risk(strategy: str, params: dict[str, Any]) -> bool:
    """ORDER EXAMPLES / proposals defined-risk new-risk types. No invented names."""
    from abcxauto.order_examples import NOT_TICKETS, ORDER_EXAMPLES
    from abcxauto.proposals import MANAGEMENT_STRATEGIES, STRATEGIES
    from abcxauto.strategy_params import OPTION_STRATEGIES

    if strategy not in STRATEGIES or strategy not in ORDER_EXAMPLES:
        return False
    if strategy in NOT_TICKETS:
        return False
    if strategy in MANAGEMENT_STRATEGIES or strategy == "close_option":
        return False
    if strategy not in OPTION_STRATEGIES:
        if strategy not in _KILL_LOOK_STOCK_ENTRIES:
            return False
        stop = _finite_geom(params.get("stop_price"))
        return stop is not None and stop > 0
    if strategy in _KILL_LOOK_FORBIDDEN:
        return False
    if strategy in _KILL_LOOK_SHORT_OK_IF_LONG:
        action = str(params.get("action") or "BUY").strip().upper()
        if action == "SELL":
            return False
    return True


def _ticket_schema_ok(strategy: str, params: dict[str, Any]) -> bool:
    """Same pydantic model as ``validate_proposal`` / the place path."""
    from pydantic import ValidationError

    from abcxauto.proposals import STRATEGIES

    entry = STRATEGIES.get(strategy)
    if entry is None:
        return False
    model_cls = entry[0]
    try:
        model_cls(**params)
    except (ValidationError, ValueError, TypeError):
        return False
    return True


def _geometry_complete(strategy: str, params: dict[str, Any]) -> bool:
    """Required geometry present and finite. Do not invent fills or strikes."""
    from abcxauto.proposals import STRATEGIES

    entry = STRATEGIES.get(strategy)
    if entry is None:
        return False
    fields = entry[0].model_fields
    for key in _GEOM_FINITE | _GEOM_TEXT:
        if key not in fields:
            continue
        if key not in params or params.get(key) in (None, ""):
            return False
        raw = params.get(key)
        if key in _GEOM_FINITE:
            val = _finite_geom(raw)
            if val is None:
                return False
        else:
            token = str(raw or "").strip()
            if not token:
                return False
            if "expiration" in key and not re.fullmatch(r"\d{8}", token):
                return False
            if key == "right" and token[:1].upper() not in {"C", "P"}:
                return False
    return True


def pcs_send_ok(
    strategy: str = "",
    params: dict[str, Any] | None = None,
    card: Any = None,
    *,
    mode: str = "",
    abort_fuse: str = "",
) -> tuple[bool, str]:
    """Legal kill-look new risk: named card + ORDER EXAMPLES schema + geometry.

    Closers / non-new-risk skip structure checks. MANAGE (open STK/pcs) does
    not refuse a second name. Nameless freestyle is refused. Credit quality
    is Grok judgement — no clerk dollar credit floor.
    """
    from abcxauto.agent_loop import is_new_risk

    strat = str(strategy or "").strip().lower()
    dumped = dict(params or {})
    if card not in (None, ""):
        dumped.setdefault("card", card)
    if _is_closing(dumped) or not is_new_risk(strat, dumped):
        return True, "closing"
    if mode == MODE_ABORT:
        return False, _abort_send_reason(abort_fuse)
    card_norm = normalize_kill_look_card(dumped.get("card"))
    if not card_norm:
        return False, REASON_NAMELESS
    if not _clerk_legal_defined_risk(strat, dumped):
        return False, REASON_STRUCTURE
    if not _ticket_schema_ok(strat, dumped):
        return False, REASON_STRUCTURE
    if not _geometry_complete(strat, dumped):
        return False, REASON_STRUCTURE
    return True, card_norm


def session_model_cost_usd(*, since_iso: str = "") -> float | None:
    """Billed model $ this scored session. None = unreadable (fail-closed).

    Prefers ``look_ledger.session_spend`` for today's ET date when that
    function exists. Ledger ``unknown`` fail-closes. Missing module keeps
    the scorecard sum.
    """
    try:
        from abcxauto.look_ledger import session_spend
    except ImportError:
        session_spend = None  # type: ignore[assignment]
    if session_spend is not None:
        try:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo

            et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
            spend = session_spend(et.date().isoformat())
            if not isinstance(spend, dict):
                return None
            if spend.get("unknown") is True:
                return None
            cost = spend.get("usd")
            if cost is None:
                return None
            val = float(cost)
            if val != val or val < 0:
                return None
            return val
        except Exception:
            logger.debug("look ledger session spend unreadable", exc_info=True)
            return None
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        if since_iso:
            used = journal.model_usage_since(since_iso)
        else:
            used = journal.model_usage_totals()
        if not isinstance(used, dict) or "cost_usd" not in used:
            return None
        cost = used.get("cost_usd")
        if cost is None:
            return None
        val = float(cost)
        if val != val or val < 0:
            return None
        return val
    except Exception:
        logger.debug("session model cost unreadable", exc_info=True)
        return None


def window_model_cost_usd(*, limit: int = WINDOW_N) -> float | None:
    """N=20 kill-window sum_model_cost. None = unreadable."""
    try:
        from abcxauto.memory import get_journal

        win = get_journal().pcs_kill_window(limit=int(limit))
        if not isinstance(win, dict) or "sum_model_cost" not in win:
            return None
        raw = win.get("sum_model_cost")
        if raw is None:
            return None
        val = float(raw)
        if val != val or val < 0:
            return None
        return val
    except Exception:
        logger.debug("window model cost unreadable", exc_info=True)
        return None


def scored_session_since_iso(*, now=None) -> str:
    """UTC ISO for ET midnight of this scored session date."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    et = clock.astimezone(ZoneInfo("America/New_York"))
    start = et.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def f10_gate(
    session_cost: float | None,
    *,
    est_this_look: float = EST_THIS_LOOK_USD,
    window_cost: float | None = 0.0,
) -> dict[str, Any]:
    """$ gate. Unreadable session cost fail-closes new risk. Preferred is not hard."""
    if session_cost is None:
        return {
            "allow_new_risk": False,
            "preferred_trip": False,
            "reason_code": REASON_MODEL_COST,
            "note": "model_cost unreadable — fail-closed",
            "projected": None,
        }
    try:
        so_far = float(session_cost)
        est = float(est_this_look)
    except (TypeError, ValueError):
        return {
            "allow_new_risk": False,
            "preferred_trip": False,
            "reason_code": REASON_MODEL_COST,
            "note": "model_cost unreadable — fail-closed",
            "projected": None,
        }
    if not math.isfinite(so_far) or not math.isfinite(est) or so_far < 0 or est < 0:
        return {
            "allow_new_risk": False,
            "preferred_trip": False,
            "reason_code": REASON_MODEL_COST,
            "note": "model_cost unreadable — fail-closed",
            "projected": None,
        }
    if window_cost is None:
        return {
            "allow_new_risk": False,
            "preferred_trip": False,
            "reason_code": REASON_MODEL_COST,
            "note": "window model_cost unreadable — fail-closed",
            "projected": None,
        }
    try:
        win = float(window_cost)
    except (TypeError, ValueError):
        return {
            "allow_new_risk": False,
            "preferred_trip": False,
            "reason_code": REASON_MODEL_COST,
            "note": "window model_cost unreadable — fail-closed",
            "projected": None,
        }
    if not math.isfinite(win) or win < 0:
        return {
            "allow_new_risk": False,
            "preferred_trip": False,
            "reason_code": REASON_MODEL_COST,
            "note": "window model_cost unreadable — fail-closed",
            "projected": None,
        }
    if win >= WINDOW_MODEL_USD:
        return {
            "allow_new_risk": False,
            "preferred_trip": True,
            "reason_code": REASON_MODEL_COST,
            "note": f"window model_cost {win} >= {WINDOW_MODEL_USD} — freeze new risk",
            "projected": so_far + est,
        }
    projected = so_far + est
    preferred = projected > F10_PREFERRED_USD
    if projected > F10_HARD_USD:
        return {
            "allow_new_risk": False,
            "preferred_trip": True,
            "reason_code": REASON_F10,
            "note": (
                f"session_model_so_far {so_far} + est_this_look {est} "
                f"> {F10_HARD_USD}"
            ),
            "projected": projected,
        }
    return {
        "allow_new_risk": True,
        "preferred_trip": preferred,
        "reason_code": "",
        "note": "preferred tripwire" if preferred else "",
        "projected": projected,
    }


def live_f10_gate() -> dict[str, Any]:
    return f10_gate(
        session_model_cost_usd(since_iso=scored_session_since_iso()),
        est_this_look=EST_THIS_LOOK_USD,
        window_cost=window_model_cost_usd(),
    )


def f10_hard_tripped(gate: dict[str, Any] | None = None) -> bool:
    """True for the hard $15 F10 fuse (not preferred $10, not unreadable)."""
    if isinstance(gate, dict) and str(gate.get("reason_code") or "") == REASON_F10:
        return True
    try:
        from abcxauto.session_caps import f10_loop_halted

        if f10_loop_halted():
            return True
    except Exception:
        logger.debug("f10 latch read failed", exc_info=True)
    return False


def f10_open_look_halted(f10: dict[str, Any] | None = None) -> bool:
    """True when the dollar / unreadable F10 fuse is tripped.

    New-risk sends stay refused. Looks still call the model; manage /
    exits are not this predicate.
    """
    try:
        from abcxauto.session_caps import f10_loop_halted

        if f10_loop_halted():
            return True
    except Exception:
        logger.debug("f10 latch read failed", exc_info=True)
    gate = f10 if isinstance(f10, dict) else live_f10_gate()
    why = str(gate.get("reason_code") or "")
    return why in (REASON_F10, REASON_MODEL_COST)


def is_f10_look_halt(reason: str = "") -> bool:
    """True when skip/send reason is the dollar / unreadable F10 fuse.

    Used for glass lines and send blocks. Does not mean skip the look.
    """
    return str(reason or "") in (REASON_F10, REASON_MODEL_COST)


def skip_glass_line(reason: str = "") -> str:
    """One stream line when a look is skipped before the model is called."""
    why = str(reason or "").strip()
    if not why:
        return ""
    if is_f10_look_halt(why):
        return "[F10 loop halt — no new-risk looks]"
    try:
        from abcxauto.research_budget import is_brief_look_halt

        if is_brief_look_halt(why):
            return "[brief loop halt — no billed research turns]"
    except Exception:
        logger.debug("brief halt glass line failed", exc_info=True)
    return f"[{why}]"


def mark_f10_hard_trip(gate: dict[str, Any] | None = None) -> bool:
    """Latch hard F10. Preferred / unreadable / window cap do not latch."""
    if isinstance(gate, dict):
        if str(gate.get("reason_code") or "") != REASON_F10:
            return False
    elif not f10_hard_tripped():
        return False
    try:
        from abcxauto.session_caps import mark_f10_loop_halt

        mark_f10_loop_halt()
        return True
    except Exception:
        logger.debug("f10 latch write failed", exc_info=True)
        return False


def record_f10_loop_halt(
    *,
    session: str = "",
    skip_reason: str = REASON_F10,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Latch + last_turn flags. Never raises. Exits are not this path."""
    marked = False
    try:
        from abcxauto.session_caps import mark_f10_loop_halt

        mark_f10_loop_halt()
        marked = True
    except Exception:
        logger.debug("f10 latch write failed", exc_info=True)
    blob = snap if isinstance(snap, dict) else {}
    payload = {
        "strat": "skipped",
        "skip_reason": str(skip_reason or REASON_F10),
        "validation": str(skip_reason or REASON_F10),
        "rationale": str(skip_reason or REASON_F10),
        "f10_tripped": True,
        "loop_halted": True,
        "model_cost_post_trip_USD": 0.0,
        "sends": 0,
        "positions": list(blob.get("positions") or []),
        "open_lots": list(blob.get("open_lots") or []),
        "reality_pulse": blob.get("reality_pulse") or {"session": {"status": session}},
    }
    try:
        from abcxauto.think_stream import write_last_turn

        write_last_turn(payload)
    except Exception:
        logger.debug("f10 last_turn persist failed", exc_info=True)
    return {
        "f10_tripped": True,
        "loop_halted": True,
        "model_cost_post_trip_USD": 0.0,
        "skip_reason": str(skip_reason or REASON_F10),
        "latched": marked,
    }


def kill_mode(
    session: str = "",
    *,
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
    f10: dict[str, Any] | None = None,
    now=None,
    in_flight: bool = False,
    abort_fuse: str | None = None,
) -> str:
    """OPEN / MANAGE / ABORT / research / empty (contract off).

    No one-look RTH entry budget. Named scorecard abort fuses stop new-risk
    looks even in-flight. Open pcs-skew or STK lots stay MANAGE so exits are
    not blocked — including premarket/postmarket (research label must not
    hide an open stock). ``in_flight`` retained for callers; unused for
    entry-budget gates.
    """
    if not kill_look_enabled():
        return ""
    # Manage first: open STK / pcs-skew still manage under a research session.
    if has_open_manage_lot(positions, open_lots):
        return MODE_MANAGE
    try:
        from abcxauto.desk_mode import is_research_session, is_rth_session

        if is_research_session(session) and not is_rth_session(session):
            return MODE_RESEARCH
        rth = is_rth_session(session)
    except Exception:
        rth = str(session or "").strip().lower() == "regular"
        if not rth:
            return MODE_RESEARCH
    if not rth:
        return MODE_RESEARCH
    if not kill_look_port_ok():
        return MODE_ABORT
    try:
        from abcxauto.session_caps import f10_loop_halted

        if f10_loop_halted():
            return MODE_ABORT
    except Exception:
        logger.debug("f10 latch read failed", exc_info=True)
    if abort_fuse is None:
        try:
            from abcxauto.abort_fuse import scorecard_abort_fuse

            abort_fuse = scorecard_abort_fuse()
        except Exception:
            abort_fuse = "none"
    if str(abort_fuse or "") in {"F10", "DD30", "QTY0_STREAK"}:
        return MODE_ABORT
    gate = f10 if isinstance(f10, dict) else live_f10_gate()
    if not gate.get("allow_new_risk", False):
        why = str(gate.get("reason_code") or "")
        if why == REASON_F10:
            mark_f10_hard_trip(gate)
        elif why == REASON_MODEL_COST:
            try:
                from abcxauto.session_caps import mark_f10_loop_halt

                mark_f10_loop_halt()
            except Exception:
                logger.debug("f10 model_cost latch write failed", exc_info=True)
        return MODE_ABORT
    return MODE_OPEN


def research_prompt_ok(prompt_tokens: int) -> bool:
    try:
        n = int(prompt_tokens or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= n < RESEARCH_PROMPT_TOKENS_MAX


def _flag_true(raw: Any) -> bool:
    return raw is True or raw in (1, "1", "true", "True", "yes", "on")


def _flag_false(raw: Any) -> bool:
    return raw is False or raw in (0, "0", "false", "False", "no", "off")


def dead_socket_skip_reason(
    snap: dict[str, Any] | None = None,
    *,
    unprotected: bool = False,
    ibkr_connected: bool | None = None,
) -> str:
    """Skip billed Grok on a dead/unreliable book. Unprotected still looks."""
    if unprotected:
        return ""
    blob = snap if isinstance(snap, dict) else {}
    pulse = blob.get("reality_pulse") if isinstance(blob.get("reality_pulse"), dict) else {}
    gates = blob.get("gates") if isinstance(blob.get("gates"), dict) else {}
    if (
        _flag_true(blob.get("book_unreliable"))
        or _flag_true(pulse.get("book_unreliable"))
        or _flag_true(gates.get("book_unreliable"))
    ):
        return REASON_BOOK_UNRELIABLE
    connected = ibkr_connected
    if connected is None:
        if "ibkr_connected" in blob:
            connected = blob.get("ibkr_connected")
        elif "ibkr_connected" in pulse:
            connected = pulse.get("ibkr_connected")
    if _flag_false(connected):
        return REASON_IBKR_DOWN
    return ""


def skip_look_reason(
    session: str = "",
    *,
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
    same_look: bool = False,
    unprotected: bool = False,
    prompt_tokens: int = 0,
    now=None,
    f10: dict[str, Any] | None = None,
    in_flight: bool = False,
    abort_fuse: str | None = None,
    snap: dict[str, Any] | None = None,
) -> str:
    """Non-empty = do not call the model. Unprotected last-stop still looks.

    No entry-budget skip. Soften=FAIL: F10 hard, nameless send, and 7496 stay.
    """
    if unprotected:
        return ""
    dead = dead_socket_skip_reason(snap, unprotected=unprotected)
    if dead:
        return dead
    # Named-card brief halt is independent of kill-look (no mill escape).
    try:
        from abcxauto.research_budget import research_brief_skip_reason

        brief_halt = research_brief_skip_reason(
            session, snap=snap, now=now, unprotected=unprotected
        )
        if brief_halt:
            return brief_halt
    except Exception:
        logger.debug("research brief skip failed", exc_info=True)
    if not kill_look_enabled():
        return ""
    fuse = abort_fuse
    if fuse is None:
        try:
            from abcxauto.abort_fuse import scorecard_abort_fuse

            fuse = scorecard_abort_fuse()
        except Exception:
            fuse = "none"
    mode = kill_mode(
        session,
        positions=positions,
        open_lots=open_lots,
        now=now,
        f10=f10,
        in_flight=bool(in_flight or same_look),
        abort_fuse=fuse,
    )
    if mode == MODE_MANAGE:
        return ""
    # Dollar / unreadable F10: latch accounting, refuse new risk at send.
    # Do not skip the look — the model still runs (research + flat RTH).
    if f10_open_look_halted(f10):
        gate = f10 if isinstance(f10, dict) else live_f10_gate()
        why = str(gate.get("reason_code") or "")
        if why == REASON_F10:
            mark_f10_hard_trip(gate)
        elif why == REASON_MODEL_COST:
            try:
                from abcxauto.session_caps import mark_f10_loop_halt

                mark_f10_loop_halt()
            except Exception:
                logger.debug("f10 model_cost latch write failed", exc_info=True)
    if mode == MODE_OPEN:
        return ""
    if mode == MODE_ABORT:
        # Named scorecard fuses alone own fuse codes. Port ≠ F10.
        # Session dollar latch (f10_loop_halted) is ABORT for new risk only.
        if fuse == "DD30":
            return REASON_DD
        if fuse == "QTY0_STREAK":
            return REASON_QTY0
        if fuse == "F10":
            return REASON_F10
        if not kill_look_port_ok():
            return REASON_PORT
        return ""
    if mode == MODE_RESEARCH:
        if AH_RESEARCH_LOOKS_PER_WEEK > 0:
            from abcxauto.session_caps import research_week_looks

            if int(research_week_looks(now=now) or 0) >= AH_RESEARCH_LOOKS_PER_WEEK:
                return REASON_RESEARCH_WEEK
        if not research_prompt_ok(prompt_tokens):
            return REASON_RESEARCH_PROMPT
        try:
            from abcxauto.research_budget import research_brief_skip_reason

            brief_halt = research_brief_skip_reason(
                session, snap=snap, now=now, unprotected=unprotected
            )
            if brief_halt:
                return brief_halt
        except Exception:
            logger.debug("research brief skip failed", exc_info=True)
        return ""
    return ""


def kill_look_send_block(
    act: dict[str, Any] | None,
    *,
    session: str = "",
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
    f10: dict[str, Any] | None = None,
    in_flight: bool = False,
    abort_fuse: str | None = None,
) -> dict[str, Any] | None:
    """Clerk block for illegal new named-card risk. Exits still go."""
    if not kill_look_rth(session):
        return None
    row = act if isinstance(act, dict) else {}
    params = _params_of(row)
    strat = str(row.get("strategy") or row.get("action") or "").strip().lower()
    card = row.get("card") or params.get("card")
    if _is_closing(params):
        return None
    from abcxauto.agent_loop import is_new_risk

    if not is_new_risk(strat, params):
        return None
    if not kill_look_port_ok():
        return {
            "status": "blocked",
            "note": "kill look paper 7497 only — 7496 off",
            "reason_code": REASON_PORT,
            "strategy": "blocked",
        }
    gate = f10 if isinstance(f10, dict) else live_f10_gate()
    if not gate.get("allow_new_risk", False):
        why = str(gate.get("reason_code") or "")
        if why == REASON_F10:
            mark_f10_hard_trip(gate)
        elif why == REASON_MODEL_COST:
            try:
                from abcxauto.session_caps import mark_f10_loop_halt

                mark_f10_loop_halt()
            except Exception:
                logger.debug("f10 model_cost latch write failed", exc_info=True)
        return {
            "status": "blocked",
            "note": str(gate.get("note") or REASON_F10),
            "reason_code": str(gate.get("reason_code") or REASON_F10),
            "strategy": "blocked",
            "preferred_trip": bool(gate.get("preferred_trip")),
        }
    if gate.get("preferred_trip"):
        logger.warning("F10 preferred $10 tripwire (hard still $15)")
        try:
            from abcxauto.think_stream import emit as think_emit

            think_emit("tool", "\n[F10 preferred $10 tripwire — hard still $15]\n")
        except Exception:
            logger.debug("F10 preferred think emit failed", exc_info=True)
    fuse = abort_fuse
    if fuse is None:
        try:
            from abcxauto.abort_fuse import scorecard_abort_fuse

            fuse = scorecard_abort_fuse()
        except Exception:
            fuse = "none"
    mode = kill_mode(
        session,
        positions=positions,
        open_lots=open_lots,
        f10=f10,
        in_flight=in_flight,
        abort_fuse=fuse,
    )
    ok, why = pcs_send_ok(strat, params, card, mode=mode, abort_fuse=str(fuse or ""))
    if not ok:
        return {
            "status": "blocked",
            "note": why,
            "reason_code": why,
            "strategy": "blocked",
        }
    return None

