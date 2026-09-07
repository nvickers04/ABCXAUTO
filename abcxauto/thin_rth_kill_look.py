"""pcs-skew Arm v0 kill-window LOOK contract (thin prompt, not SYSTEM_PROMPT).

Hard clerk: ≤1 RTH entry look/scored session, STAY-tool allowlist, mill widen,
RTH no-xhigh / AH-rare, F10 $2 hard / $1 preferred. Paper 7497. Not looking.
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

STAY_TOOLS = frozenset({
    "book",
    "status",
    "quote",
    "option_chain",
    "option_quote",
    "fills",
    "send",
})
DIE_TOOLS = frozenset({
    "scan",
    "news",
    "candles",
    "odds",
    "self_tune",
    "web",
    "option_facts",
    "write_research_brief",
})

# F10 dollars. Not raiseable Settings knobs. Preferred is an ops tripwire.
F10_HARD_USD = 2.0
F10_PREFERRED_USD = 1.0
WINDOW_MODEL_USD = 40.0
WINDOW_N = 20
RTH_ENTRY_LOOKS_MAX = 1
AH_RESEARCH_LOOKS_PER_WEEK = 2
RESEARCH_PROMPT_TOKENS_MAX = 200_000
MODEL_TURNS_MAX = 4
TOOLS_ENTRY_MAX = 6
TOOLS_MANAGE_MAX = 4
# Conservative thin STAY look. Used as est_this_look in the F10 sum.
EST_THIS_LOOK_USD = 0.35

REASON_F10 = "NO_SEND:f10"
REASON_DD = "NO_SEND:dd_fuse"
REASON_QTY0 = "NO_SEND:qty0_streak"
REASON_MODEL_COST = "NO_SEND:model_cost_cap"
REASON_ALLOWLIST = "kill_look_allowlist"
REASON_ENTRY_BUDGET = "kill_look_entry_budget"
REASON_RESEARCH_WEEK = "kill_look_research_week"
REASON_RESEARCH_PROMPT = "kill_look_research_prompt"
REASON_DIE_TOOL = "kill_look_die_tool"
REASON_TURNS = "kill_look_turns"
REASON_TOOLS = "kill_look_tools"
REASON_PORT = "kill_look_live_port"
REASON_ONE_SEND = "kill_look_one_send"

LIVE_PORTS = frozenset({7496, 4001})

_XHIGH_RE = re.compile(r"-xhigh\b", re.IGNORECASE)
_GATHER_SPIN_RE = re.compile(
    r"\bgather(?:ing)?\b"
    r"|\b(?:keep\s+)?(?:scanning|browsing)\b"
    r"|\bspin(?:ning)?\b"
    r"|\bone\s+more\s+(?:scan|pass)\b"
    r"|\bdecide(?:d)?\s+without\s+(?:a\s+)?send\b",
    re.IGNORECASE,
)


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
    name = str(key or "").strip().lower()
    blob = str(value or "").strip().lower()
    if name in {"effort", "reasoning_effort"} and "xhigh" in blob:
        return True
    return isinstance(value, str) and "xhigh" in value.lower()


def rth_params_no_xhigh(params: Any, *, enabled: bool | None = None) -> dict[str, Any]:
    """RTH thin: drop xhigh effort so params cannot undo ``rth_model_no_xhigh`` / F10.

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


def spoken_gather_spin(text: str = "") -> bool:
    blob = str(text or "")
    if not blob.strip():
        return False
    return bool(_GATHER_SPIN_RE.search(blob))


def _die_tools_in_trace(tool_trace: list[Any] | None) -> bool:
    for raw in tool_trace or []:
        name = str(raw or "").strip().split()[0].lower()
        if name in DIE_TOOLS:
            return True
    return False


def look_gather_spin_mill(payload: dict[str, Any] | None) -> bool:
    """DIE-tool or spoken gather-spin with zero send. Keep #165 zero-tool mill separate."""
    row = payload if isinstance(payload, dict) else {}
    try:
        sends = int(row.get("sends") or 0)
    except (TypeError, ValueError):
        sends = 0
    if sends > 0:
        return False
    trace = list(row.get("tool_trace") or [])
    for raw in trace:
        name = str(raw or "").strip().split()[0].lower()
        if name == "send":
            return False
    if _die_tools_in_trace(trace):
        return True
    text = str(row.get("rationale") or row.get("text") or "")
    if spoken_gather_spin(text) and _die_tools_in_trace(trace):
        return True
    if spoken_gather_spin(text) and not any(str(x or "").strip() for x in trace):
        return True
    return False


def look_kill_mill(payload: dict[str, Any] | None, *, session: str = "") -> bool:
    """#165 synthesize mill, plus gather-spin / DIE mill on RTH kill looks."""
    try:
        from abcxauto.desk_mode import look_synthesize_mill

        if look_synthesize_mill(payload):
            return True
    except Exception:
        logger.debug("look_synthesize_mill failed", exc_info=True)
    if not kill_look_rth(session):
        return False
    return look_gather_spin_mill(payload)


def tool_allowed(name: str, *, session: str = "", mode: str = "") -> bool:
    """STAY only on RTH kill looks. Research keeps its own send omit."""
    key = str(name or "").strip().lower()
    if not key:
        return False
    if mode == MODE_RESEARCH or not kill_look_rth(session):
        return True
    return key in STAY_TOOLS


def die_tool_block(name: str, *, session: str = "") -> dict[str, Any] | None:
    key = str(name or "").strip().lower()
    if not kill_look_rth(session):
        return None
    if key in STAY_TOOLS:
        return None
    if key in DIE_TOOLS or key not in STAY_TOOLS:
        return {
            "error": REASON_DIE_TOOL,
            "tool": key,
            "note": "RTH kill look STAY tools only",
            "reason_code": REASON_DIE_TOOL,
        }
    return None


def filter_agent_tool_names(
    names: list[str],
    *,
    session: str = "",
) -> list[str]:
    if not kill_look_rth(session):
        return list(names)
    return [n for n in names if str(n or "").strip().lower() in STAY_TOOLS]


def send_strategy_names(*, session: str = "") -> list[str] | None:
    """None = default enum. Kill RTH send is vertical_spread only (close via param)."""
    if not kill_look_rth(session):
        return None
    return [PCS_STRATEGY]


def _params_of(act: dict[str, Any] | None) -> dict[str, Any]:
    row = act if isinstance(act, dict) else {}
    params = row.get("params")
    return dict(params) if isinstance(params, dict) else {}


def _is_closing(params: dict[str, Any]) -> bool:
    return params.get("closing_position") is True


def _abort_send_reason(abort_fuse: str = "") -> str:
    token = str(abort_fuse or "").strip()
    if token == "DD30":
        return REASON_DD
    if token == "QTY0_STREAK":
        return REASON_QTY0
    return REASON_F10


def pcs_send_ok(
    strategy: str = "",
    params: dict[str, Any] | None = None,
    card: Any = None,
    *,
    mode: str = "",
    abort_fuse: str = "",
) -> tuple[bool, str]:
    """Legal kill-look send: pcs-skew put vertical BAG, or closing_position."""
    from abcxauto.agent_loop import is_new_risk
    from abcxauto.pcs_fill_lambda import is_pcs_ticket

    strat = str(strategy or "").strip().lower()
    dumped = dict(params or {})
    if card not in (None, ""):
        dumped.setdefault("card", card)
    if _is_closing(dumped) or not is_new_risk(strat, dumped):
        return True, "closing"
    if mode == MODE_MANAGE:
        return False, REASON_ALLOWLIST
    if mode == MODE_ABORT:
        return False, _abort_send_reason(abort_fuse)
    if strat != PCS_STRATEGY:
        return False, REASON_ALLOWLIST
    if not is_pcs_ticket(strat, dumped, dumped.get("card")):
        return False, REASON_ALLOWLIST
    return True, "pcs-skew"


def session_model_cost_usd(*, since_iso: str = "") -> float | None:
    """Billed model $ this scored session. None = unreadable (fail-closed)."""
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
    """True for the hard $2 F10 fuse (not preferred $1, not unreadable)."""
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
    """True when subsequent OPEN / new-risk looks must not call the model."""
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
    """True when skip/send reason must stop billed new-risk looks."""
    return str(reason or "") in (REASON_F10, REASON_MODEL_COST)


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
    entry_looks: int | None = None,
    f10: dict[str, Any] | None = None,
    now=None,
    in_flight: bool = False,
    abort_fuse: str | None = None,
) -> str:
    """OPEN / MANAGE / ABORT / research / empty (contract off).

    ``in_flight`` is look #1 after consume (mill/unpaid/recover included).
    Entry budget still refuses a *new* look; it must not refuse the SEND on
    the look that already spent the budget.
    Named scorecard abort fuses stop new-risk looks even in-flight. Open
    lots stay MANAGE so exits are not blocked.
    """
    if not kill_look_enabled():
        return ""
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
    if has_open_pcs_skew_lot(positions, open_lots):
        return MODE_MANAGE
    if not kill_look_port_ok():
        return MODE_ABORT
    try:
        from abcxauto.session_caps import f10_loop_halted

        if f10_loop_halted():
            return MODE_ABORT
    except Exception:
        logger.debug("f10 latch read failed", exc_info=True)
    if entry_looks is None:
        from abcxauto.session_caps import kill_entry_looks

        entry_looks = kill_entry_looks(session, now=now)
    if int(entry_looks or 0) >= RTH_ENTRY_LOOKS_MAX and not in_flight:
        return MODE_ABORT
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
        if str(gate.get("reason_code") or "") == REASON_F10:
            mark_f10_hard_trip(gate)
        return MODE_ABORT
    return MODE_OPEN


def max_model_turns(mode: str) -> int:
    if mode in (MODE_OPEN, MODE_MANAGE):
        return MODEL_TURNS_MAX
    return 0


def max_tools(mode: str) -> int:
    if mode == MODE_OPEN:
        return TOOLS_ENTRY_MAX
    if mode == MODE_MANAGE:
        return TOOLS_MANAGE_MAX
    return 0


def turns_or_tools_breached(
    mode: str,
    *,
    model_turns: int = 0,
    tool_count: int = 0,
) -> str:
    """Force SKIP (OPEN) or MANAGE-only (MANAGE). Empty = under cap."""
    if mode not in (MODE_OPEN, MODE_MANAGE):
        return ""
    if int(model_turns or 0) > MODEL_TURNS_MAX:
        return REASON_TURNS
    cap = max_tools(mode)
    if cap and int(tool_count or 0) > cap:
        return REASON_TOOLS
    return ""


def research_prompt_ok(prompt_tokens: int) -> bool:
    try:
        n = int(prompt_tokens or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= n < RESEARCH_PROMPT_TOKENS_MAX


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
) -> str:
    """Non-empty = do not call the model. Unprotected last-stop still looks."""
    if not kill_look_enabled():
        return ""
    if unprotected:
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
    # New-risk / OPEN / research-as-entry. MANAGE + unprotected still look.
    if f10_open_look_halted(f10):
        try:
            from abcxauto.session_caps import mark_f10_loop_halt

            mark_f10_loop_halt()
        except Exception:
            logger.debug("f10 latch write failed", exc_info=True)
        gate = f10 if isinstance(f10, dict) else live_f10_gate()
        why = str(gate.get("reason_code") or "")
        if why == REASON_MODEL_COST:
            return REASON_MODEL_COST
        return REASON_F10
    if mode == MODE_OPEN:
        return ""
    if mode == MODE_ABORT:
        # Named scorecard fuses. Live F10 / port / spent look stay entry-budget
        # so look-#1 mill tests keep their existing skip code.
        if fuse == "DD30":
            return REASON_DD
        if fuse == "QTY0_STREAK":
            return REASON_QTY0
        if fuse == "F10":
            return REASON_F10
        return REASON_ENTRY_BUDGET
    if mode == MODE_RESEARCH:
        from abcxauto.session_caps import research_week_looks

        if int(research_week_looks(now=now) or 0) >= AH_RESEARCH_LOOKS_PER_WEEK:
            return REASON_RESEARCH_WEEK
        if not research_prompt_ok(prompt_tokens):
            return REASON_RESEARCH_PROMPT
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
    """Clerk block for illegal new pcs-skew risk. Exits still go."""
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
        logger.warning("F10 preferred $1 tripwire (hard still $2)")
        try:
            from abcxauto.think_stream import emit as think_emit

            think_emit("tool", "\n[F10 preferred $1 tripwire — hard still $2]\n")
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


def open_send_used(turn: Any = None) -> bool:
    """True when OPEN already placed a non-blocked ticket this look."""
    for item in getattr(turn, "sends", None) or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(result.get("status") or "").lower()
        strat = str(item.get("strat") or result.get("strategy") or "").lower()
        if status in ("blocked", "rejected", "validated_block"):
            continue
        if strat in ("blocked", "skipped"):
            continue
        return True
    return False


def one_open_send_block(mode: str, turn: Any = None) -> dict[str, Any] | None:
    """OPEN: one pcs-skew BAG then stop."""
    if mode != MODE_OPEN or not open_send_used(turn):
        return None
    return {
        "status": "blocked",
        "note": "OPEN: one pcs-skew BAG then stop",
        "reason_code": REASON_ONE_SEND,
        "strategy": "skipped",
    }


def force_skip_or_manage(
    mode: str,
    *,
    strategy: str = "",
    params: dict[str, Any] | None = None,
    breached: str = "",
) -> dict[str, Any] | None:
    """On turn/tool cap: OPEN → SKIP (no new send). MANAGE → closing only."""
    if not breached:
        return None
    dumped = dict(params or {})
    if mode == MODE_OPEN:
        return {
            "status": "blocked",
            "note": f"force SKIP ({breached})",
            "reason_code": breached,
            "strategy": "skipped",
        }
    if mode == MODE_MANAGE:
        if _is_closing(dumped):
            return None
        from abcxauto.agent_loop import is_new_risk

        if not is_new_risk(str(strategy or "").strip().lower(), dumped):
            return None
        return {
            "status": "blocked",
            "note": f"MANAGE-only ({breached})",
            "reason_code": breached,
            "strategy": "blocked",
        }
    return None
