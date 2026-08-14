"""Paper lab playbook — Grok writes the instructions; live only follows a promote.

Paper invents tactics, journals what beat model cost, and does those more.
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
_MAX_INSTRUCTIONS = 4000


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


def clamp_update(raw: Any) -> dict[str, Any] | None:
    """Keep Grok's standing instructions; drop junk."""
    if not isinstance(raw, dict):
        return None
    instructions = str(raw.get("instructions") or "").strip()[:_MAX_INSTRUCTIONS]
    if not instructions:
        return None
    mode = str(raw.get("mode") or "explore").strip().lower()
    if mode not in ("explore", "exploit"):
        mode = "explore"
    return {
        "mode": mode,
        "instructions": instructions,
        "do_more": str(raw.get("do_more") or "")[:800],
        "stop_doing": str(raw.get("stop_doing") or "")[:800],
        "ready_to_promote": bool(raw.get("ready_to_promote")),
    }


def save_lab(update: dict[str, Any], *, scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    prev = load_lab()
    now = datetime.now(timezone.utc).isoformat()
    rev = int(prev.get("revision") or 0) + 1
    state = {
        **prev,
        **update,
        "revision": rev,
        "written_at": now,
        "promoted": False,
    }
    if scorecard:
        state["paper_score"] = {
            "beating_model": scorecard.get("beating_model"),
            "edge_usd": scorecard.get("edge_usd"),
            "book_return_pct": scorecard.get("book_return_pct"),
            "model_cost_usd": scorecard.get("model_cost_usd"),
        }
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


def playbook_facts(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forest vs the last write: age and score at write vs now. No lecture."""
    lab = load_lab()
    inst = str(lab.get("instructions") or "").strip()
    at_write = lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    now_sc = scorecard if isinstance(scorecard, dict) else {}
    age = playbook_age_hours(lab)
    return {
        "revision": lab.get("revision"),
        "mode": lab.get("mode") or None,
        "has_instructions": bool(inst),
        "ready_to_promote": bool(lab.get("ready_to_promote")) if inst else None,
        "age_h": round(age, 1) if age is not None else None,
        "at_write_edge": at_write.get("edge_usd"),
        "at_write_beating": at_write.get("beating_model"),
        "now_edge": now_sc.get("edge_usd"),
        "now_beating": now_sc.get("beating_model"),
    }


def format_block() -> str:
    paper = is_paper()
    lab = load_lab()
    live = load_live()
    if paper:
        inst = str(lab.get("instructions") or "").strip()
        mode = lab.get("mode") or "explore"
        if not inst:
            return "LAB PLAYBOOK: none. Use write_lab_playbook to set standing notes.\n"
        return (
            "LAB PLAYBOOK:\n"
            f"- mode={mode} revision={lab.get('revision')} "
            f"ready_to_promote={bool(lab.get('ready_to_promote'))} "
            f"promoted={bool(lab.get('promoted'))}\n"
            f"- instructions: {inst[:1800]}\n"
            f"- do_more: {str(lab.get('do_more') or '-')[:400]}\n"
            f"- stop_doing: {str(lab.get('stop_doing') or '-')[:400]}\n"
        )
    inst = str(live.get("instructions") or "").strip()
    if not inst:
        return "LIVE: no promoted paper playbook. New risk blocked until promote (code).\n"
    return (
        "LIVE PLAYBOOK (promoted snapshot):\n"
        f"- promoted_revision={live.get('promoted_revision')} "
        f"promoted_at={live.get('promoted_at')}\n"
        f"- instructions: {inst[:1800]}\n"
        f"- do_more: {str(live.get('do_more') or '-')[:400]}\n"
        f"- stop_doing: {str(live.get('stop_doing') or '-')[:400]}\n"
    )
