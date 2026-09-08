"""Named-card research-brief budget + lineage promote gate (Rank 2).

research_card_id → prove_window_id → gate_verdict + model_cost_window_USD.
Hard $ / turns / tool-call budget per named card. Trip ⇒ brief_loop_halted.
Promote/lab refuses unless gate_verdict=PASS and model_cost_window present
(finite ≥ 0). Unreadable / non-finite cost fail-closes the loop and is
missing for promote.

V0 constants are code constants — not self_tune-raiseable.
AH_RESEARCH_LOOKS_PER_WEEK and RESEARCH_PROMPT_TOKENS_MAX stay additional
rails. Paper 7497. Not looking. Do not grow SYSTEM_PROMPT.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard rails. Not Settings knobs. Not self_tune-raiseable.
BRIEF_CARD_MODEL_HARD_USD = 1.50
BRIEF_CARD_TURNS_MAX = 8
BRIEF_CARD_TOOLS_MAX = 40
EST_BRIEF_TURN_USD = 0.20

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_INCONCLUSIVE = "INCONCLUSIVE"
GATE_KILL = "KILL"
GATE_VERDICTS = frozenset({GATE_PASS, GATE_FAIL, GATE_INCONCLUSIVE, GATE_KILL})

REASON_BRIEF_LOOP = "brief_loop_halted"
REASON_BRIEF_COST = "brief_model_cost_missing"
REASON_LAB_PROMOTE = "lab_promote_refused"

# Clerk name for the existing AH brief pipeline. Not a new strategy card.
DEFAULT_RESEARCH_CARD_ID = "research-brief"

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO / "data" / "state" / "research_budget.json"

_cache: dict[str, Any] | None = None
_cache_path: str = ""


def _path() -> Path:
    raw = (os.environ.get("ABCXAUTO_RESEARCH_BUDGET_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def reset_research_budget() -> None:
    """Drop the in-memory cache (tests)."""
    global _cache, _cache_path
    _cache = None
    _cache_path = ""


def _et_now(now: datetime | None = None) -> datetime:
    from zoneinfo import ZoneInfo

    clock = now or datetime.now(ZoneInfo("America/New_York"))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        clock = clock.astimezone(ZoneInfo("America/New_York"))
    return clock


def default_prove_window_id(*, now: datetime | None = None) -> str:
    """ISO week prove window (ET). Separate from the weekly look-count key."""
    iso = _et_now(now).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def card_key(research_card_id: str, prove_window_id: str) -> str:
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    return f"{card}::{window}"


def parse_model_cost(raw: Any) -> float | None:
    """Finite billed USD ≥ 0, or None (missing / unreadable)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val < 0:
        return None
    return val


def model_cost_present(raw: Any) -> bool:
    return parse_model_cost(raw) is not None


def _int_ge0(raw: Any) -> int:
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw in (1, "1", "true", "True", "yes", "on"):
        return True
    return False


def _verdict_of(raw: Any) -> str:
    token = str(raw or "").strip().upper()
    if token in GATE_VERDICTS:
        return token
    return ""


def _empty_row(research_card_id: str, prove_window_id: str) -> dict[str, Any]:
    return {
        "research_card_id": str(research_card_id or "").strip(),
        "prove_window_id": str(prove_window_id or "").strip(),
        "gate_verdict": "",
        "model_cost_window_USD": 0.0,
        "turns": 0,
        "tool_calls": 0,
        "brief_loop_halted": False,
        "halt_reason": "",
    }


def _row_of(raw: Any, research_card_id: str = "", prove_window_id: str = "") -> dict[str, Any]:
    blob = raw if isinstance(raw, dict) else {}
    card = str(blob.get("research_card_id") or research_card_id or "").strip()
    window = str(blob.get("prove_window_id") or prove_window_id or "").strip()
    cost_raw = blob.get("model_cost_window_USD", blob.get("model_cost_window_usd"))
    # Distinguish "key missing / unreadable" from billed 0. New writes always
    # store a float. A corrupt / NaN / null value stays None so promote fails.
    if "model_cost_window_USD" not in blob and "model_cost_window_usd" not in blob:
        stored: float | None = 0.0
    else:
        stored = parse_model_cost(cost_raw)
    return {
        "research_card_id": card,
        "prove_window_id": window,
        "gate_verdict": _verdict_of(blob.get("gate_verdict")),
        "model_cost_window_USD": stored,
        "turns": _int_ge0(blob.get("turns")),
        "tool_calls": _int_ge0(blob.get("tool_calls")),
        "brief_loop_halted": _flag(blob.get("brief_loop_halted")),
        "halt_reason": str(blob.get("halt_reason") or ""),
    }


def _load_table() -> dict[str, dict[str, Any]]:
    global _cache, _cache_path
    p = str(_path())
    if _cache is not None and _cache_path == p:
        return _cache
    table: dict[str, dict[str, Any]] = {}
    path = _path()
    blob: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                blob = raw
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            blob = {}
    cards = blob.get("cards") if isinstance(blob.get("cards"), dict) else {}
    for raw_key, raw_row in dict(cards or {}).items():
        key = str(raw_key or "")
        if not key:
            continue
        row = _row_of(raw_row)
        if not row["research_card_id"] or not row["prove_window_id"]:
            parts = key.split("::", 1)
            if len(parts) == 2:
                row["research_card_id"] = row["research_card_id"] or parts[0]
                row["prove_window_id"] = row["prove_window_id"] or parts[1]
        table[key] = row
    _cache = table
    _cache_path = p
    return table


def _save_table(table: dict[str, dict[str, Any]]) -> None:
    global _cache, _cache_path
    path = _path()
    clean: dict[str, dict[str, Any]] = {}
    for raw_key, raw_row in dict(table or {}).items():
        key = str(raw_key or "")
        if not key:
            continue
        row = _row_of(raw_row)
        if not row["research_card_id"] or not row["prove_window_id"]:
            continue
        clean[card_key(row["research_card_id"], row["prove_window_id"])] = row
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cards": clean}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug("research_budget write failed", exc_info=True)
    _cache = clean
    _cache_path = str(path)


def card_row(
    research_card_id: str,
    prove_window_id: str,
) -> dict[str, Any] | None:
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    if not card or not window:
        return None
    table = _load_table()
    row = table.get(card_key(card, window))
    if not isinstance(row, dict):
        return None
    return dict(row)


def lineage_fields(row: dict[str, Any] | None) -> dict[str, Any]:
    blob = row if isinstance(row, dict) else {}
    return {
        "research_card_id": str(blob.get("research_card_id") or ""),
        "prove_window_id": str(blob.get("prove_window_id") or ""),
        "gate_verdict": str(blob.get("gate_verdict") or ""),
        "model_cost_window_USD": blob.get("model_cost_window_USD"),
    }


def open_research_card(
    research_card_id: str,
    prove_window_id: str,
    *,
    gate_verdict: str = "",
) -> dict[str, Any]:
    """Create or return the named-card ledger row. Cost starts billed 0."""
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    if not card or not window:
        return _empty_row(card, window)
    table = _load_table()
    key = card_key(card, window)
    row = table.get(key)
    if not isinstance(row, dict):
        row = _empty_row(card, window)
        verdict = _verdict_of(gate_verdict)
        if verdict:
            row["gate_verdict"] = verdict
        table[key] = row
        _save_table(table)
        return dict(row)
    if gate_verdict:
        verdict = _verdict_of(gate_verdict)
        if verdict and not row.get("gate_verdict"):
            row["gate_verdict"] = verdict
            table[key] = row
            _save_table(table)
    return dict(row)


def ensure_research_card(
    research_card_id: str,
    prove_window_id: str,
) -> dict[str, Any]:
    return open_research_card(research_card_id, prove_window_id)


def resolve_research_card(
    *,
    research_card_id: str = "",
    prove_window_id: str = "",
    snap: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Named card + prove window. Brief / snap win over the clerk default."""
    bag = snap if isinstance(snap, dict) else {}
    disk = brief if isinstance(brief, dict) else None
    if disk is None:
        try:
            from abcxauto.desk_mode import load_research_brief

            loaded = load_research_brief()
            disk = loaded if isinstance(loaded, dict) else {}
        except Exception:
            disk = {}
    card = (
        str(research_card_id or "").strip()
        or str(bag.get("research_card_id") or "").strip()
        or str(disk.get("research_card_id") or "").strip()
        or DEFAULT_RESEARCH_CARD_ID
    )
    window = (
        str(prove_window_id or "").strip()
        or str(bag.get("prove_window_id") or "").strip()
        or str(disk.get("prove_window_id") or "").strip()
        or default_prove_window_id(now=now)
    )
    return card, window


def brief_budget_gate(
    cost: Any,
    turns: Any = 0,
    tools: Any = 0,
    *,
    est: Any = EST_BRIEF_TURN_USD,
    add_tools: Any = 0,
) -> dict[str, Any]:
    """Trip any: projected $ · turns · tool-calls. Unreadable cost fail-closes."""
    so_far = parse_model_cost(cost)
    if so_far is None:
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_COST,
            "note": "model_cost_window unreadable — fail-closed",
            "projected": None,
        }
    add = parse_model_cost(est)
    if add is None:
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_COST,
            "note": "model_cost_window unreadable — fail-closed",
            "projected": None,
        }
    n_turns = _int_ge0(turns)
    n_tools = _int_ge0(tools)
    extra_tools = _int_ge0(add_tools)
    if n_turns >= BRIEF_CARD_TURNS_MAX:
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_LOOP,
            "note": f"brief turns {n_turns} >= {BRIEF_CARD_TURNS_MAX}",
            "projected": so_far + add,
        }
    if n_tools + extra_tools > BRIEF_CARD_TOOLS_MAX or n_tools >= BRIEF_CARD_TOOLS_MAX:
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_LOOP,
            "note": (
                f"brief tools {n_tools}+{extra_tools} "
                f"> {BRIEF_CARD_TOOLS_MAX}"
            ),
            "projected": so_far + add,
        }
    projected = so_far + add
    if projected > BRIEF_CARD_MODEL_HARD_USD:
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_LOOP,
            "note": (
                f"model_cost_window {so_far} + est {add} "
                f"> {BRIEF_CARD_MODEL_HARD_USD}"
            ),
            "projected": projected,
        }
    return {
        "allow": True,
        "reason_code": "",
        "note": "",
        "projected": projected,
    }


def mark_brief_loop_halt(
    research_card_id: str,
    prove_window_id: str,
    *,
    reason: str = REASON_BRIEF_LOOP,
    missing_cost: bool = False,
) -> dict[str, Any]:
    """Latch brief_loop_halted for this card/window. Idempotent."""
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    if not card or not window:
        return _empty_row(card, window)
    table = _load_table()
    key = card_key(card, window)
    row = _row_of(table.get(key) or _empty_row(card, window), card, window)
    row["brief_loop_halted"] = True
    row["halt_reason"] = str(reason or REASON_BRIEF_LOOP)
    if missing_cost:
        row["model_cost_window_USD"] = None
    table[key] = row
    _save_table(table)
    return dict(row)


def brief_loop_halted(
    research_card_id: str = "",
    prove_window_id: str = "",
    *,
    snap: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    card, window = resolve_research_card(
        research_card_id=research_card_id,
        prove_window_id=prove_window_id,
        snap=snap,
        now=now,
    )
    row = card_row(card, window)
    if not isinstance(row, dict):
        return False
    if row.get("brief_loop_halted"):
        return True
    if row.get("model_cost_window_USD") is None:
        return True
    return False


def allow_brief_turn(
    research_card_id: str,
    prove_window_id: str,
    *,
    est: Any = EST_BRIEF_TURN_USD,
    add_tools: int = 0,
) -> dict[str, Any]:
    """False when halted or the next billed turn would trip any rail."""
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    if not card or not window:
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_COST,
            "note": "research_card_id / prove_window_id missing — fail-closed",
            "projected": None,
        }
    row = card_row(card, window)
    if row is None:
        return {
            "allow": True,
            "reason_code": "",
            "note": "",
            "projected": parse_model_cost(est) or EST_BRIEF_TURN_USD,
        }
    if row.get("brief_loop_halted"):
        return {
            "allow": False,
            "reason_code": str(row.get("halt_reason") or REASON_BRIEF_LOOP),
            "note": "brief_loop_halted",
            "projected": None,
        }
    cost = row.get("model_cost_window_USD")
    if cost is None or parse_model_cost(cost) is None:
        mark_brief_loop_halt(card, window, reason=REASON_BRIEF_COST, missing_cost=True)
        return {
            "allow": False,
            "reason_code": REASON_BRIEF_COST,
            "note": "model_cost_window unreadable — fail-closed",
            "projected": None,
        }
    gate = brief_budget_gate(
        cost,
        row.get("turns"),
        row.get("tool_calls"),
        est=est,
        add_tools=add_tools,
    )
    if not gate.get("allow"):
        missing = str(gate.get("reason_code") or "") == REASON_BRIEF_COST
        mark_brief_loop_halt(
            card,
            window,
            reason=str(gate.get("reason_code") or REASON_BRIEF_LOOP),
            missing_cost=missing,
        )
    return gate


def note_brief_turn(
    research_card_id: str,
    prove_window_id: str,
    *,
    cost_usd: Any = EST_BRIEF_TURN_USD,
    tool_calls: int = 0,
) -> dict[str, Any]:
    """Increment the named-card ledger. After trip, no further billing."""
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    add_tools = _int_ge0(tool_calls)
    if not card or not window:
        return {
            **_empty_row(card, window),
            "billed": False,
            "reason_code": REASON_BRIEF_COST,
        }
    ensure_research_card(card, window)
    gate = allow_brief_turn(card, window, est=cost_usd, add_tools=add_tools)
    row = card_row(card, window) or ensure_research_card(card, window)
    if not gate.get("allow"):
        out = dict(card_row(card, window) or row)
        out["billed"] = False
        out["reason_code"] = str(gate.get("reason_code") or REASON_BRIEF_LOOP)
        return out
    add = parse_model_cost(cost_usd)
    if add is None:
        halted = mark_brief_loop_halt(
            card, window, reason=REASON_BRIEF_COST, missing_cost=True
        )
        halted["billed"] = False
        halted["reason_code"] = REASON_BRIEF_COST
        return halted
    table = _load_table()
    key = card_key(card, window)
    row = _row_of(table.get(key) or _empty_row(card, window), card, window)
    so_far = parse_model_cost(row.get("model_cost_window_USD"))
    if so_far is None:
        halted = mark_brief_loop_halt(
            card, window, reason=REASON_BRIEF_COST, missing_cost=True
        )
        halted["billed"] = False
        halted["reason_code"] = REASON_BRIEF_COST
        return halted
    row["turns"] = _int_ge0(row.get("turns")) + 1
    row["tool_calls"] = _int_ge0(row.get("tool_calls")) + add_tools
    row["model_cost_window_USD"] = so_far + add
    if (
        row["turns"] >= BRIEF_CARD_TURNS_MAX
        or row["tool_calls"] >= BRIEF_CARD_TOOLS_MAX
        or float(row["model_cost_window_USD"]) > BRIEF_CARD_MODEL_HARD_USD
    ):
        row["brief_loop_halted"] = True
        row["halt_reason"] = REASON_BRIEF_LOOP
    table[key] = row
    _save_table(table)
    out = dict(row)
    out["billed"] = True
    out["reason_code"] = str(row.get("halt_reason") or "")
    return out


def set_gate_verdict(
    research_card_id: str,
    prove_window_id: str,
    verdict: str,
) -> dict[str, Any]:
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    token = _verdict_of(verdict)
    row = ensure_research_card(card, window)
    if not card or not window or not token:
        return row
    table = _load_table()
    key = card_key(card, window)
    row = _row_of(table.get(key) or row, card, window)
    row["gate_verdict"] = token
    table[key] = row
    _save_table(table)
    return dict(row)


def set_model_cost_window(
    research_card_id: str,
    prove_window_id: str,
    cost: Any,
) -> dict[str, Any]:
    """Stamp billed window cost. Unreadable / non-finite fail-closes + missing."""
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    parsed = parse_model_cost(cost)
    if parsed is None:
        return mark_brief_loop_halt(
            card, window, reason=REASON_BRIEF_COST, missing_cost=True
        )
    row = ensure_research_card(card, window)
    table = _load_table()
    key = card_key(card, window)
    row = _row_of(table.get(key) or row, card, window)
    row["model_cost_window_USD"] = parsed
    table[key] = row
    _save_table(table)
    return dict(row)


def lab_promote_ok(row: dict[str, Any] | None) -> bool:
    """True only when gate_verdict=PASS and model_cost_window is finite ≥ 0."""
    blob = row if isinstance(row, dict) else {}
    if _verdict_of(blob.get("gate_verdict")) != GATE_PASS:
        return False
    return model_cost_present(blob.get("model_cost_window_USD"))


def lab_promote(
    research_card_id: str,
    prove_window_id: str,
) -> dict[str, Any]:
    """Promote/lab path. Refuse unless PASS + finite model_cost_window_USD."""
    card = str(research_card_id or "").strip()
    window = str(prove_window_id or "").strip()
    row = card_row(card, window)
    lineage = lineage_fields(row)
    if row is None:
        return {
            "status": "refused",
            "ok": False,
            "allowed": False,
            "reason_code": REASON_LAB_PROMOTE,
            "note": "promote/lab refused — missing lineage",
            **lineage,
            "research_card_id": card,
            "prove_window_id": window,
        }
    if not lab_promote_ok(row):
        verdict = str(row.get("gate_verdict") or "")
        cost = row.get("model_cost_window_USD")
        if not model_cost_present(cost):
            note = "promote/lab refused — model_cost_window missing"
        elif verdict != GATE_PASS:
            note = f"promote/lab refused — gate_verdict={verdict or 'missing'}"
        else:
            note = "promote/lab refused"
        return {
            "status": "refused",
            "ok": False,
            "allowed": False,
            "reason_code": REASON_LAB_PROMOTE,
            "note": note,
            **lineage,
        }
    return {
        "status": "ok",
        "ok": True,
        "allowed": True,
        "reason_code": "",
        "note": "lab promote allowed",
        **lineage,
    }


promote_lab = lab_promote


def is_brief_look_halt(reason: str = "") -> bool:
    return str(reason or "") in (REASON_BRIEF_LOOP, REASON_BRIEF_COST)


def research_brief_skip_reason(
    session: str = "",
    *,
    snap: dict[str, Any] | None = None,
    now: datetime | None = None,
    unprotected: bool = False,
) -> str:
    """Non-empty = do not start a billed research/brief turn."""
    if unprotected:
        return ""
    try:
        from abcxauto.desk_mode import is_research_session, is_rth_session

        if not is_research_session(session) or is_rth_session(session):
            return ""
    except Exception:
        sess = str(session or "").strip().lower()
        if sess in ("", "regular", "unknown"):
            return ""
    card, window = resolve_research_card(snap=snap, now=now)
    gate = allow_brief_turn(card, window)
    if gate.get("allow"):
        return ""
    return str(gate.get("reason_code") or REASON_BRIEF_LOOP)


def stamp_brief_lineage(
    payload: dict[str, Any],
    *,
    research_card_id: str = "",
    prove_window_id: str = "",
    snap: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write named-card lineage onto a research brief payload."""
    bag = payload if isinstance(payload, dict) else {}
    card, window = resolve_research_card(
        research_card_id=research_card_id,
        prove_window_id=prove_window_id,
        snap=snap,
        brief=bag,
        now=now,
    )
    row = ensure_research_card(card, window)
    bag.update(lineage_fields(row))
    bag["brief_loop_halted"] = bool(row.get("brief_loop_halted"))
    return bag
