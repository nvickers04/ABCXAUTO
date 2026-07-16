"""Session prep + EOD review artifacts (separate from the 120s cycle)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PREP_PATH = _REPO_ROOT / "session_prep.json"
_REVIEW_PATH = _REPO_ROOT / "session_review.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _path(kind: str) -> Path:
    import os

    if kind == "prep":
        raw = os.environ.get("ABCXAUTO_SESSION_PREP_PATH", "").strip()
        return Path(raw) if raw else _PREP_PATH
    raw = os.environ.get("ABCXAUTO_SESSION_REVIEW_PATH", "").strip()
    return Path(raw) if raw else _REVIEW_PATH


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("session_cadence load failed %s", path)
        return {}


def save_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def load_prep() -> dict[str, Any]:
    return load_json(_path("prep"))


def load_review() -> dict[str, Any]:
    return load_json(_path("review"))


def write_prep(
    *,
    bias: str = "",
    levels: str = "",
    do_not_trade_if: str = "",
    watchlist: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    data = {
        "ts": _utc_now(),
        "bias": bias,
        "levels": levels,
        "do_not_trade_if": do_not_trade_if,
        "watchlist": list(watchlist or []),
        "notes": notes,
    }
    save_json(_path("prep"), data)
    return data


def write_review(
    *,
    what_worked: str = "",
    mistake: str = "",
    next_change: str = "",
    notes: str = "",
) -> dict[str, Any]:
    data = {
        "ts": _utc_now(),
        "what_worked": what_worked,
        "mistake": mistake,
        "next_change": next_change,
        "notes": notes,
    }
    save_json(_path("review"), data)
    return data


def maybe_auto_prep_from_world(world: dict[str, Any]) -> dict[str, Any]:
    """Lightweight prep if none exists for today (UTC day)."""
    existing = load_prep()
    today = _utc_now()[:10]
    if existing.get("ts", "").startswith(today):
        return existing
    opps = world.get("opportunities") or []
    watch = [str(o.get("symbol") or "") for o in opps[:5] if o.get("symbol")]
    regime = world.get("regime") or {}
    bias = str(regime.get("trend_bias") or "neutral")
    return write_prep(
        bias=bias,
        levels=str(regime.get("session_phase") or ""),
        do_not_trade_if="unprotected STK or no A/B setup under posture",
        watchlist=watch,
        notes="auto-prep from WorldState",
    )


def maybe_auto_review_from_cycle(summary: dict[str, Any]) -> dict[str, Any] | None:
    """Write a thin review stub when agent stops or end-of-day flag set."""
    if not summary.get("force") and not summary.get("end_of_day"):
        return None
    return write_review(
        what_worked=str(summary.get("what_worked") or summary.get("thesis") or "")[:400],
        mistake=str(summary.get("mistake") or "")[:400],
        next_change=str(summary.get("next_change") or "one change max")[:400],
        notes=str(summary.get("notes") or "auto-review")[:400],
    )
