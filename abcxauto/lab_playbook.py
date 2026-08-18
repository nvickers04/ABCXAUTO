"""Paper lab playbook — Grok writes the instructions; live only follows a promote.

Paper researches established structures, journals what beat model cost, and does those more.
Live never copies paper fills. It may take new risk only after a promoted
snapshot exists (scorecard beating + Grok marked ready). Operator still must
connect live TWS (7496) with the confirm phrase. Two processes, two client ids.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LAB = _REPO_ROOT / "playbook_lab.json"
_DEFAULT_LIVE = _REPO_ROOT / "playbook_live.json"
_MAX_INSTRUCTIONS = 8000
_LEDGER_CAP = 12
_PATCH_KEYS = (
    "instructions",
    "mode",
    "do_more",
    "stop_doing",
    "ready_to_promote",
    "basis",
    "evidence",
)
_RESEARCH_TOOLS = frozenset({
    "strategies",
    "book",
    "scan",
    "news",
    "candles",
    "journal",
    "option_facts",
})
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


def load_lab() -> dict[str, Any]:
    return _read(_lab_path())


def load_live() -> dict[str, Any]:
    return _read(_live_path())


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


def clamp_update(raw: Any) -> dict[str, Any] | None:
    """Full rewrite or patch. Omitted fields keep the previous lab text."""
    if not isinstance(raw, dict):
        return None
    if not any(k in raw for k in _PATCH_KEYS):
        return None
    prev = load_lab()
    instructions = _field(raw, prev, "instructions").strip()[:_MAX_INSTRUCTIONS]
    if not instructions:
        return None
    mode = _field(raw, prev, "mode", "explore").strip().lower()
    if mode not in ("explore", "exploit"):
        mode = "explore"
    ready = raw["ready_to_promote"] if "ready_to_promote" in raw else prev.get("ready_to_promote")
    replacing = bool(instructions) and "instructions" in raw
    if replacing:
        do_more = str(raw.get("do_more") or "")[:800] if "do_more" in raw else ""
        stop_doing = str(raw.get("stop_doing") or "")[:800] if "stop_doing" in raw else ""
    else:
        do_more = _field(raw, prev, "do_more")[:800]
        stop_doing = _field(raw, prev, "stop_doing")[:800]
    return {
        "mode": mode,
        "instructions": instructions,
        "do_more": do_more,
        "stop_doing": stop_doing,
        "ready_to_promote": bool(ready),
        "basis": _basis_list(raw, prev),
        "evidence": _field(raw, prev, "evidence")[:800],
        "research_tools": [
            str(x) for x in (raw.get("research_tools") or prev.get("research_tools") or [])
            if str(x).strip()
        ][:16],
    }


def _basis_list(raw: dict[str, Any], prev: dict[str, Any]) -> list[str]:
    from abcxauto.strategy_catalog import resolve_basis

    if "basis" not in raw:
        prev_b = prev.get("basis") if isinstance(prev.get("basis"), list) else []
        return [str(x) for x in prev_b if str(x).strip()][:12]
    items = raw.get("basis")
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        items = []
    out: list[str] = []
    for item in items:
        key = resolve_basis(str(item))
        if key and key not in out:
            out.append(key)
        if len(out) >= 12:
            break
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


def _score_snap(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else {}
    return {
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "model_cost_usd": sc.get("model_cost_usd"),
    }


def _ledger_card(state: dict[str, Any], scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else (
        state.get("paper_score") if isinstance(state.get("paper_score"), dict) else {}
    )
    return {
        "revision": state.get("revision"),
        "written_at": state.get("written_at"),
        "mode": state.get("mode"),
        "ready_to_promote": bool(state.get("ready_to_promote")),
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "do_more": str(state.get("do_more") or "")[:200],
        "stop_doing": str(state.get("stop_doing") or "")[:200],
        "basis": list(state.get("basis") or [])[:12],
        "evidence": str(state.get("evidence") or "")[:240],
        "instructions": str(state.get("instructions") or "")[:_MAX_INSTRUCTIONS],
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
        "basis": list(c.get("basis") or [])[:8],
    }


def _close_card(
    card: dict[str, Any],
    scorecard: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    sc = _score_snap(scorecard)
    out = dict(card)
    out["closed_at"] = now
    out["closed_edge"] = sc.get("edge_usd")
    out["closed_beating"] = sc.get("beating_model")
    out["closed_return_pct"] = sc.get("book_return_pct")
    return out


def ensure_ledger(lab: dict[str, Any] | None) -> list[dict[str, Any]]:
    """In-memory ledger. Seed from the current blob if the file is still flat."""
    state = lab if isinstance(lab, dict) else {}
    rows = [r for r in (state.get("ledger") or []) if isinstance(r, dict)]
    if rows:
        return rows[-_LEDGER_CAP:]
    if state.get("instructions") or state.get("revision"):
        return [_ledger_card(state, state.get("paper_score"))]
    return []


def revision_card(revision: int, lab: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = lab if isinstance(lab, dict) else load_lab()
    want = int(revision)
    for row in reversed(ensure_ledger(state)):
        try:
            if int(row.get("revision") or 0) == want:
                return dict(row)
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
    state = {
        **prev,
        **update,
        "revision": rev,
        "written_at": now,
        "promoted": False,
        "lots_at_write": [str(x) for x in (lots_at or [])][:32],
    }
    if scorecard:
        state["paper_score"] = _score_snap(scorecard)
    ledger.append(_ledger_card(state, state.get("paper_score")))
    state["ledger"] = ledger[-_LEDGER_CAP:]
    _write(_lab_path(), state)
    return state


def maybe_promote(*, scorecard: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Copy lab → live snapshot only when paper is beating the model bill."""
    lab = load_lab()
    if not lab.get("instructions"):
        return None
    if not lab.get("ready_to_promote"):
        return None
    sc = scorecard or lab.get("paper_score") or {}
    if sc.get("beating_model") is not True:
        return None
    now = datetime.now(timezone.utc).isoformat()
    live = {
        **lab,
        "promoted": True,
        "promoted_at": now,
        "promoted_revision": lab.get("revision"),
        "source": "paper_lab",
        "note": "live follows this snapshot; does not copy paper fills",
    }
    _write(_live_path(), live)
    lab["promoted"] = True
    lab["promoted_at"] = now
    _write(_lab_path(), lab)
    return live


def live_has_promoted() -> bool:
    live = load_live()
    return bool(live.get("promoted") and str(live.get("instructions") or "").strip())


def live_new_risk_allowed() -> bool:
    """Paper may hunt. Live may hunt only with a promoted playbook."""
    if is_paper():
        return True
    return live_has_promoted()


def apply_from_judgment(judgment: dict[str, Any] | None) -> dict[str, Any] | None:
    """Paper: persist Grok's playbook rewrite. Live: ignore writes."""
    if not judgment or not is_paper():
        return None
    raw = judgment.get("lab_playbook") or judgment.get("playbook")
    update = clamp_update(raw)
    if not update:
        return None
    score = None
    try:
        from abcxauto.scorecard import compute_scorecard

        score = compute_scorecard()
    except Exception:
        score = None
    state = save_lab(update, scorecard=score)
    maybe_promote(scorecard=score)
    return state


def playbook_is_stale(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a standing card is older than the lab rewrite cadence."""
    state = lab if isinstance(lab, dict) else load_lab()
    if not str(state.get("instructions") or "").strip():
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
    """Score since the last write. Not the notebook text — Grok asks playbook() for that."""
    facts = playbook_facts(scorecard)
    return {
        "revision": facts.get("revision"),
        "age_h": facts.get("age_h"),
        "since_write_edge": facts.get("since_write_edge"),
        "now_edge": facts.get("now_edge"),
        "win_4h": facts.get("win_4h"),
        "lots_at_write": list(facts.get("lots_at_write") or [])[:16],
    }


def playbook_facts(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forest vs the last write: age and score at write vs now. No lecture."""
    lab = load_lab()
    inst = str(lab.get("instructions") or "").strip()
    at_write = lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    now_sc = scorecard if isinstance(scorecard, dict) else {}
    age = playbook_age_hours(lab)
    ledger = [_compact_card(r) for r in ensure_ledger(lab)]
    since = _since_write_score(lab, now_sc)
    facts = {
        "revision": lab.get("revision"),
        "mode": lab.get("mode") or None,
        "has_instructions": bool(inst),
        "ready_to_promote": bool(lab.get("ready_to_promote")) if inst else None,
        "age_h": round(age, 1) if age is not None else None,
        "at_write_edge": at_write.get("edge_usd"),
        "at_write_beating": at_write.get("beating_model"),
        "now_edge": now_sc.get("edge_usd"),
        "now_beating": now_sc.get("beating_model"),
        "since_write_edge": since.get("since_write_edge"),
        "since_write_pnl": since.get("since_write_pnl"),
        "lots_at_write": [str(x) for x in (lab.get("lots_at_write") or [])][:16],
        "ledger": ledger[-8:],
    }
    facts.update(_window_edges(now_sc))
    return facts


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
        inst = str(lab.get("instructions") or "").strip()
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
    inst = str(live.get("instructions") or "").strip()
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
        "do_more": "",
        "stop_doing": "",
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
    """Notebook plus score since write. full is accepted and ignored — the notes are the tool."""
    paper = is_paper()
    lab = load_lab() if paper else load_live()
    live_sc = _live_scorecard(lab) if paper else (
        lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    )
    facts = playbook_facts(live_sc)
    inst = str(lab.get("instructions") or "")
    current: dict[str, Any] = {
        "revision": lab.get("revision") or lab.get("promoted_revision"),
        "mode": lab.get("mode"),
        "ready_to_promote": bool(lab.get("ready_to_promote")),
        "promoted": bool(lab.get("promoted")),
        "written_at": lab.get("written_at") or lab.get("promoted_at"),
        "paper_score": lab.get("paper_score") or {},
        "instructions": inst,
        "instructions_n": len(inst),
    }
    out: dict[str, Any] = {
        "scope": "lab" if paper else "live",
        "score": {
            "revision": facts.get("revision"),
            "age_h": facts.get("age_h"),
            "at_write_edge": facts.get("at_write_edge"),
            "now_edge": facts.get("now_edge"),
            "since_write_edge": facts.get("since_write_edge"),
            "since_write_pnl": facts.get("since_write_pnl"),
            "lots_at_write": list(facts.get("lots_at_write") or []),
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
    out["revision"] = card
    return out
