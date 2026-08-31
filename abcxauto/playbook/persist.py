"""Lab/live json. The socket is the live switch, not a second rulebook."""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abcxauto.playbook.hub import hub as _hub
from abcxauto.playbook.schema import (
    UNFILED_KEY,
    _DEAD_LAB_KEYS,
    _GATE_FORBIDDEN,
    _STALE_H_DEFAULT,
    _clean_types,
    _flat_card_projection,
    _incoming_card_name,
    _norm_card,
    _seed_open_type_starters,
    _strip_projection,
    card_key,
    card_ticket_of,
    fill_assumption_of,
    playbook_type_keys,
    walk_cards,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_LAB = _REPO_ROOT / "playbook_lab.json"
_DEFAULT_LIVE = _REPO_ROOT / "playbook_live.json"
_LEDGER_CAP = 12

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


def _named_card_in(cards: Any, name: str) -> dict[str, Any] | None:
    want = str(name or "").strip().lower()
    if not want:
        return None
    for card in cards or []:
        if isinstance(card, dict) and str(card.get("name") or "").strip().lower() == want:
            return card
    return None


def _upsert_named_card(
    branch: list[Any],
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace a same-name card in place; append if the name is new."""
    key = str(row.get("name") or "").strip().lower()
    out: list[dict[str, Any]] = []
    found = False
    for card in branch:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        if str(card.get("name") or "").strip().lower() == key:
            if not found:
                out.append(row)
                found = True
            continue
        out.append(card)
    if not found:
        out.append(row)
    return out


def _file_cards_into_tree(
    types: Any,
    cards: list[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Route loose cards under the type they name, creating the trunk if new.

    A card is never dropped for want of a stanza. One that names nothing
    sendable cannot be placed, so it lands in ``unfiled_cards`` where it is
    still visible and still owed a parent. Same-name writes merge against the
    stored card and keep siblings — a one-card flat list is not a wipe.
    """
    allowed = set(playbook_type_keys())
    tree: dict[str, Any] = {
        k: dict(v) for k, v in (types or {}).items() if isinstance(v, dict)
    } if isinstance(types, dict) else {}
    unfiled: list[dict[str, Any]] = []
    for raw in cards:
        ticket = card_ticket_of(raw)
        if not ticket or ticket not in allowed:
            row = _norm_card(raw)
            if row is None:
                continue
            if ticket:
                # Unsendable, so it has no parent — but keep what it claimed so
                # the cockpit can still say which ticket does not exist.
                row["claimed_ticket"] = ticket
            if not any(c["name"].lower() == row["name"].lower() for c in unfiled):
                unfiled.append(row)
            continue
        stanza = dict(tree.get(ticket) or {})
        prev_card = _named_card_in(stanza.get("cards"), _incoming_card_name(raw))
        row = _norm_card(raw, prev=prev_card)
        if row is None:
            continue
        branch = [
            c for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        stanza["cards"] = _upsert_named_card(branch, row)
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
    """Lab notebook. Missing OPEN types gain locked starters; live cards stay."""
    return _seed_open_type_starters(_migrate_book(_read(_lab_path())))


def load_live() -> dict[str, Any]:
    """Promoted snapshot only. Never seed untested starters onto live."""
    return _migrate_book(_read(_live_path()))


def is_paper() -> bool:
    try:
        from abcxauto.config import get_config

        return bool(get_config().is_paper)
    except Exception:
        return True


def book_label() -> str:
    """paper TWS vs live TWS. The socket, not a second rulebook."""
    return "paper TWS" if _hub().is_paper() else "live TWS"

def _ensure_card_clocks(
    state: dict[str, Any],
    prev: dict[str, Any],
    now: str,
) -> None:
    """Stamp written_at once. Existing unstamped cards inherit the last wipe."""
    prev_by = {
        card_key(t, c.get("name")): c
        for t, c in walk_cards(prev)
        if isinstance(c, dict) and c.get("name")
    }
    old_fallback = str(
        (prev or {}).get("cleared_at") or (prev or {}).get("written_at") or now
    ).strip()
    for _t, card in walk_cards(state):
        if not isinstance(card, dict) or not card.get("name"):
            continue
        if str(card.get("written_at") or "").strip():
            continue
        old = prev_by.get(card_key(_t, card.get("name")))
        if old and str(old.get("written_at") or "").strip():
            card["written_at"] = str(old["written_at"]).strip()[:48]
        elif old:
            card["written_at"] = old_fallback[:48]
        else:
            card["written_at"] = now[:48]


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
    state = lab if isinstance(lab, dict) else _hub().load_lab()
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


def _norm_book_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _card_book_tuple(card: dict[str, Any]) -> tuple[Any, ...]:
    retire = card.get("retire_if") if isinstance(card.get("retire_if"), dict) else {}
    return (
        _norm_book_text(card.get("name")).lower(),
        _norm_book_text(card.get("thesis")),
        _norm_book_text(card.get("when_on")),
        _norm_book_text(card.get("scan")),
        _norm_book_text(card.get("shape")),
        _norm_book_text(card.get("invalidation")),
        _norm_book_text(card.get("status")).lower(),
        card.get("locked") is True,
        json.dumps(retire, sort_keys=True, default=str),
        _norm_book_text(card.get("expect_hit_rate")),
        fill_assumption_of(card),
    )


def book_fingerprint(state: dict[str, Any] | None) -> tuple[Any, ...]:
    """Durable book only. Notes, evidence, and next_look_s are look diary."""
    blob = state if isinstance(state, dict) else {}
    types = _clean_types(blob.get("types"))
    trunks: list[tuple[Any, ...]] = []
    for type_name in sorted(types):
        stanza = types[type_name] if isinstance(types.get(type_name), dict) else {}
        cards = tuple(
            _card_book_tuple(c)
            for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        )
        order = tuple(
            _norm_book_text(x).lower()
            for x in (stanza.get("tool_order") or [])
            if str(x or "").strip()
        )
        trunks.append(
            (
                type_name,
                order,
                _norm_book_text(stanza.get("gotchas")),
                _norm_book_text(stanza.get("review")),
                cards,
            )
        )
    return (
        str(blob.get("mode") or "explore").strip().lower(),
        bool(blob.get("ready_to_promote")),
        tuple(trunks),
    )


def _cadence_tuple(book: dict[str, Any] | None) -> tuple[tuple[Any, ...], ...]:
    rows: list[tuple[Any, ...]] = []
    for type_name, card in walk_cards(book if isinstance(book, dict) else {}):
        rows.append((type_name, card.get("name"), card.get("next_look_s")))
    return tuple(rows)


def _held_book(
    prev: dict[str, Any],
    staged: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    """Same fingerprint: keep the last real book. Overlay clerk-only fields.

    Cadence may move. Sanitized instructions (invented gates stripped) must
    land. Diary notes and evidence do not — those were minting fake progress.
    """
    out = _strip_projection(copy.deepcopy(prev))
    dest_types = out.get("types") if isinstance(out.get("types"), dict) else {}
    src_types = staged.get("types") if isinstance(staged.get("types"), dict) else {}
    for tname, stanza in src_types.items():
        dst = dest_types.get(tname)
        if not isinstance(dst, dict) or not isinstance(stanza, dict):
            continue
        by_name = {
            c.get("name"): c
            for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        }
        for card in dst.get("cards") or []:
            if not isinstance(card, dict):
                continue
            src = by_name.get(card.get("name"))
            if isinstance(src, dict) and "next_look_s" in src:
                card["next_look_s"] = src["next_look_s"]
    return out


def save_lab(
    update: dict[str, Any],
    *,
    scorecard: dict[str, Any] | None = None,
    persist_instructions: bool = False,
) -> dict[str, Any]:
    prev = _hub().load_lab()
    now = datetime.now(timezone.utc).isoformat()
    lots_at = update.get("lots_at_write")
    if not lots_at:
        try:
            from abcxauto.think_stream import LAST_TURN_PATH, _read_json

            lots_at = list((_read_json(LAST_TURN_PATH) or {}).get("open_lots") or [])
        except Exception:
            lots_at = list(prev.get("lots_at_write") or [])
    staged = _strip_projection(
        _seed_open_type_starters(
            _migrate_book(
                {
                    **_strip_projection(prev),
                    **update,
                }
            )
        )
    )
    prev_rev = int(prev.get("revision") or 0)
    hold = prev_rev > 0 and book_fingerprint(prev) == book_fingerprint(staged)
    if hold:
        out = _held_book(prev, staged, update)
        if persist_instructions and "instructions" in update:
            out["instructions"] = update.get("instructions") or ""
        dirty = (
            (out.get("instructions") or "") != (prev.get("instructions") or "")
            or _cadence_tuple(out) != _cadence_tuple(prev)
        )
        if dirty:
            disk = dict(out)
            disk.pop("revision_held", None)
            _write(_lab_path(), disk)
        out["revision_held"] = True
        return out
    ledger = ensure_ledger(prev)
    rev = prev_rev + 1
    if ledger and scorecard:
        ledger[-1] = _close_card(ledger[-1], scorecard, now)
    # A caller may still hand us the flat shape, so file ``update``'s cards into
    # the tree. ``prev``'s derived list is dropped first so a projection replay
    # does not double-file. Named writes merge; they do not wipe siblings.
    state = _strip_projection(
        _seed_open_type_starters(
            _migrate_book(
                {
                    **_strip_projection(prev),
                    **update,
                    "revision": rev,
                    "written_at": now,
                    "promoted": False,
                    "lots_at_write": [str(x) for x in (lots_at or [])][:32],
                }
            )
        )
    )
    _ensure_card_clocks(state, prev, now)
    if scorecard:
        state["paper_score"] = _score_snap(scorecard)
    ledger.append(_ledger_card(state, state.get("paper_score")))
    state["ledger"] = ledger[-_LEDGER_CAP:]
    state.pop("revision_held", None)
    _write(_lab_path(), state)
    return state

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



__all__ = [
    '_REPO_ROOT',
    '_DEFAULT_LAB',
    '_DEFAULT_LIVE',
    '_LEDGER_CAP',
    'stale_hours',
    '_lab_path',
    '_live_path',
    '_read',
    '_write',
    '_drop_dead_lab_keys',
    '_named_card_in',
    '_upsert_named_card',
    '_file_cards_into_tree',
    '_migrate_book',
    'load_lab',
    'load_live',
    'is_paper',
    'book_label',
    '_ensure_card_clocks',
    '_score_snap',
    '_outcome_card',
    '_ledger_card',
    '_compact_card',
    '_close_card',
    'ensure_ledger',
    'revision_card',
    '_norm_book_text',
    '_card_book_tuple',
    'book_fingerprint',
    '_cadence_tuple',
    '_held_book',
    'save_lab',
    'clear_lab',
]
