"""Durable desk tool-facts. Not playbook cards, not wake jobs, not law.

Grok reads the shelf on ``book()`` (and ``status``) every look. It writes
with ``write_desk_lessons``, same persist pattern as ``write_lab_playbook``.
Daily notebook / lab cards stay daily trade notes.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO_ROOT / "data" / "state" / "desk_lessons.json"
_MAX_FACT = 800
_MAX_LESSONS = 16
_MAX_ID = 64

SEED_ID = "scan_overflow"
SEED_FACT = (
    "Scan overflow drops ranked hits and keeps sessions. Do not rescan the "
    "same arena for the missing list. Use the names you already have, or "
    "scan(symbols=[...]) / candles / quote those names."
)
RISKLESS_COMBO_CAP_ID = "riskless_combo_cap"
RISKLESS_COMBO_CAP_FACT = (
    "IBKR [202] caps active riskless/guaranteed-loss BAGs (iron condor, "
    "iron butterfly, butterfly) at one. A second send pops TWS confirm. "
    "Wait until that working BAG is gone."
)
SEED_LESSONS = (
    {"id": SEED_ID, "fact": SEED_FACT},
    {"id": RISKLESS_COMBO_CAP_ID, "fact": RISKLESS_COMBO_CAP_FACT},
)
SEED_IDS = frozenset(row["id"] for row in SEED_LESSONS)

__all__ = (
    "RISKLESS_COMBO_CAP_FACT",
    "RISKLESS_COMBO_CAP_ID",
    "SEED_FACT",
    "SEED_ID",
    "SEED_IDS",
    "SEED_LESSONS",
    "apply_desk_lessons",
    "desk_lessons_payload",
    "load_desk_lessons",
)


def _path() -> Path:
    raw = (os.environ.get("ABCXAUTO_DESK_LESSONS_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_rows() -> list[dict[str, str]]:
    return [dict(row) for row in SEED_LESSONS]


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("desk lessons read failed path=%s", path, exc_info=True)
        return {}


def _write(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8"
        )
    except Exception:
        logger.exception("desk lessons write failed path=%s", path)


def _norm_id(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    return text[:_MAX_ID]


def _norm_fact(raw: Any) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    return text[:_MAX_FACT]


def _norm_row(raw: Any) -> dict[str, str] | None:
    if isinstance(raw, str):
        fact = _norm_fact(raw)
        return {"id": "", "fact": fact} if fact else None
    if not isinstance(raw, dict):
        return None
    fact = _norm_fact(raw.get("fact") or raw.get("lesson") or raw.get("text"))
    if not fact:
        return None
    return {"id": _norm_id(raw.get("id") or raw.get("name")), "fact": fact}


def _norm_lessons(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("lessons") or raw.get("desk_lessons") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        row = _norm_row(item)
        if not row:
            continue
        key = row["id"] or row["fact"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= _MAX_LESSONS:
            break
    return out


def _seeds_current(stored: list[dict[str, str]]) -> bool:
    if len(stored) < len(SEED_LESSONS):
        return False
    for index, seed in enumerate(SEED_LESSONS):
        if stored[index].get("id") != seed["id"] or stored[index].get("fact") != seed["fact"]:
            return False
    return True


def _merge_seed(rows: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Code seeds always lead. File extras follow. Seed ids cannot be replaced."""
    extras: list[dict[str, str]] = []
    seen: set[str] = set(SEED_IDS)
    for row in rows or []:
        rid = row.get("id") or ""
        if rid in SEED_IDS:
            continue
        key = rid or (row.get("fact") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        extras.append({"id": rid, "fact": row.get("fact") or ""})
        if len(extras) >= _MAX_LESSONS - len(SEED_LESSONS):
            break
    return [*_seed_rows(), *extras]


def load_desk_lessons() -> dict[str, Any]:
    """Full shelf. Seed is present even when the file is missing."""
    path = _path()
    raw = _read(path)
    lessons = _merge_seed(_norm_lessons(raw))
    stored = _norm_lessons(raw)
    if not raw or not stored or not _seeds_current(stored):
        _write(
            path,
            {
                "lessons": lessons,
                "written_at": raw.get("written_at") or _now(),
                "revision": int(raw.get("revision") or 0) + (0 if raw else 1),
            },
        )
    return {"lessons": lessons}


def desk_lessons_payload() -> list[dict[str, str]]:
    """Compact list for book() / status. Tool facts only."""
    return [dict(row) for row in load_desk_lessons().get("lessons") or []]


def _incoming_rows(raw: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        return []
    rows = _norm_lessons(raw.get("lessons") if "lessons" in raw else None)
    one = _norm_row(
        {
            "id": raw.get("id") or raw.get("name"),
            "fact": raw.get("fact") or raw.get("lesson") or raw.get("text"),
        }
        if raw.get("fact") or raw.get("lesson") or raw.get("text")
        else None
    )
    if one:
        rows.append(one)
    return rows


def apply_desk_lessons(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Persist extras. Seed stays. Empty write is rejected."""
    incoming = _incoming_rows(raw if isinstance(raw, dict) else None)
    if not incoming:
        return {
            "status": "rejected",
            "note": "write_desk_lessons needs a lesson",
            "desk_lessons": desk_lessons_payload(),
        }
    path = _path()
    prior = _norm_lessons(_read(path))
    extras = [row for row in prior if row.get("id") not in SEED_IDS]
    by_id: dict[str, dict[str, str]] = {}
    ordered: list[dict[str, str]] = []
    for row in extras + incoming:
        if row.get("id") in SEED_IDS:
            continue
        key = row.get("id") or row.get("fact", "").lower()
        if not key:
            continue
        if key in by_id:
            by_id[key]["fact"] = row["fact"]
            if row.get("id"):
                by_id[key]["id"] = row["id"]
            continue
        item = {"id": row.get("id") or "", "fact": row["fact"]}
        by_id[key] = item
        ordered.append(item)
    lessons = _merge_seed(ordered)
    prev = _read(path)
    try:
        rev = int(prev.get("revision") or 0) + 1
    except (TypeError, ValueError):
        rev = 1
    state = {"lessons": lessons, "written_at": _now(), "revision": rev}
    _write(path, state)
    return {"status": "ok", "desk_lessons": [dict(r) for r in lessons], "revision": rev}
