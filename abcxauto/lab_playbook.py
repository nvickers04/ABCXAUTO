"""Paper lab playbook â€” Grok's notebook; live only follows a promote.

One tree, two layers, both written by Grok::

    TYPE market_bracket            <- durable: tool_order, gotchas, review
      |- card: mega-cap earnings-flush bounce   <- thesis, evidence, retire_if
      |- card: opening-range continuation
    TYPE vertical_spread
      |- card: post-earnings IV crush

* the **trunk** is ``types``: one entry per sendable ORDER_EXAMPLES key holding
  what Grok learned about *executing that structure* â€” the tool sequence that
  works, the execution gotchas, how it reviews the result. Durable, changes
  slowly. The clerk never writes schema here: ``ORDER EXAMPLES`` is already in
  the prompt, and restating it was how ~40% of the old notebook became
  boilerplate. See ``type_schema_echo_keys``.
* the **branches** are that type's ``cards``: disposable hypotheses, each
  carrying its thesis, the evidence that produced it, and the falsification it
  declares for itself (``retire_if``). Numerous, tested, retired.

A card's position in the tree *is* its ticket, so a winning card sits inside
the type entry it is supposed to improve â€” promoting what it learned is a move
within one stanza, not a join across two lists. Card identity is therefore
``(type, name)``, not a bare name.

The clerk's job is attribution, not authorship: ``send`` must name a card under
the type it is sending (new risk only â€” exits are never blocked), every
dispatched ticket is tagged, and each card is scored on its own resolved
trades. Operator flattens, panic, and halt exits are tracked but kept out of
the graduation math â€” an interrupted trade is neither proof nor falsification.
A card graduates when it meets *its own* declared sample with positive resolved
edge; only graduated cards, inside their pruned type stanzas, reach
``playbook_live.json``.

A flat top-level ``cards`` list is still accepted on a write and is still
*projected* on a read for the cockpit and older callers, but the tree is the
only thing stored. See ``_migrate_book`` and ``_flat_card_projection``.

Notebook is not executable, not a wake clock, not a standing order. Clerk
validates writes against gates (floors / live / sleeve) like self_tune.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LAB = _REPO_ROOT / "playbook_lab.json"
_DEFAULT_LIVE = _REPO_ROOT / "playbook_live.json"
_MAX_INSTRUCTIONS = 16000
_LEDGER_CAP = 12
_PATCH_KEYS = (
    "instructions",
    "cards",
    "types",
    "catalog",
    "mode",
    "ready_to_promote",
)
# A type entry holds only what Grok learned about running that structure.
# open_shape / close_tp_sl / defined_risk were clerk-derived restatements of
# ORDER EXAMPLES — they are stripped on read and on write, not stored.
TYPE_LEARNED_FIELDS = ("tool_order", "gotchas", "review", "note")
_TYPE_SCHEMA_ECHO = (
    "open_shape",
    "close_tp_sl",
    "defined_risk",
    "ticket_shape",
    "strategies",
)
# Cards are the book. A card lives *under* its type, so the parent key is the
# ticket and identity is (type, name). Shape lives in ``_norm_card``.
_MAX_CARDS = 24
_MAX_CARDS_PER_TYPE = 12
# Legacy cards that named no type at all. Never dropped, never sendable (they
# have no ticket to send), surfaced as owing a parent.
UNFILED_KEY = "unfiled_cards"
# Derived on read for the cockpit and older callers; never written to disk.
_PROJECTED_KEYS = ("cards",)
# What a card actually used. Not a checklist — populate what applies.
EVIDENCE_FIELDS = ("scan", "news", "reads", "odds")
CARD_STATUSES = ("testing", "working", "retired")
# Resolved trades before a hit rate is worth printing. Matches path_math's
# refusal to compute expectancy on a thinner sample than this.
_CALIBRATION_MIN_N = 4
# How a card's trade ended. Only ``protective`` and ``decision`` are evidence.
EXIT_PROTECTIVE = "protective"
EXIT_DECISION = "decision"
EXIT_OPERATOR = "operator"
EXIT_OPEN = "open"
CARD_EXIT_KINDS = (EXIT_PROTECTIVE, EXIT_DECISION, EXIT_OPERATOR, EXIT_OPEN)
RESOLVED_EXITS = (EXIT_PROTECTIVE, EXIT_DECISION)
# Not catalog trunks: knobs, plus defined_risk_only rejects.
_SKIP_PLAYBOOK_TYPES = frozenset({
    "set_risk",
    "self_tune",
    "ratio_spread",
    "jade_lizard",
})
# Trunk = these sendable ORDER_EXAMPLES keys. Never invert.
PLAYBOOK_TYPE_KEYS = (
    "market_bracket",
    "bracket",
    "trailing_stop",
    "modify_stop",
    "modify_target",
    "cancel_order",
    "close_option",
    "buy_option",
    "vertical_spread",
    "calendar_spread",
    "diagonal_spread",
    "butterfly",
    "iron_butterfly",
    "iron_condor",
    "straddle",
    "strangle",
    "protective_put",
    "collar",
    "covered_call",
    "cash_secured_put",
)
_TYPE_META_KEYS = frozenset(
    {
        "mode",
        "ready_to_promote",
        "instructions",
        "types",
        "catalog",
        "default_tool_recipe",
    }
    | set(TYPE_LEARNED_FIELDS)
    | set(_TYPE_SCHEMA_ECHO)
)
# Only an unsendable ticket kills a write now. Notes, regime reads, and
# per-name observations are the point of the book.
_HARD_SHAPE = frozenset({"unknown_type"})
# Ceremony leftovers. Must not linger via save_lab merging **prev.
_DEAD_LAB_KEYS = (
    "do_more",
    "stop_doing",
    "basis",
    "evidence",
    "research_tools",
    "diary",
    "nap",
    "naps",
    "wake_at",
    "wake_in_s",
    "wake_if",
    "ticker_list",
    "tickers",
)
# Notebook is not self_tune. Same class of knobs self_tune rejects or clamps â€”
# write_lab_playbook must not loosen floors, switch live, or set a dollar sleeve.
_GATE_FORBIDDEN: dict[str, str] = {
    "trading_mode": "live remains gated â€” notebook cannot switch mode",
    "live_confirm": "live remains gated â€” notebook cannot switch mode",
    "sizing_floors": "operator-only â€” notebook cannot flip sizing floors",
    "trading_budget_usd": "size and risk are % of NetLiq â€” no dollar sleeve",
    "risk_posture": "risk_posture is locked â€” notebook cannot retune",
    "daily_loss_limit_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_position_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_risk_per_trade_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_peak_drawdown_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_option_premium_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_open_positions": "knobs are self_tune â€” notebook cannot retune risk",
    "risk_gates_enabled": "immutable floor â€” notebook cannot disable",
    "auto_panic_on_breach": "immutable floor â€” notebook cannot disable",
    "defined_risk_only": "immutable floor â€” notebook cannot disable",
    "cash_only": "immutable floor â€” notebook cannot disable",
}
# GATES: N% / floor N% NL is clerk law. Notebook may restate it only when
# sizing_floors is ON and N is the live max_risk_per_trade_pct knob.
_GATES_HDR = re.compile(r"\bGATES\b[^:\n]{0,48}:", re.IGNORECASE)
_FLOOR_NL = re.compile(r"\bfloor\s+(\d+(?:\.\d+)?)\s*%\s*NL\b", re.IGNORECASE)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TYPE_HDR = re.compile(r"^TYPE\s+(\S+)", re.IGNORECASE)
_FIELD_LINE = re.compile(
    r"^(tool_order|default_tool_recipe|gotchas|review|note)\s*[:=]\s*(.*)$",
    re.IGNORECASE,
)
_STALE_H_DEFAULT = 1.0
_CARD_WINDOWS = ("15m", "1h", "4h")


def stale_hours() -> float:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_STALE_H") or "").strip()
    if not raw:
        return _STALE_H_DEFAULT
    try:
        return max(0.25, float(raw))
    except ValueError:
        return _STALE_H_DEFAULT


def _lab_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_LAB_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_LAB


def _live_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_LIVE_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_LIVE


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("playbook read failed path=%s", path, exc_info=True)
        return {}


def _write(path: Path, state: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("playbook write failed path=%s", path)


def _drop_dead_lab_keys(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in _DEAD_LAB_KEYS:
        out.pop(key, None)
    for key in _GATE_FORBIDDEN:
        out.pop(key, None)
    out.pop("risk", None)
    out.pop("universe", None)
    out.pop("rejected", None)
    return out


def _file_cards_into_tree(
    types: Any,
    cards: list[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Route loose cards under the type they name, creating the trunk if new.

    A card is never dropped for want of a stanza. One that names nothing
    sendable cannot be placed, so it lands in ``unfiled_cards`` where it is
    still visible and still owed a parent.
    """
    allowed = set(playbook_type_keys())
    tree: dict[str, Any] = {
        k: dict(v) for k, v in (types or {}).items() if isinstance(v, dict)
    } if isinstance(types, dict) else {}
    unfiled: list[dict[str, Any]] = []
    for raw in cards:
        row = _norm_card(raw)
        if row is None:
            continue
        ticket = card_ticket_of(raw)
        if not ticket or ticket not in allowed:
            if ticket:
                # Unsendable, so it has no parent — but keep what it claimed so
                # the cockpit can still say which ticket does not exist.
                row["claimed_ticket"] = ticket
            if not any(c["name"].lower() == row["name"].lower() for c in unfiled):
                unfiled.append(row)
            continue
        stanza = dict(tree.get(ticket) or {})
        branch = [
            c
            for c in (stanza.get("cards") or [])
            if isinstance(c, dict)
            and str(c.get("name") or "").lower() != row["name"].lower()
        ]
        branch.append(row)
        stanza["cards"] = branch
        tree[ticket] = stanza
    return tree, unfiled


def _migrate_book(state: dict[str, Any]) -> dict[str, Any]:
    """Read-time migration to the tree. Type schema echoes drop; cards do not.

    A flat ``cards`` list from the old shape is filed under each card's
    ``ticket`` with every field, note and status intact. Revision 1 cards have
    no ``thesis``, ``evidence`` or ``retire_if``: they are real work product, so
    they migrate with empty declarations and are surfaced as owing one on the
    next write, never retired and never dropped.
    """
    if not isinstance(state, dict) or not state:
        return {}
    out = _drop_dead_lab_keys(state)
    loose = list(out.get("cards") or []) + list(out.get(UNFILED_KEY) or [])
    tree, unfiled = _file_cards_into_tree(out.get("types"), loose)
    types = _clean_types(tree)
    if types or "types" in out:
        out["types"] = types
    if unfiled:
        out[UNFILED_KEY] = unfiled
    else:
        out.pop(UNFILED_KEY, None)
    projected = _flat_card_projection(out)
    if projected or "cards" in out:
        out["cards"] = projected
    return out


def load_lab() -> dict[str, Any]:
    return _migrate_book(_read(_lab_path()))


def load_live() -> dict[str, Any]:
    return _migrate_book(_read(_live_path()))


def is_paper() -> bool:
    try:
        from abcxauto.config import get_config

        return bool(get_config().is_paper)
    except Exception:
        return True


def _field(raw: dict[str, Any], prev: dict[str, Any], key: str, default: str = "") -> str:
    if key in raw:
        return str(raw.get(key) or default)
    return str(prev.get(key) or default)


def playbook_type_keys() -> tuple[str, ...]:
    """Sendable ORDER_EXAMPLES keys the notebook may use as trunks."""
    from abcxauto.order_examples import NOT_TICKETS, ORDER_EXAMPLES

    skip = _SKIP_PLAYBOOK_TYPES | NOT_TICKETS
    return tuple(k for k in PLAYBOOK_TYPE_KEYS if k in ORDER_EXAMPLES and k not in skip)


def type_coverage(book: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Operator view: every sendable trunk and whether Grok has filled it in.

    Derived on read for the cockpit, like ``_flat_card_projection`` — never
    stored and never in the payload, because a stanza per sendable key is the
    boilerplate ``empty_type_catalog`` exists to refuse. An untouched trunk here
    is a gap in the notebook, not a slot waiting to be seeded with schema.
    """
    state = book if isinstance(book, dict) else load_lab()
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    out: list[dict[str, Any]] = []
    for name in playbook_type_keys():
        row = types.get(name) if isinstance(types.get(name), dict) else {}
        learned: list[str] = []
        for field in TYPE_LEARNED_FIELDS:
            val = row.get(field)
            if isinstance(val, (list, tuple)):
                if val:
                    learned.append(field)
            elif str(val or "").strip():
                learned.append(field)
        cards = [
            c for c in (row.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        out.append({
            "type": name,
            "cards": len(cards),
            "learned": learned,
            "touched": bool(learned or cards),
        })
    return out


def empty_type_catalog() -> dict[str, Any]:
    """No stanzas. A type exists in the book once Grok has learned something.

    The old catalog seeded every sendable key with ``open_shape`` /
    ``close_tp_sl`` copied out of ORDER_EXAMPLES, which is already in the
    prompt. Seeding nothing is the fix.
    """
    return {}


def _norm_recipe(raw: Any) -> list[str]:
    """Optional tool sequence. Stored, never gated."""
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,;>]+|->", raw) if p.strip()]
        return parts[:16]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()][:16]
    return []


def _floors_and_knob() -> tuple[bool, float]:
    """Live clerk flag + max_risk_per_trade_pct. Fail closed: floors off."""
    try:
        from abcxauto.config import get_config
        from abcxauto.risk_gates import sizing_floors_active

        cfg = get_config()
        knob = float(getattr(cfg, "max_risk_per_trade_pct", 0) or 0)
        return bool(sizing_floors_active(cfg)), knob
    except Exception:
        return False, 0.0


def _gate_pcts_on_line(line: str) -> list[float]:
    """Percents claimed as GATES: N% or floor N% NL on one line."""
    pcts: list[float] = []
    if _GATES_HDR.search(line):
        pcts.extend(float(m.group(1)) for m in _PCT.finditer(line))
    pcts.extend(float(m.group(1)) for m in _FLOOR_NL.finditer(line))
    return pcts


def _invented_pct_gate_line(line: str, floors_on: bool, knob: float) -> bool:
    """True when this line invents a % gate the clerk is not enforcing."""
    pcts = _gate_pcts_on_line(line)
    if not pcts:
        return False
    if floors_on and knob > 0 and all(abs(n - knob) < 1e-6 for n in pcts):
        return False
    return True


def _has_invented_pct_gate(text: str) -> bool:
    floors_on, knob = _floors_and_knob()
    return any(_invented_pct_gate_line(line, floors_on, knob) for line in text.splitlines())


def _strip_invented_pct_gate_lines(text: str) -> str:
    """Drop GATES: N% / floor N% NL lines unless floors ON and N is the live knob."""
    floors_on, knob = _floors_and_knob()
    kept = [
        line
        for line in text.splitlines()
        if not _invented_pct_gate_line(line, floors_on, knob)
    ]
    return "\n".join(kept)


def _walk_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_walk_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "\n".join(_walk_text(v) for v in obj)
    return ""


def _norm_type_row(row: Any, *, prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """One trunk: learned execution plus its cards.

    Schema echoes are dropped, never merged forward. ``cards`` is a full
    replace when present and keeps the previous branch list when omitted, so
    a note-only write does not silently prune the hypotheses under it.
    """
    prev = prev if isinstance(prev, dict) else {}
    src = row if isinstance(row, dict) else {}
    out: dict[str, Any] = {}
    incoming_order = src.get("tool_order", src.get("default_tool_recipe"))
    if "tool_order" in src or "default_tool_recipe" in src:
        rec = _norm_recipe(incoming_order)
    else:
        rec = _norm_recipe(prev.get("tool_order") or prev.get("default_tool_recipe"))
    if rec:
        out["tool_order"] = rec
    for key in ("gotchas", "review", "note"):
        val = src.get(key) if key in src else prev.get(key)
        if key == "note" and val in (None, ""):
            val = src.get("notes") if "notes" in src else None
        text = _strip_invented_pct_gate_lines(str(val or "")).strip()[:1200]
        if text:
            out[key] = text
    if "cards" in src:
        cards = _norm_cards(
            src.get("cards"),
            cap=_MAX_CARDS_PER_TYPE,
            prev=prev.get("cards"),
        )
    else:
        cards = _norm_cards(prev.get("cards"), cap=_MAX_CARDS_PER_TYPE)
    if cards:
        out["cards"] = cards
    return out


def _clean_types(types: Any) -> dict[str, Any]:
    """Keep sendable trunks that hold learnings or cards. Drop schema echoes."""
    if not isinstance(types, dict):
        return {}
    out: dict[str, Any] = {}
    budget = _MAX_CARDS
    for name in playbook_type_keys():
        row = types.get(name)
        if not isinstance(row, dict):
            continue
        stanza = _norm_type_row(row, prev={})
        cards = stanza.get("cards") or []
        if cards:
            # Tree-wide cap, applied trunk by trunk in catalog order.
            keep = cards[:budget]
            budget -= len(keep)
            if keep:
                stanza["cards"] = keep
            else:
                stanza.pop("cards", None)
        if stanza:
            out[name] = stanza
    return out


def type_cards(types: Any, name: str) -> list[dict[str, Any]]:
    """Cards branching under one type."""
    row = (types or {}).get(name) if isinstance(types, dict) else None
    if not isinstance(row, dict):
        return []
    return [c for c in (row.get("cards") or []) if isinstance(c, dict) and c.get("name")]


def walk_cards(state: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(type, card)`` pair in the tree, in catalog order.

    Unfiled legacy cards come last with an empty type: they are surfaced and
    scored, but nothing can send them.
    """
    blob = state if isinstance(state, dict) else {}
    types = blob.get("types") if isinstance(blob.get("types"), dict) else {}
    out: list[tuple[str, dict[str, Any]]] = []
    for name in playbook_type_keys():
        for card in type_cards(types, name):
            out.append((name, card))
    for card in blob.get(UNFILED_KEY) or []:
        if isinstance(card, dict) and card.get("name"):
            out.append(("", card))
    return out


def card_key(type_name: Any, card_name: Any) -> tuple[str, str]:
    """Identity of a card: its type and its name, both case-folded."""
    return (
        str(type_name or "").strip().lower(),
        str(card_name or "").strip().lower(),
    )


def _flat_card_projection(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Read-only flat view of the tree, each card stamped with its parent.

    The cockpit and the scorecard table were written against a flat list. The
    tree is the only stored shape, so this is derived on read and stripped
    before any write â€” see ``_strip_projection``.
    """
    out: list[dict[str, Any]] = []
    for type_name, card in walk_cards(state):
        row = dict(card)
        row["ticket"] = type_name or str(card.get("claimed_ticket") or "")
        row["type"] = type_name
        out.append(row)
    return out


def _strip_projection(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in _PROJECTED_KEYS:
        out.pop(key, None)
    return out


def type_schema_echo_keys(types: Any) -> list[str]:
    """Schema-restatement keys still present on a type map. Should be empty."""
    if not isinstance(types, dict):
        return []
    found: list[str] = []
    for row in types.values():
        if not isinstance(row, dict):
            continue
        for key in _TYPE_SCHEMA_ECHO:
            if key in row and key not in found:
                found.append(key)
    return found


def _norm_evidence(raw: Any, *, scan: str = "") -> dict[str, str]:
    """What the card actually used. Populate what applies, not a checklist."""
    src = raw if isinstance(raw, dict) else {}
    if isinstance(raw, str) and raw.strip():
        src = {"reads": raw}
    out: dict[str, str] = {}
    for key in EVIDENCE_FIELDS:
        val = src.get(key)
        if key == "reads" and val in (None, "", []):
            val = src.get("tool_reads")
        if key == "news" and val in (None, "", []):
            val = src.get("headlines")
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(x).strip() for x in val if str(x).strip())
        text = str(val or "").strip()[:800]
        if text:
            out[key] = text
    if scan and not out.get("scan"):
        out["scan"] = scan
    return out


def _norm_expect_hit_rate(raw: Any) -> float | None:
    """The win rate a card claims, as a percent. Measured, never enforced.

    A fraction (``0.62``) and a percent (``62``) both mean 62 — the model
    writes whichever it thinks in, and ``1`` reads as certainty, not 1%.
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val <= 1.0:
        val *= 100.0
    if val > 100.0:
        return None
    return round(val, 1)


def _norm_retire_if(raw: Any) -> dict[str, Any]:
    """The card's own falsification. Clerk enforces it; it never invents one."""
    if isinstance(raw, str):
        text = raw.strip()
        return {"condition": text[:600]} if text else {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    sample = raw.get("sample", raw.get("sample_size", raw.get("n")))
    try:
        n = int(float(sample)) if sample not in (None, "") else 0
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        out["sample"] = min(n, 200)
    cond = str(raw.get("condition") or raw.get("retire_if") or "").strip()
    if cond:
        out["condition"] = _strip_invented_pct_gate_lines(cond).strip()[:600]
    for key, cap in (("max_loss_usd", 1e9), ("max_losses", 200)):
        val = raw.get(key)
        if val in (None, ""):
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        out[key] = int(min(num, cap)) if key == "max_losses" else round(min(num, cap), 2)
    return out


def card_ticket_of(raw: Any) -> str:
    """The type a loose card claims. Only used to file it under its parent."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("ticket") or raw.get("strategy") or "").strip().lower()[:60]


def _incoming_card_name(raw: Any) -> str:
    """Name on an unnormalized card, for matching it to the one on disk."""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("setup") or "").strip()
    return ""


def _norm_card(raw: Any, *, prev: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One card. Its parent type is the ticket, so no ``ticket`` is stored.

    A card already on the tree keeps its declarations when a write omits them.
    ``cards`` is a full replace, so an evidence-only rewrite used to delete the
    ``retire_if`` it did not restate: the clerk then reported the card as owing a
    declaration, and the model spent a second write putting it back — every look,
    and the card could neither graduate nor trip in between. Observations still
    replace on every write; declarations persist until Grok changes them.
    """
    carried = prev if isinstance(prev, dict) else {}
    if isinstance(raw, str):
        name = raw.strip()
        raw = {"name": name} if name else {}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("setup") or "").strip()
    if not name:
        return None
    scan = raw.get("scan")
    if isinstance(scan, (list, tuple)):
        scan = ", ".join(str(x).strip() for x in scan if str(x).strip())
    scan_s = str(scan or "").strip()[:400]
    evidence = _norm_evidence(raw.get("evidence"), scan=scan_s)
    # ``scan`` stays top-level as well: it is the one evidence field that
    # already exists on disk and on the cockpit card. What the card wrote there
    # wins — the evidence copy only fills the gap.
    scan_s = scan_s or evidence.get("scan") or ""
    status = str(raw.get("status") or "testing").strip().lower()
    if status not in CARD_STATUSES:
        # Graduation is the clerk's verdict from resolved trades, not a status
        # Grok can assert.
        status = "working" if status == "graduated" else "testing"
    thesis = _strip_invented_pct_gate_lines(
        str(raw.get("thesis") or "").strip()
    ).strip()[:1200]
    if not thesis:
        thesis = str(carried.get("thesis") or "").strip()[:1200]
    out: dict[str, Any] = {
        "name": name[:120],
        "thesis": thesis,
        "when_on": str(raw.get("when_on") or "").strip()[:800],
        "scan": scan_s,
        "shape": str(raw.get("shape") or raw.get("ticket_shape") or "").strip()[:800],
        "invalidation": str(raw.get("invalidation") or "").strip()[:800],
        "status": status,
        "note": str(raw.get("note") or raw.get("notes") or "").strip()[:800],
    }
    if evidence:
        out["evidence"] = evidence
    expect = _norm_expect_hit_rate(raw.get("expect_hit_rate"))
    if expect is None:
        expect = _norm_expect_hit_rate(carried.get("expect_hit_rate"))
    if expect is not None:
        out["expect_hit_rate"] = expect
    retire = _norm_retire_if(raw.get("retire_if"))
    if not retire:
        retire = _norm_retire_if(carried.get("retire_if"))
    if retire:
        out["retire_if"] = retire
    return out


def unknown_card_tickets(cards: Any) -> list[str]:
    """Loose-card ``ticket`` values that are not sendable ORDER_EXAMPLES keys.

    Only reachable from the flat write shape. A nested card cannot have an
    unknown ticket â€” its parent key is validated as a type.
    """
    if not isinstance(cards, list):
        return []
    allowed = set(playbook_type_keys())
    bad: list[str] = []
    for raw in cards:
        if _norm_card(raw) is None:
            continue
        ticket = card_ticket_of(raw)
        if ticket and ticket not in allowed and ticket not in bad:
            bad.append(ticket)
    return bad


def conflicting_card_tickets(types: Any) -> list[str]:
    """Nested cards whose ``ticket`` disagrees with the type they sit under.

    Position decides the ticket. A card that says otherwise is a write the
    clerk refuses rather than silently re-filing.
    """
    if not isinstance(types, dict):
        return []
    bad: list[str] = []
    for name, row in types.items():
        if not isinstance(row, dict):
            continue
        for raw in row.get("cards") or []:
            ticket = card_ticket_of(raw)
            if not ticket or ticket == str(name).strip().lower():
                continue
            label = f"{str((raw or {}).get('name') or '?')[:60]}: {ticket} under {name}"
            if label not in bad:
                bad.append(label)
    return bad


def untyped_card_names(cards: Any) -> list[str]:
    """Loose cards with no ticket at all. They have no home in the tree."""
    if not isinstance(cards, list):
        return []
    out: list[str] = []
    for raw in cards:
        row = _norm_card(raw)
        if row is None or card_ticket_of(raw):
            continue
        if row["name"] not in out:
            out.append(row["name"])
    return out


def _norm_cards(
    raw: Any,
    *,
    cap: int = _MAX_CARDS,
    prev: Any = None,
) -> list[dict[str, Any]]:
    """Normalize one card list. Last write of a name wins within the list.

    ``prev`` is the branch list already on the tree. A card that reappears is
    merged against its stored self so declarations survive a partial rewrite;
    a card left out of the list is still dropped, so retiring one still works.
    """
    if not isinstance(raw, list):
        return []
    carried: dict[str, dict[str, Any]] = {}
    for row in prev or []:
        if isinstance(row, dict) and row.get("name"):
            carried[str(row["name"]).strip().lower()] = row
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        row = _norm_card(item, prev=carried.get(_incoming_card_name(item).lower()))
        if not row:
            continue
        key = row["name"].lower()
        if key in seen:
            out = [r for r in out if r["name"].lower() != key]
        else:
            seen.add(key)
        out.append(row)
        if len(out) >= cap:
            break
    return out


def _retire_if_line(retire: Any) -> str:
    row = retire if isinstance(retire, dict) else {}
    bits: list[str] = []
    if row.get("sample"):
        bits.append(f"sample={row['sample']}")
    if row.get("max_loss_usd"):
        bits.append(f"max_loss_usd={row['max_loss_usd']}")
    if row.get("max_losses"):
        bits.append(f"max_losses={row['max_losses']}")
    if row.get("condition"):
        bits.append(str(row["condition"]))
    return " ".join(bits)


def render_cards(cards: list[dict[str, Any]] | None, *, indent: str = "") -> str:
    """Readable card stanzas. Indented, these are the branches of a trunk."""
    rows = [c for c in (cards or []) if isinstance(c, dict) and c.get("name")]
    if not rows:
        return ""
    lines: list[str] = []
    for card in rows:
        head = f"{indent}CARD {card.get('name')}"
        status = str(card.get("status") or "").strip()
        if status:
            head += f"  [{status}]"
        lines.append(head)
        for key in ("thesis", "when_on", "scan", "shape", "invalidation", "note"):
            val = str(card.get(key) or "").strip()
            if val:
                lines.append(f"{indent}  {key}: {val}")
        evidence = card.get("evidence") if isinstance(card.get("evidence"), dict) else {}
        for key in EVIDENCE_FIELDS:
            if key == "scan":
                continue
            val = str(evidence.get(key) or "").strip()
            if val:
                lines.append(f"{indent}  evidence.{key}: {val}")
        retire = _retire_if_line(card.get("retire_if"))
        lines.append(
            f"{indent}  retire_if: {retire}"
            if retire
            else f"{indent}  retire_if: NOT DECLARED"
        )
    return "\n".join(lines)


def render_playbook_tree(state: dict[str, Any] | None) -> str:
    """The tree: each trunk's learnings, then the cards branching under it.

    Accepts the whole book or just its ``types`` map, so older callers that
    passed the type layer alone still render.
    """
    blob = state if isinstance(state, dict) else {}
    types = blob.get("types") if isinstance(blob.get("types"), dict) else blob
    if not isinstance(types, dict) or not types:
        types = {}
    lines: list[str] = []
    for name in playbook_type_keys():
        row = types.get(name)
        if not isinstance(row, dict):
            continue
        stanza = _norm_type_row(row, prev={})
        if not stanza:
            continue
        lines.append(f"TYPE {name}")
        order = stanza.get("tool_order")
        if order:
            lines.append(f"  tool_order: {', '.join(str(x) for x in order)}")
        for key in ("gotchas", "review", "note"):
            val = str(stanza.get(key) or "").strip()
            if val:
                lines.append(f"  {key}: {val}")
        branch = render_cards(stanza.get("cards"), indent="  ")
        if branch:
            lines.append(branch)
    unfiled = blob.get(UNFILED_KEY) if isinstance(blob, dict) else None
    if isinstance(unfiled, list) and unfiled:
        lines.append("UNFILED (needs a parent type before it can send)")
        lines.append(render_cards(unfiled, indent="  "))
    return "\n".join(lines)


def notebook_text(state: dict[str, Any] | None) -> str:
    """The tree. Prose instructions are the fallback when nothing is written."""
    blob = state if isinstance(state, dict) else {}
    tree = render_playbook_tree(blob)
    if tree:
        return tree
    return str(blob.get("instructions") or "").strip()


def _has_book(state: dict[str, Any] | None) -> bool:
    return bool(notebook_text(state))


def _unknown_type_keys(blob: dict[str, Any]) -> list[str]:
    allowed = set(playbook_type_keys())
    return sorted(
        k for k in blob
        if k not in allowed and k not in _TYPE_META_KEYS
    )


def _parse_structured_text(text: str) -> dict[str, Any] | None:
    """TYPE stanzas written as text. Learned fields only."""
    types: dict[str, Any] = {}
    cur_type: str | None = None
    found_type = False
    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        m = _TYPE_HDR.match(s)
        if m:
            cur_type = m.group(1).strip().rstrip(":").strip()
            types.setdefault(cur_type, {})
            found_type = True
            continue
        m = _FIELD_LINE.match(s)
        if not m or not cur_type:
            continue
        field = m.group(1).lower()
        val = m.group(2).strip()
        if field == "default_tool_recipe":
            field = "tool_order"
        if field == "tool_order":
            types[cur_type][field] = _norm_recipe(val)
        else:
            types[cur_type][field] = val
    if not found_type:
        return None
    return types


def _coerce_types_blob(blob: Any) -> dict[str, Any] | None:
    if blob is True:
        return {}
    if isinstance(blob, str):
        text = blob.strip()
        if not text:
            return None
        if text.lower() in ("catalog", "type catalog", "types"):
            return {}
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                inner = parsed.get("types")
                if isinstance(inner, dict):
                    return inner
                if (
                    parsed == {}
                    or any(k in playbook_type_keys() for k in parsed)
                    or _looks_like_type_map(parsed)
                ):
                    return parsed
                return None
        return _parse_structured_text(text)
    if isinstance(blob, dict):
        inner = blob.get("types")
        if isinstance(inner, dict) and (
            inner == {}
            or any(k in playbook_type_keys() for k in inner)
            or _looks_like_type_map(inner)
        ):
            return inner
        if any(k in playbook_type_keys() for k in blob) or blob == {} or _looks_like_type_map(blob):
            return blob
    return None


def _looks_like_type_map(blob: dict[str, Any]) -> bool:
    if not blob:
        return False
    stanza = (
        set(TYPE_LEARNED_FIELDS)
        | set(_TYPE_SCHEMA_ECHO)
        | {"default_tool_recipe", "cards"}
    )
    values = list(blob.values())
    if not all(isinstance(v, dict) for v in values):
        return False
    return any(stanza & set(v) for v in values)


def _extract_types(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Incoming types dict, or None if omitted. Error is unknown_type / unstructured."""
    for key in ("types", "catalog"):
        if key not in raw:
            continue
        blob = raw.get(key)
        if blob is None or blob == "":
            continue
        parsed = _coerce_types_blob(blob)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    inst = raw.get("instructions")
    if isinstance(inst, dict):
        parsed = _coerce_types_blob(inst)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    if isinstance(inst, str) and inst.strip():
        parsed = _coerce_types_blob(inst)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    return None, ""


def book_shape_rejects(raw: Any) -> dict[str, str]:
    """Reject only writes the tree cannot hold. Prose observations always save.

    Three ways a card has no place: it claims a ticket the clerk cannot send, it
    claims none at all, or it sits under one type while claiming another. The
    last one is the reason this is a reject and not a silent re-filing â€” position
    and ticket disagreeing is exactly the ambiguity nesting removes. Everything
    else — notes, regime reads, per-name observations — saves. The notebook is
    never a clock: ``set_wake`` parks, and ``format_block`` never paints notes as
    orders.
    """
    if not isinstance(raw, dict):
        return {}
    if "cards" in raw:
        bad = unknown_card_tickets(raw.get("cards"))
        if bad:
            return {
                "unknown_type": f"card ticket must be a sendable type ({', '.join(bad)})"
            }
        orphans = untyped_card_names(raw.get("cards"))
        if orphans:
            return {
                "unknown_type": (
                    "a card lives under an order type — nest it in "
                    f"types[<type>].cards or give it a ticket ({', '.join(orphans)})"
                )
            }
    incoming, err = _extract_types(raw)
    if isinstance(incoming, dict):
        clash = conflicting_card_tickets(incoming)
        if clash:
            return {
                "unknown_type": (
                    "a card's ticket must match the type it sits under "
                    f"({'; '.join(clash)})"
                )
            }
    if err == "unknown_type":
        blob = raw.get("types") if "types" in raw else raw.get("catalog")
        parsed = blob if isinstance(blob, dict) else _coerce_types_blob(blob)
        bad_types = _unknown_type_keys(parsed) if isinstance(parsed, dict) else []
        label = ", ".join(bad_types) if bad_types else "unknown"
        return {"unknown_type": f"do not add unknown types ({label})"}
    return {}


def _merge_type_catalog(
    prev: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any] | None:
    """Patch the learned layer. The clerk contributes nothing of its own.

    The old version stamped every trunk's ``open_shape`` / ``close_tp_sl`` from
    ORDER_EXAMPLES on every write, so the notebook grew a verbatim copy of the
    ticket schema Grok already has. Now an untouched type keeps exactly what
    Grok last wrote â€” learnings *and* the cards branching under it â€” and a type
    with nothing learned and nothing to test is simply absent.
    """
    allowed_set = set(playbook_type_keys())
    if _unknown_type_keys(incoming):
        return None
    out = _clean_types(prev.get("types"))
    for name, row in (incoming or {}).items():
        if name not in allowed_set:
            continue
        merged = _norm_type_row(row, prev=out.get(name))
        if merged:
            out[name] = merged
        else:
            out.pop(name, None)
    return out


def _cards_update(raw: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any] | None:
    """Incoming loose ``cards`` list refiled under the tree, or None if absent.

    The flat list is the legacy write shape and still means *replace the whole
    card set*, so it is applied to a tree stripped of its branches. Nested
    ``types[*].cards`` then wins for any trunk that declared one.
    """
    if "cards" not in raw:
        return None
    bare = {
        name: {k: v for k, v in row.items() if k != "cards"}
        for name, row in _clean_types(prev.get("types")).items()
        if isinstance(row, dict)
    }
    tree, _unfiled = _file_cards_into_tree(bare, list(raw.get("cards") or []))
    return tree


def gate_rejects(raw: Any) -> dict[str, str]:
    """Reject floors / live / sleeve knobs on a notebook write. Notes stay notes."""
    if not isinstance(raw, dict):
        return {}
    rejected: dict[str, str] = {}
    for key, reason in _GATE_FORBIDDEN.items():
        if key in raw:
            rejected[key] = reason
    # Nested self_tune-shaped blobs are not notebook fields either.
    for nest in ("risk", "universe"):
        blob = raw.get(nest)
        if nest in raw and isinstance(blob, dict):
            rejected[nest] = "knobs are self_tune â€” notebook cannot retune"
            for key, reason in _GATE_FORBIDDEN.items():
                if key in blob:
                    rejected[key] = reason
    inst = str(raw.get("instructions") or "")
    types_text = _walk_text(raw.get("types")) if isinstance(raw.get("types"), dict) else ""
    catalog_text = _walk_text(raw.get("catalog")) if isinstance(raw.get("catalog"), dict) else ""
    if (
        (inst and _has_invented_pct_gate(inst))
        or (types_text and _has_invented_pct_gate(types_text))
        or (catalog_text and _has_invented_pct_gate(catalog_text))
    ):
        rejected["invented_pct_gate"] = "notebook cannot invent a % gate"
    return rejected


def clamp_update(raw: Any) -> dict[str, Any] | None:
    """Full rewrite or patch. Omitted fields keep the previous lab text.

    Gate knobs (floors / live / sleeve) are never stored â€” see gate_rejects.
    Invented GATES: N% / floor N% NL lines are stripped unless floors are ON
    and N equals the live max_risk_per_trade_pct knob.
    The book is one TYPE tree (sendable keys) with cards branching under it,
    not a diary.
    """
    if not isinstance(raw, dict):
        return None
    if not any(k in raw for k in _PATCH_KEYS):
        return None
    if _HARD_SHAPE.intersection(book_shape_rejects(raw)):
        return None
    prev = load_lab()

    # A loose cards[] list replaces the whole card set; nested types[*].cards
    # then wins for any trunk that named one.
    base = _cards_update(raw, prev)
    cards_given = base is not None
    staged = dict(prev)
    if base is not None:
        staged["types"] = base

    incoming, err = _extract_types(raw)
    types_given = incoming is not None
    types: dict[str, Any] = {}
    if incoming is not None:
        merged = _merge_type_catalog(staged, incoming)
        types = merged if merged is not None else {}
    else:
        types = _clean_types(staged.get("types"))
    _ = err

    if "instructions" in raw:
        instructions = _strip_invented_pct_gate_lines(
            str(raw.get("instructions") or "")
        )
    elif types:
        instructions = _strip_invented_pct_gate_lines(notebook_text({"types": types}))
    else:
        instructions = str(prev.get("instructions") or "")
    instructions = instructions.strip()[:_MAX_INSTRUCTIONS]

    if not instructions and not types:
        return None
    mode = _field(raw, prev, "mode", "explore").strip().lower()
    if mode not in ("explore", "exploit"):
        mode = "explore"
    ready = raw["ready_to_promote"] if "ready_to_promote" in raw else prev.get("ready_to_promote")
    out: dict[str, Any] = {
        "mode": mode,
        "instructions": instructions,
        "ready_to_promote": bool(ready),
    }
    if types or types_given or cards_given:
        # Writing an empty stanza is how Grok drops a type it no longer trusts.
        out["types"] = types
    return out


def grounding_error(
    raw: dict[str, Any] | None,
    *,
    tool_trace: list[str] | None = None,
) -> str:
    """Shape only. The notebook is Grok's; clerk does not demand a constitution."""
    if not isinstance(raw, dict):
        return "write_lab_playbook needs a notebook object"
    return ""


def _card_log_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_CARD_LOG_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _REPO_ROOT / "data" / "state" / "card_sends.jsonl"


def _send_is_new_risk(strategy: str, params: dict[str, Any] | None) -> bool:
    """Reuse the clerk's own predicate so sample counting matches the gate."""
    try:
        from abcxauto.agent_loop import is_new_risk

        return bool(is_new_risk(strategy, params))
    except Exception:
        return False


def card_types_by_name(state: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """``name`` -> the types it branches under. More than one is ambiguous."""
    blob = state if isinstance(state, dict) else load_lab()
    out: dict[str, list[str]] = {}
    for type_name, card in walk_cards(blob):
        key = str(card.get("name") or "").strip().lower()
        if not key:
            continue
        seen = out.setdefault(key, [])
        if type_name and type_name not in seen:
            seen.append(type_name)
    return out


def resolve_card_type(
    card: Any,
    *,
    strategy: str = "",
    state: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Which type a card send belongs to. Returns ``(type, ambiguous)``.

    A new-risk send is unambiguous by construction: the gate already proved the
    card lives under the strategy being sent. Anything else â€” a management
    ticket, or a row written before cards were nested â€” is resolved by name,
    and only when exactly one type claims that name.
    """
    name = str(card or "").strip().lower()
    if not name:
        return "", False
    index = card_types_by_name(state)
    types = index.get(name) or []
    want = str(strategy or "").strip().lower()
    if want and want in types:
        return want, False
    if len(types) == 1:
        return types[0], False
    if len(types) > 1:
        return "", True
    return "", False


def record_card_send(
    *,
    card: str,
    strategy: str,
    symbol: str = "",
    result: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Tie a dispatched ticket to the card that called for it.

    Without this the only feedback a card gets is whole-book drift, which is
    the same number for every card. ``new_risk`` separates the entry that opens
    a trade from the management tickets that follow, so a card's declared sample
    counts trades and not keystrokes. ``type`` pins the row to one branch of the
    tree, because a name alone is no longer an identity.
    """
    name = str(card or "").strip()
    if not name:
        return
    from abcxauto.memory.journal import _order_ids_from_result_json

    try:
        oids = sorted(_order_ids_from_result_json(json.dumps(result or {}, default=str)))
    except Exception:
        oids = []
    try:
        card_type, _ambiguous = resolve_card_type(name, strategy=strategy)
    except Exception:
        card_type = ""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "card": name[:120],
        "type": card_type[:60],
        "strategy": str(strategy or "")[:60],
        "symbol": str(symbol or "").upper()[:12],
        "order_ids": oids[:12],
        "new_risk": _send_is_new_risk(strategy, params),
    }
    path = _card_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        logger.debug("card send log write failed", exc_info=True)


def _card_sends(limit: int = 400) -> list[dict[str, Any]]:
    path = _card_log_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                blob = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(blob, dict) and blob.get("card"):
                rows.append(blob)
    except OSError:
        return []
    return rows


def resolve_send_types(
    sends: list[dict[str, Any]] | None,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stamp every send row with its ``(type, name)`` identity.

    Rows written before cards were nested carry only a name. One resolves when
    exactly one type claims that name, or when the row's own strategy is a type
    holding it. When two types hold the name the row is marked ``ambiguous`` and
    attributed to neither: crediting a trade to a card that never asked for it
    is worse than leaving it visibly unattributed.
    """
    index = card_types_by_name(state)
    out: list[dict[str, Any]] = []
    for row in sends or []:
        if not isinstance(row, dict) or not row.get("card"):
            continue
        item = dict(row)
        have = str(item.get("type") or "").strip().lower()
        if have:
            item["type"] = have
            item["ambiguous"] = False
            out.append(item)
            continue
        name = str(item.get("card") or "").strip().lower()
        types = index.get(name) or []
        want = str(item.get("strategy") or "").strip().lower()
        if want and want in types:
            item["type"], item["ambiguous"] = want, False
        elif len(types) == 1:
            item["type"], item["ambiguous"] = types[0], False
        else:
            item["type"], item["ambiguous"] = "", len(types) > 1
        out.append(item)
    return out


def _send_oids(row: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for oid in row.get("order_ids") or []:
        try:
            out.append(int(oid))
        except (TypeError, ValueError):
            continue
    return out


def _ts_num(raw: Any) -> float:
    """Epoch seconds from an ISO stamp. Fills say ``Z``, card sends say ``+00:00``."""
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def classify_card_trades(
    sends: list[dict[str, Any]] | None,
    fills: list[dict[str, Any]] | None,
    dispatched: set | None = None,
) -> list[dict[str, Any]]:
    """One row per new-risk card send: how that trade actually ended.

    ``protective`` â€” the card's own stop/target filled.
    ``decision``   â€” a dispatched ticket closed it (Grok's call).
    ``operator``   â€” the exit has no dispatch behind it: a manual TWS flatten,
                     another client session, or the panic/halt path. The card's
                     thesis was interrupted, so it is neither proof nor
                     falsification and never counts toward the declared sample.
    ``open``       â€” nothing has closed it yet.
    """
    rows = [r for r in (sends or []) if isinstance(r, dict) and r.get("card")]
    rows.sort(key=lambda r: _ts_num(r.get("ts")))
    placed = set(dispatched or set())
    # Which send produced each order id, so a fill can be traced to its ticket.
    owner: dict[int, dict[str, Any]] = {}
    for row in rows:
        for oid in _send_oids(row):
            owner.setdefault(oid, row)
    closers = [
        f
        for f in (fills or [])
        if isinstance(f, dict) and f.get("order_id") is not None
    ]
    closers.sort(key=lambda f: _ts_num(f.get("ts")))
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("new_risk") is False:
            continue
        if row.get("new_risk") is None and not _send_is_new_risk(
            str(row.get("strategy") or ""), None
        ):
            continue
        sym = str(row.get("symbol") or "").upper()
        ts = str(row.get("ts") or "")
        opened = _ts_num(ts)
        own = set(_send_oids(row))
        trade: dict[str, Any] = {
            "card": str(row.get("card") or ""),
            "type": str(row.get("type") or ""),
            "ambiguous": bool(row.get("ambiguous")),
            "symbol": sym,
            "strategy": str(row.get("strategy") or ""),
            "opened_at": ts,
            "exit": EXIT_OPEN,
            "realized_pnl": None,
            "exit_order_id": None,
            "exit_at": None,
        }
        for idx, fill in enumerate(closers):
            if idx in used:
                continue
            if sym and str(fill.get("symbol") or "").upper() != sym:
                continue
            if opened and _ts_num(fill.get("ts")) < opened:
                continue
            oid = int(fill["order_id"])
            src = owner.get(oid)
            if oid in own:
                kind = EXIT_PROTECTIVE
            elif src is not None or oid in placed:
                kind = EXIT_DECISION
            else:
                kind = EXIT_OPERATOR
            used.add(idx)
            trade.update(
                exit=kind,
                realized_pnl=round(float(fill.get("realized_pnl") or 0.0), 4),
                exit_order_id=oid,
                exit_at=fill.get("ts"),
            )
            break
        out.append(trade)
    return out


def _journal_exit_facts() -> tuple[list[dict[str, Any]], set]:
    """Closing fills plus the order ids the clerk dispatched. Degrades to empty."""
    fills: list[dict[str, Any]] = []
    placed: set = set()
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "closing_fills", None)
        if callable(fn):
            fills = [f for f in (fn() or []) if isinstance(f, dict)]
        fn = getattr(journal, "dispatched_order_ids", None)
        if callable(fn):
            placed = set(fn() or set())
    except Exception:
        logger.debug("journal exit facts unavailable", exc_info=True)
    return fills, placed


def _empty_score(card: str = "", card_type: str = "") -> dict[str, Any]:
    return {
        "card": card,
        "type": card_type,
        "sends": 0,
        "realized_pnl": 0.0,
        "attributed_fills": 0,
        "last_send": None,
        "symbols": [],
        "trades": 0,
        "resolved": 0,
        "interrupted": 0,
        "open": 0,
        "resolved_pnl": 0.0,
        "interrupted_pnl": 0.0,
        "resolved_wins": 0,
        "resolved_losses": 0,
        "ambiguous_sends": 0,
        "exits": {k: 0 for k in CARD_EXIT_KINDS},
    }


def card_scores(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Per-card attribution: what it sent, what resolved, what interrupted it.

    Buckets are keyed by ``(type, name)``, so the same setup name under two
    order types scores as the two different experiments it is. ``realized_pnl``
    is every dollar the card's own order ids booked â€” the book number has to
    reconcile. ``resolved_pnl`` is the graduation number: only trades whose exit
    was the card's own protection or a dispatched decision.
    """
    raw_sends = _card_sends()
    if not raw_sends:
        return []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for c in cards or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        name = str(c["name"]).strip().lower()
        by_key[card_key(c.get("type") or c.get("ticket"), name)] = c
        by_name.setdefault(name, []).append(c)
    state: dict[str, Any] | None = None
    if cards is not None:
        # Resolve legacy name-only rows against the book we were handed, not
        # whatever happens to be on disk.
        tree: dict[str, Any] = {}
        for c in cards:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            parent = str(c.get("type") or c.get("ticket") or "").strip().lower()
            if parent:
                tree.setdefault(parent, {}).setdefault("cards", []).append(c)
        state = {"types": tree}
    sends = resolve_send_types(raw_sends, state)
    realized: dict[int, float] = {}
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "realized_by_order_id", None)
        if callable(fn):
            realized = dict(fn() or {})
    except Exception:
        realized = {}
    fills, placed = _journal_exit_facts()
    trades = classify_card_trades(sends, fills, placed)
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def _bucket(card_type: Any, name: str) -> dict[str, Any]:
        key = card_key(card_type, name)
        return buckets.setdefault(key, _empty_score(name, key[0]))

    for row in sends:
        name = str(row.get("card") or "")
        bucket = _bucket(row.get("type"), name)
        bucket["sends"] += 1
        if row.get("ambiguous"):
            bucket["ambiguous_sends"] += 1
        bucket["last_send"] = row.get("ts") or bucket["last_send"]
        sym = str(row.get("symbol") or "")
        if sym and sym not in bucket["symbols"]:
            bucket["symbols"].append(sym)
        for oid in _send_oids(row):
            if oid in realized:
                bucket["realized_pnl"] += float(realized[oid])
                bucket["attributed_fills"] += 1
    for trade in trades:
        bucket = _bucket(trade.get("type"), str(trade.get("card") or ""))
        kind = str(trade.get("exit") or EXIT_OPEN)
        bucket["trades"] += 1
        bucket["exits"][kind] = bucket["exits"].get(kind, 0) + 1
        pnl = trade.get("realized_pnl")
        pnl_f = float(pnl) if isinstance(pnl, (int, float)) else 0.0
        if kind in RESOLVED_EXITS:
            bucket["resolved"] += 1
            bucket["resolved_pnl"] += pnl_f
            if pnl_f < 0:
                bucket["resolved_losses"] += 1
            elif pnl_f > 0:
                bucket["resolved_wins"] += 1
        elif kind == EXIT_OPERATOR:
            bucket["interrupted"] += 1
            bucket["interrupted_pnl"] += pnl_f
        else:
            bucket["open"] += 1
    out: list[dict[str, Any]] = []
    for (card_type, name), bucket in buckets.items():
        for key in ("realized_pnl", "resolved_pnl", "interrupted_pnl"):
            bucket[key] = round(float(bucket[key]), 4)
        bucket["symbols"] = bucket["symbols"][:8]
        match = by_key.get((card_type, name))
        if match is None:
            same = by_name.get(name) or []
            match = same[0] if len(same) == 1 else None
        if by_key:
            bucket["on_current_book"] = match is not None
        bucket.update(card_verdict(bucket, match))
        out.append(bucket)
    return sorted(
        out,
        key=lambda b: (-int(b.get("sends") or 0), str(b.get("card")), str(b.get("type"))),
    )


def card_calibration(
    score: dict[str, Any] | None,
    card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The hit rate a card claimed against the one it actually produced.

    Report only. A card that claimed 70 and returned 40 is not tripped and its
    graduation is untouched — the gap is the fact worth reading, because
    positive resolved edge on a hit rate far under the claim is one fat winner
    rather than a repeatable setup. Thin samples say so instead of printing a
    number that cannot mean anything yet.
    """
    sc = score if isinstance(score, dict) else {}
    row = card if isinstance(card, dict) else {}
    declared = _norm_expect_hit_rate(row.get("expect_hit_rate"))
    resolved = int(sc.get("resolved") or 0)
    wins = int(sc.get("resolved_wins") or 0)
    out: dict[str, Any] = {
        "expect_hit_rate": declared,
        "resolved": resolved,
        "resolved_wins": wins,
        "hit_rate": None,
        "hit_rate_gap": None,
        "note": "",
    }
    if resolved < _CALIBRATION_MIN_N:
        out["note"] = f"thin resolved sample ({resolved} of {_CALIBRATION_MIN_N})"
        return out
    hit = round(100.0 * wins / resolved, 1)
    out["hit_rate"] = hit
    if declared is None:
        out["note"] = "no expect_hit_rate declared"
    else:
        out["hit_rate_gap"] = round(hit - declared, 1)
    return out


def card_verdict(
    score: dict[str, Any] | None,
    card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Graduated / tripped strictly from the card's own declaration.

    The clerk supplies no thresholds. A card that declared nothing can neither
    graduate nor trip â€” it is flagged as owing a declaration on the next write.
    ``calibration`` rides along as a fact and never moves either verdict: a
    miscalibrated card with positive resolved edge still graduates.
    """
    sc = score if isinstance(score, dict) else {}
    row = card if isinstance(card, dict) else {}
    retire = row.get("retire_if") if isinstance(row.get("retire_if"), dict) else {}
    status = str(row.get("status") or "").strip().lower()
    thesis = bool(str(row.get("thesis") or "").strip())
    try:
        sample = int(retire.get("sample") or 0)
    except (TypeError, ValueError):
        sample = 0
    resolved = int(sc.get("resolved") or 0)
    resolved_pnl = float(sc.get("resolved_pnl") or 0.0)
    losses = int(sc.get("resolved_losses") or 0)
    anchored = str(row.get("type") or row.get("ticket") or sc.get("type") or "")
    out: dict[str, Any] = {
        "anchored_type": anchored or None,
        "status": status or None,
        "retire_if": dict(retire) or None,
        "declared_sample": sample or None,
        "needs_retire_if": bool(row) and not retire,
        "needs_thesis": bool(row) and not thesis,
        "sample_left": max(0, sample - resolved) if sample else None,
        "calibration": card_calibration(sc, row),
        "graduated": False,
        "tripped": False,
        "trip_reason": "",
    }
    if status == "retired":
        return out
    max_loss = retire.get("max_loss_usd")
    max_losses = retire.get("max_losses")
    if sample and resolved >= sample:
        if resolved_pnl > 0:
            # A card with no written thesis has nothing for live to follow.
            out["graduated"] = bool(thesis)
        else:
            out["tripped"] = True
            out["trip_reason"] = (
                f"declared sample {sample} reached with resolved edge "
                f"{resolved_pnl:+.2f} — retire or rewrite"
            )
            return out
    if isinstance(max_loss, (int, float)) and max_loss > 0 and resolved_pnl <= -float(max_loss):
        out["tripped"] = True
        out["graduated"] = False
        out["trip_reason"] = (
            f"resolved edge {resolved_pnl:+.2f} hit declared max_loss_usd {max_loss}"
        )
        return out
    if isinstance(max_losses, int) and max_losses > 0 and losses >= max_losses:
        out["tripped"] = True
        out["graduated"] = False
        out["trip_reason"] = (
            f"{losses} losing resolved trades at declared max_losses {max_losses}"
        )
    return out


def card_facts(book: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every ``(type, card)`` on the tree with its attribution, sent or not."""
    state = book if isinstance(book, dict) else load_lab()
    pairs = walk_cards(state)
    flat = _flat_card_projection(state)
    scored = {card_key(r.get("type"), r.get("card")): r for r in card_scores(flat)}
    unresolved = {
        str(r.get("card") or "").lower(): r
        for r in scored.values()
        if not r.get("type") and int(r.get("ambiguous_sends") or 0) > 0
    }
    out: list[dict[str, Any]] = []
    for type_name, card in pairs:
        row = dict(scored.get(card_key(type_name, card.get("name"))) or {})
        if not row:
            row = _empty_score(str(card.get("name") or ""), type_name)
            row["on_current_book"] = True
        stamped = dict(card)
        stamped["type"] = type_name
        row.update(card_verdict(row, stamped))
        stray = unresolved.get(str(card.get("name") or "").lower())
        if stray is not None:
            # Same name under two types: those sends belong to neither.
            row["ambiguous_sends"] = int(stray.get("ambiguous_sends") or 0)
        out.append(row)
    return out


def graduated_card_names(book: dict[str, Any] | None = None) -> list[str]:
    """Cards that met their own declared sample with positive resolved edge."""
    state = book if isinstance(book, dict) else load_lab()
    declared = state.get("graduated")
    if isinstance(declared, list) and declared:
        return [str(x) for x in declared if str(x).strip()]
    return [
        str(row.get("card"))
        for row in card_facts(state)
        if row.get("graduated")
    ]


def _card_label(row: dict[str, Any] | None) -> str:
    """``name [type]`` — the readable form of a card's identity."""
    blob = row if isinstance(row, dict) else {}
    name = str(blob.get("card") or blob.get("name") or "")
    parent = str(blob.get("type") or blob.get("ticket") or "")
    return f"{name} [{parent}]" if parent else name


def _card_names_blob(names: list[str]) -> str:
    return " | ".join(names) if names else "none — write_lab_playbook first"


def _cards_under_blob(pairs: list[tuple[str, dict[str, Any]]], type_name: str) -> str:
    """Live card names under one trunk, else where the cards actually are."""
    here = [
        str(c.get("name"))
        for t, c in pairs
        if t == type_name and c.get("status") != "retired"
    ]
    if here:
        return " | ".join(here)
    elsewhere = [
        f"{c.get('name')} [{t or 'unfiled'}]"
        for t, c in pairs
        if c.get("status") != "retired"
    ]
    if elsewhere:
        return f"none (elsewhere: {' | '.join(elsewhere[:8])})"
    return "none — write_lab_playbook first"


def new_risk_card_error(
    card: Any,
    *,
    type: str = "",
    book: dict[str, Any] | None = None,
) -> str:
    """Clerk gate on new risk. Empty string means this ticket may go.

    A card's identity is ``(type, name)``, so the ticket being sent has to be
    the trunk the card branches from. ``type`` is the strategy on the send;
    empty means "look the name up anywhere", which is what an ad-hoc caller
    without a ticket gets.

    Exits, protection, closes, modifies and cancels never reach here â€”
    ``is_new_risk`` is False for them, and that invariant is what keeps a
    tripped or retired card manageable instead of stranded.
    """
    paper = is_paper()
    state = book if isinstance(book, dict) else (load_lab() if paper else load_live())
    pairs = walk_cards(state)
    sending = str(type or "").strip().lower()
    want = str(card or "").strip()
    if not want:
        if sending:
            return (
                f"new risk requires params.card naming a card under TYPE {sending}; "
                f"cards under {sending}: {_cards_under_blob(pairs, sending)}"
            )
        return (
            "new risk requires params.card naming a playbook card; "
            f"cards: {_card_names_blob([str(c.get('name')) for _t, c in pairs])}"
        )
    hits = [(t, c) for t, c in pairs if str(c.get("name") or "").lower() == want.lower()]
    if not hits:
        return (
            f"new risk card {want!r} is not on the playbook; "
            f"cards under {sending or 'any type'}: {_cards_under_blob(pairs, sending)}"
        )
    if sending:
        under = [(t, c) for t, c in hits if t == sending]
        if not under:
            homes = ", ".join(sorted({t or "unfiled" for t, _c in hits}))
            return (
                f"card {want!r} lives under TYPE {homes}, not {sending} — send it "
                f"as {homes} or write it under {sending} "
                f"(cards under {sending}: {_cards_under_blob(pairs, sending)})"
            )
        hits = under
    elif len(hits) > 1:
        homes = ", ".join(sorted({t or "unfiled" for t, _c in hits}))
        return (
            f"card {want!r} exists under TYPE {homes} — name the type by sending "
            "that ticket so the trade is attributed to one card"
        )
    card_type, match = hits[0]
    label = f"{match.get('name')!r} under TYPE {card_type or 'unfiled'}"
    if not card_type:
        return (
            f"card {label} has no parent order type — nest it under the type it "
            "sends before taking risk on it"
        )
    if match.get("status") == "retired":
        return (
            f"card {label} is retired — no new risk on it (exits are never "
            f"blocked); cards under {card_type}: {_cards_under_blob(pairs, card_type)}"
        )
    key = card_key(card_type, match.get("name"))
    facts = next(
        (r for r in card_facts(state) if card_key(r.get("type"), r.get("card")) == key),
        {},
    )
    if facts.get("tripped"):
        return (
            f"card {label} tripped its declared retire_if "
            f"({facts.get('trip_reason')}) — rewrite or retire it "
            "(exits are never blocked)"
        )
    if not paper:
        grads = graduated_card_names(state)
        if str(match.get("name")) not in grads:
            return (
                f"live new risk must cite a graduated card; "
                f"graduated: {_card_names_blob(grads)}"
            )
    return ""


def strategy_scores() -> list[dict[str, Any]]:
    """Realized P&L per sendable strategy, from the journal's fills join."""
    try:
        from abcxauto.memory import get_journal

        rows = get_journal().strategy_performance() or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "strategy": row.get("strategy"),
                "n_fills": row.get("n_fills"),
                "realized_pnl": round(float(row.get("realized_pnl_sum") or 0.0), 4),
                "commissions": round(float(row.get("commissions_sum") or 0.0), 4),
                "last_fill_ts": row.get("last_fill_ts"),
            }
        )
    return sorted(out, key=lambda r: float(r.get("realized_pnl") or 0.0), reverse=True)


def _score_snap(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else {}
    return {
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "model_cost_usd": sc.get("model_cost_usd"),
    }


def _outcome_card(card: dict[str, Any] | None) -> dict[str, Any]:
    """Ledger row is the score of a card, not the notes."""
    out = dict(card) if isinstance(card, dict) else {}
    out.pop("instructions", None)
    out.pop("types", None)
    out.pop("cards", None)
    for key in _DEAD_LAB_KEYS:
        out.pop(key, None)
    return out


def _ledger_card(state: dict[str, Any], scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else (
        state.get("paper_score") if isinstance(state.get("paper_score"), dict) else {}
    )
    lots = [str(x) for x in (state.get("lots_at_write") or [])][:16]
    return {
        "revision": state.get("revision"),
        "written_at": state.get("written_at"),
        "mode": state.get("mode"),
        "ready_to_promote": bool(state.get("ready_to_promote")),
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "lots_at_write": lots,
    }


def _compact_card(card: dict[str, Any] | None) -> dict[str, Any]:
    c = card if isinstance(card, dict) else {}
    return {
        "revision": c.get("revision"),
        "written_at": c.get("written_at"),
        "mode": c.get("mode"),
        "ready_to_promote": c.get("ready_to_promote"),
        "beating_model": c.get("beating_model"),
        "edge_usd": c.get("edge_usd"),
        "book_return_pct": c.get("book_return_pct"),
        "closed_edge": c.get("closed_edge"),
        "closed_beating": c.get("closed_beating"),
        "closed_at": c.get("closed_at"),
    }


def _close_card(
    card: dict[str, Any],
    scorecard: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    sc = _score_snap(scorecard)
    out = _outcome_card(card)
    out["closed_at"] = now
    out["closed_edge"] = sc.get("edge_usd")
    out["closed_beating"] = sc.get("beating_model")
    out["closed_return_pct"] = sc.get("book_return_pct")
    return out


def ensure_ledger(lab: dict[str, Any] | None) -> list[dict[str, Any]]:
    """In-memory ledger. Seed from the current blob if the file is still flat."""
    state = lab if isinstance(lab, dict) else {}
    rows = [_outcome_card(r) for r in (state.get("ledger") or []) if isinstance(r, dict)]
    if rows:
        return rows[-_LEDGER_CAP:]
    if state.get("instructions") or state.get("revision") or state.get("types"):
        return [_ledger_card(state, state.get("paper_score"))]
    return []


def revision_card(revision: int, lab: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = lab if isinstance(lab, dict) else load_lab()
    want = int(revision)
    for row in reversed(ensure_ledger(state)):
        try:
            if int(row.get("revision") or 0) == want:
                return _outcome_card(row)
        except (TypeError, ValueError):
            continue
    try:
        if int(state.get("revision") or 0) == want:
            return _ledger_card(state, state.get("paper_score"))
    except (TypeError, ValueError):
        return None
    return None


def save_lab(update: dict[str, Any], *, scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    prev = load_lab()
    now = datetime.now(timezone.utc).isoformat()
    rev = int(prev.get("revision") or 0) + 1
    ledger = ensure_ledger(prev)
    if ledger and scorecard:
        ledger[-1] = _close_card(ledger[-1], scorecard, now)
    lots_at = update.get("lots_at_write")
    if not lots_at:
        try:
            from abcxauto.think_stream import LAST_TURN_PATH, _read_json

            lots_at = list((_read_json(LAST_TURN_PATH) or {}).get("open_lots") or [])
        except Exception:
            lots_at = list(prev.get("lots_at_write") or [])
    # A caller may still hand us the flat shape, so file ``update``'s cards into
    # the tree. ``prev``'s derived list is dropped first: replaying it would
    # resurrect a card this write just deleted.
    state = _strip_projection(_migrate_book({
        **_strip_projection(prev),
        **update,
        "revision": rev,
        "written_at": now,
        "promoted": False,
        "lots_at_write": [str(x) for x in (lots_at or [])][:32],
    }))
    if scorecard:
        state["paper_score"] = _score_snap(scorecard)
    ledger.append(_ledger_card(state, state.get("paper_score")))
    state["ledger"] = ledger[-_LEDGER_CAP:]
    _write(_lab_path(), state)
    return state


def maybe_promote(*, scorecard: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Promote graduated cards, not the book.

    A card graduates on its own declared sample with positive resolved edge.
    Only those cards reach the live snapshot, and they travel inside their own
    pruned type stanza â€” one lucky ad-hoc trade no longer unlocks live for
    everything, and live never sees a hypothesis that has not earned it.
    """
    lab = load_lab()
    if not lab.get("ready_to_promote"):
        return None
    facts = card_facts(lab)
    grads = [row for row in facts if row.get("graduated")]
    if not grads:
        return None
    keys = {card_key(row.get("type"), row.get("card")) for row in grads}
    # A verdict that named no type still promotes, but only when one card on the
    # tree answers to that name.
    index = card_types_by_name(lab)
    for row in grads:
        name = str(row.get("card") or "").strip().lower()
        if not row.get("type") and len(index.get(name) or []) == 1:
            keys.add((index[name][0], name))
    types: dict[str, Any] = {}
    names: list[str] = []
    for type_name, stanza in _clean_types(lab.get("types")).items():
        keep = [
            c
            for c in (stanza.get("cards") or [])
            if card_key(type_name, c.get("name")) in keys
        ]
        if not keep:
            continue
        pruned = dict(stanza)
        pruned["cards"] = keep
        types[type_name] = pruned
        names.extend(str(c.get("name")) for c in keep)
    if not names:
        return None
    now = datetime.now(timezone.utc).isoformat()
    sc = scorecard or lab.get("paper_score") or {}
    live = {
        "promoted": True,
        "promoted_at": now,
        "promoted_revision": lab.get("revision"),
        "source": "paper_lab",
        "mode": lab.get("mode"),
        "ready_to_promote": True,
        "types": types,
        "graduated": names,
        "card_scores": grads,
        "instructions": notebook_text({"types": types}),
        "paper_score": _score_snap(sc),
        "book_beating_at_promote": promote_beating(sc),
        "note": "live follows this snapshot; does not copy paper fills",
    }
    _write(_live_path(), live)
    lab["promoted"] = True
    lab["promoted_at"] = now
    _write(_lab_path(), _strip_projection(lab))
    return live


def promote_window() -> str:
    """Scorecard window the promote gate reads. Inception never recovers."""
    import os

    raw = (os.environ.get("ABCXAUTO_PROMOTE_WINDOW") or "").strip()
    return raw or "1d"


def promote_beating(scorecard: dict[str, Any] | None) -> bool | None:
    """Beating on the promote window, falling back to the full-book flag.

    Lifetime ``beating_model`` folds in every past experiment plus the whole
    cumulative model bill, so once it is behind it stays behind. A window is a
    question the lab can actually answer.
    """
    sc = scorecard if isinstance(scorecard, dict) else {}
    wins = sc.get("windows") if isinstance(sc.get("windows"), dict) else {}
    row = wins.get(promote_window()) if isinstance(wins, dict) else None
    if isinstance(row, dict) and row.get("coverage") == "ok":
        return row.get("beating_model")
    return sc.get("beating_model")


def playbook_mode() -> str:
    """explore = widen the search, keep size small. exploit = trade the winners."""
    mode = str((load_lab() or {}).get("mode") or "explore").strip().lower()
    return mode if mode in ("explore", "exploit") else "explore"


def live_has_promoted() -> bool:
    """A promoted snapshot only counts if a graduated card is actually in it."""
    live = load_live()
    if not live.get("promoted") or not _has_book(live):
        return False
    return bool(graduated_card_names(live))


def live_new_risk_allowed() -> bool:
    """Paper may take new risk. Live needs at least one graduated card."""
    if is_paper():
        return True
    return live_has_promoted()


def _reject_note(rejected: dict[str, str]) -> str:
    if "unknown_type" in rejected:
        return rejected["unknown_type"]
    if "invented_pct_gate" in rejected and set(rejected) <= {"invented_pct_gate"}:
        return "notebook cannot invent a % gate"
    if "ticker_list" in rejected:
        return rejected["ticker_list"]
    if "diary" in rejected:
        return rejected["diary"]
    if "shape" in rejected:
        return rejected["shape"]
    if "invented_pct_gate" in rejected:
        return "notebook cannot invent a % gate"
    return "notebook cannot loosen gates"


def apply_from_judgment(judgment: dict[str, Any] | None) -> dict[str, Any] | None:
    """Paper: persist Grok's notebook. Live: ignore writes.

    Gate knobs in the payload are rejected (not applied). Notes still save.
    Diary / ticker-list / unknown-type writes do not save.
    """
    if not judgment or not is_paper():
        return None
    raw = judgment.get("lab_playbook") or judgment.get("playbook")
    rejected = dict(gate_rejects(raw))
    rejected.update(book_shape_rejects(raw))
    if _HARD_SHAPE.intersection(rejected):
        return {
            "status": "rejected",
            "rejected": rejected,
            "note": _reject_note(rejected),
        }
    update = clamp_update(raw)
    if not update:
        if rejected:
            note = "notebook cannot loosen gates"
            if "invented_pct_gate" in rejected:
                note = "notebook cannot invent a % gate"
            return {
                "status": "rejected",
                "rejected": rejected,
                "note": _reject_note(rejected),
            }
        return None
    score = None
    try:
        from abcxauto.scorecard import compute_scorecard

        score = compute_scorecard()
    except Exception:
        score = None
    state = save_lab(update, scorecard=score)
    maybe_promote(scorecard=score)
    out = dict(state)
    facts = card_facts(state)
    out["cards"] = _flat_card_projection(state)
    out["graduated_cards"] = [_card_label(r) for r in facts if r.get("graduated")]
    out["tripped_cards"] = [_card_label(r) for r in facts if r.get("tripped")]
    out["needs_declaration"] = [
        _card_label(r)
        for r in facts
        if r.get("needs_retire_if") or r.get("needs_thesis")
    ]
    if rejected:
        out["rejected"] = rejected
        if "invented_pct_gate" in rejected:
            out["note"] = "notes saved; invented % gate lines stripped"
        else:
            out["note"] = "notes saved; gate knobs ignored"
    return out


def playbook_is_stale(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a standing card is older than the lab rewrite cadence."""
    state = lab if isinstance(lab, dict) else load_lab()
    if not _has_book(state):
        return False
    age = playbook_age_hours(state, now=now)
    return age is not None and age >= stale_hours()


def _window_edges(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    wins = (scorecard or {}).get("windows") if isinstance(scorecard, dict) else None
    if not isinstance(wins, dict):
        wins = {}
    out: dict[str, Any] = {}
    for label in _CARD_WINDOWS:
        row = wins.get(label) if isinstance(wins.get(label), dict) else {}
        out[f"win_{label}"] = row.get("edge_usd")
        out[f"win_{label}_beat"] = row.get("beating_model")
    return out


def _since_write_score(
    lab: dict[str, Any],
    scorecard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Book vs model since this card was written. Inception hole stays on the scorecard."""
    written = str(lab.get("written_at") or "")
    now_nl = (scorecard or {}).get("net_liquidation") if isinstance(scorecard, dict) else None
    empty = {"since_write_edge": None, "since_write_pnl": None, "since_write_cost": None}
    if not written:
        return empty
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
    except Exception:
        return empty
    start_nl, _start_ts = None, None
    try:
        if hasattr(journal, "nav_at_or_before"):
            start_nl, _start_ts = journal.nav_at_or_before(written)
    except Exception:
        start_nl = None
    usage = {}
    try:
        if hasattr(journal, "model_usage_since"):
            usage = dict(journal.model_usage_since(written) or {})
    except Exception:
        usage = {}
    cost = float(usage.get("cost_usd") or 0.0)
    try:
        now_f = float(now_nl) if now_nl is not None else None
        start_f = float(start_nl) if start_nl is not None else None
    except (TypeError, ValueError):
        return {**empty, "since_write_cost": cost}
    if now_f is None or start_f is None or start_f <= 0:
        return {**empty, "since_write_cost": cost}
    pnl = now_f - start_f
    return {
        "since_write_edge": pnl - cost,
        "since_write_pnl": pnl,
        "since_write_cost": cost,
    }


def playbook_age_hours(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> float | None:
    raw = str((lab or {}).get("written_at") or "")
    if not raw:
        return None
    try:
        written = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return max(0.0, (clock - written).total_seconds() / 3600.0)


def playbook_glance(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score since the last write. Not the notebook text â€” Grok asks playbook() for that."""
    facts = playbook_facts(scorecard)
    return {
        "revision": facts.get("revision"),
        "age_h": facts.get("age_h"),
        "since_write_edge": facts.get("since_write_edge"),
        "now_edge": facts.get("now_edge"),
        "win_4h": facts.get("win_4h"),
        "lots_at_write": list(facts.get("lots_at_write") or [])[:16],
    }


def _MANAGEMENT_TRUNKS() -> frozenset[str]:
    """Trunks that adjust or close existing risk rather than open it."""
    from abcxauto.proposals import MANAGEMENT_STRATEGIES

    return MANAGEMENT_STRATEGIES | frozenset({"close_option"})


def lab_facts(
    book: dict[str, Any] | None = None,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What the lab has under test, and which trunks carry no hypothesis.

    The other half of ``strategy_scores``. That key reports what already traded
    and what it earned, so on its own the only breadth signal on this surface is
    the P&L of what ran — which reads as a reason to run it again. This reports
    what has never been tried.     Counts and names only: which structure to test is
    the notebook's business, not the clerk's.

    Only entry structures count as untried. ``modify_stop`` and ``cancel_order``
    adjust risk that already exists, so listing them as gaps would invite a
    hypothesis about cancelling an order.
    """
    state = book if isinstance(book, dict) else load_lab()
    card_rows = rows if rows is not None else card_facts(state)
    by_status = {status: 0 for status in CARD_STATUSES}
    awaiting: list[str] = []
    resolved_total = 0
    for row in card_rows:
        status = str(row.get("status") or "testing").strip().lower()
        if status in by_status:
            by_status[status] += 1
        n = int(row.get("resolved") or 0)
        resolved_total += n
        if status != "retired" and n == 0:
            awaiting.append(_card_label(row))
    coverage = type_coverage(state)
    return {
        "cards": by_status,
        "resolved_trades": resolved_total,
        "cards_awaiting_first_trade": awaiting[:12],
        "trunks_with_cards": [r["type"] for r in coverage if r["cards"]],
        "entry_trunks_untried": [
            r["type"] for r in coverage
            if not r["cards"] and r["type"] not in _MANAGEMENT_TRUNKS()
        ],
    }


def playbook_facts(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forest vs the last write: age and score at write vs now. No lecture."""
    from abcxauto.world_state import pct_of_nl

    lab = load_lab()
    inst = notebook_text(lab)
    at_write = lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    now_sc = scorecard if isinstance(scorecard, dict) else {}
    age = playbook_age_hours(lab)
    ledger = [_compact_card(r) for r in ensure_ledger(lab)]
    since = _since_write_score(lab, now_sc)
    nl = now_sc.get("net_liquidation")
    at_edge = at_write.get("edge_usd")
    now_edge = now_sc.get("edge_usd")
    since_edge = since.get("since_write_edge")
    facts = {
        "revision": lab.get("revision"),
        "mode": lab.get("mode") or None,
        "has_instructions": bool(inst),
        "ready_to_promote": bool(lab.get("ready_to_promote")) if inst else None,
        "age_h": round(age, 1) if age is not None else None,
        "at_write_edge": at_edge,
        "at_write_edge_pct_of_nl": pct_of_nl(at_edge, nl),
        "at_write_beating": at_write.get("beating_model"),
        "now_edge": now_edge,
        "now_edge_pct_of_nl": pct_of_nl(now_edge, nl),
        "now_beating": now_sc.get("beating_model"),
        "since_write_edge": since_edge,
        "since_write_edge_pct_of_nl": pct_of_nl(since_edge, nl),
        "since_write_pnl": since.get("since_write_pnl"),
        "lots_at_write": [str(x) for x in (lab.get("lots_at_write") or [])][:16],
        "ledger": ledger[-8:],
    }
    facts.update(_window_edges(now_sc))
    halt = _clerk_halt_slice(now_sc)
    facts.update(halt)
    if isinstance(halt, dict):
        facts["halt_trips_at_pct_of_nl"] = pct_of_nl(halt.get("halt_trips_at_usd"), nl)
        facts["ibkr_day_vs_halt_pct_of_nl"] = pct_of_nl(halt.get("ibkr_day_vs_halt"), nl)
    return facts


def _clerk_halt_slice(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    from abcxauto.book import clerk_halt_facts

    nl = None
    day = None
    if isinstance(scorecard, dict):
        nl = scorecard.get("net_liquidation")
        day = scorecard.get("ibkr_daily_pnl")
        if day is None:
            day = scorecard.get("daily_pnl")
    if nl is None or day is None:
        try:
            from abcxauto.memory import get_journal

            perf = get_journal().account_performance() or {}
            if nl is None:
                nl = perf.get("net_liquidation")
            if day is None:
                day = perf.get("daily_pnl")
        except Exception:
            pass
    return clerk_halt_facts(nl, day)


def format_ledger_line(facts: dict[str, Any] | None) -> str:
    rows = (facts or {}).get("ledger") if isinstance(facts, dict) else None
    if not isinstance(rows, list) or not rows:
        return ""
    bits = []
    for row in rows[-4:]:
        if not isinstance(row, dict) or row.get("revision") is None:
            continue
        bit = f"r{row.get('revision')}:{row.get('edge_usd')}"
        if row.get("closed_edge") is not None:
            bit += f">{row.get('closed_edge')}"
        bits.append(bit)
    return " ".join(bits)


def _live_scorecard(lab: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current scorecard. The stamp on disk is at_write, not now."""
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard()
        if isinstance(sc, dict) and sc:
            return sc
    except Exception:
        pass
    state = lab if isinstance(lab, dict) else {}
    stamp = state.get("paper_score")
    return stamp if isinstance(stamp, dict) else {}


def format_block() -> str:
    """Compact forest for book/wake. Full prose is the playbook tool."""
    paper = is_paper()
    lab = load_lab()
    live = load_live()
    if paper:
        inst = notebook_text(lab)
        if not inst:
            return "LAB PLAYBOOK: none. write_lab_playbook to set; playbook tool for full text.\n"
        live_sc = _live_scorecard(lab)
        facts = playbook_facts(live_sc)
        ledger = format_ledger_line(facts)
        lots = facts.get("lots_at_write") or []
        lots_s = ",".join(str(x) for x in lots[:8]) if lots else "none"
        return (
            "LAB PLAYBOOK:\n"
            f"- rev={lab.get('revision')} mode={lab.get('mode') or 'explore'} "
            f"promoted={bool(lab.get('promoted'))}\n"
            f"- since_write={facts.get('since_write_edge')} "
            f"now_edge={facts.get('now_edge')} "
            f"4h={facts.get('win_4h')} age_h={facts.get('age_h')}\n"
            f"- lots_at_write={lots_s}\n"
            f"- ledger: {ledger or 'none'}\n"
            "- notebook: playbook tool; send is the book\n"
        )
    inst = notebook_text(live)
    if not inst:
        return "LIVE: no promoted paper playbook. New risk blocked until promote (code).\n"
    return (
        "LIVE PLAYBOOK (promoted snapshot):\n"
        f"- promoted_revision={live.get('promoted_revision')} "
        f"promoted_at={live.get('promoted_at')}\n"
        "- notebook: playbook tool\n"
    )


def clear_lab(*, reason: str = "") -> dict[str, Any]:
    """Operator wipe. Grok starts a new notebook; no standing essay."""
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "mode": "explore",
        "instructions": "",
        "types": {},
        "ready_to_promote": False,
        "promoted": False,
        "revision": 0,
        "written_at": now,
        "cleared_at": now,
        "cleared_reason": str(reason or "")[:240],
        "ledger": [],
        "paper_score": {},
    }
    _write(_lab_path(), state)
    return state


def playbook_payload(revision: Any = None, *, full: bool = False) -> dict[str, Any]:
    """Notebook plus score since write. full is accepted and ignored â€” the notes are the tool."""
    paper = is_paper()
    lab = load_lab() if paper else load_live()
    live_sc = _live_scorecard(lab) if paper else (
        lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    )
    facts = playbook_facts(live_sc)
    inst = notebook_text(lab)
    types = lab.get("types") if isinstance(lab.get("types"), dict) else {}
    current: dict[str, Any] = {
        "revision": lab.get("revision") or lab.get("promoted_revision"),
        "mode": lab.get("mode"),
        "ready_to_promote": bool(lab.get("ready_to_promote")),
        "promoted": bool(lab.get("promoted")),
        "written_at": lab.get("written_at") or lab.get("promoted_at"),
        "paper_score": lab.get("paper_score") or {},
        "types": types,
        "instructions": inst,
        "instructions_n": len(inst),
    }
    cards = _flat_card_projection(lab)
    facts_by_card = card_facts(lab)
    out: dict[str, Any] = {
        "scope": "lab" if paper else "live",
        "tree": notebook_text(lab),
        "cards": cards,
        "types": types,
        "unfiled_cards": list(lab.get(UNFILED_KEY) or []),
        "card_scores": facts_by_card,
        "graduated": [_card_label(r) for r in facts_by_card if r.get("graduated")],
        "tripped": [_card_label(r) for r in facts_by_card if r.get("tripped")],
        "needs_declaration": [
            _card_label(r)
            for r in facts_by_card
            if r.get("needs_retire_if") or r.get("needs_thesis")
        ],
        "strategy_scores": strategy_scores(),
        "lab": lab_facts(lab, rows=facts_by_card),
        "score": {
            "revision": facts.get("revision"),
            "age_h": facts.get("age_h"),
            "at_write_edge": facts.get("at_write_edge"),
            "now_edge": facts.get("now_edge"),
            "since_write_edge": facts.get("since_write_edge"),
            "since_write_pnl": facts.get("since_write_pnl"),
            "lots_at_write": list(facts.get("lots_at_write") or []),
            "clerk_halted": facts.get("clerk_halted"),
            "halt_kind": facts.get("halt_kind"),
            "halt_reason": facts.get("halt_reason"),
            "daily_loss_limit_pct": facts.get("daily_loss_limit_pct"),
            "halt_trips_at_usd": facts.get("halt_trips_at_usd"),
            "halt_trips_at_pct_of_nl": facts.get("halt_trips_at_pct_of_nl"),
            "ibkr_day_vs_halt": facts.get("ibkr_day_vs_halt"),
            "ibkr_day_vs_halt_pct_of_nl": facts.get("ibkr_day_vs_halt_pct_of_nl"),
            "now_edge_pct_of_nl": facts.get("now_edge_pct_of_nl"),
            "since_write_edge_pct_of_nl": facts.get("since_write_edge_pct_of_nl"),
        },
        "current": current,
        "facts": facts,
        "ledger": [_compact_card(r) for r in ensure_ledger(lab)],
    }
    if revision in (None, ""):
        return out
    try:
        want = int(revision)
    except (TypeError, ValueError):
        out["error"] = "revision must be an int"
        return out
    card = revision_card(want, lab)
    if card is None:
        out["error"] = f"revision {want} not in ledger"
        return out
    out["revision"] = _outcome_card(card)
    return out
