"""Durable stance — one Grok-owned conclusion that outlives the chat.

``working_memory`` is this-flight and drops with the chat. Nothing survived
an overnight park, so every session rediscovered the same conclusions from
the same journal. This is the one thing that does survive: a single short
text Grok sets through the ``stance`` tool, stamped when it was set, shown
on the wake as one line so a stale stance is visible as stale.

Shell never writes it. Shell never clears it. No cap on age — the age is
painted so Grok decides whether it still holds. Cap on size, not on content.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_CHARS = 600

_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
STANCE_PATH = _STATE_DIR / "stance.json"


def stance_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_STANCE_PATH") or "").strip()
    return Path(raw) if raw else STANCE_PATH


def shape_text(raw: Any) -> str:
    """Whitespace-collapsed. Empty means nothing to set. Over the cap stays over."""
    return " ".join(str(raw or "").replace("\r", " ").replace("\n", " ").split())


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def stance_age(set_at: Any, now: datetime | None = None) -> str:
    """``12m`` / ``5h`` / ``3d``. Empty when the stamp is unreadable."""
    try:
        then = datetime.fromisoformat(str(set_at or ""))
    except (TypeError, ValueError):
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = max(0.0, (_now(now) - then).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 48 * 3600:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def read_stance() -> dict[str, Any]:
    """``{text, set_at, session}`` or ``{}``. A broken file is no stance."""
    path = stance_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    text = shape_text(raw.get("text"))
    if not text:
        return {}
    if len(text) > MAX_CHARS:
        # A hand-edited file over the cap is still one stance; the wake stays short.
        text = text[:MAX_CHARS].rstrip()
    return {
        "text": text,
        "set_at": str(raw.get("set_at") or ""),
        "session": str(raw.get("session") or ""),
    }


def stance_fact(now: datetime | None = None) -> dict[str, Any]:
    """Wake-side view: text plus an age so stale reads as stale."""
    row = read_stance()
    if not row:
        return {}
    return {
        "text": row["text"],
        "set_at": row["set_at"],
        "age": stance_age(row["set_at"], now=now),
    }


def format_stance_bit(fact: dict[str, Any] | None) -> str:
    """One wake part: ``stance(3h)=...``. Empty when there is no stance."""
    f = fact if isinstance(fact, dict) else {}
    text = shape_text(f.get("text"))
    if not text:
        return ""
    age = str(f.get("age") or "").strip()
    head = f"stance({age})" if age else "stance"
    return f"{head}={text}"


def stance_view(*, reason: str, now: datetime | None = None, **extra: Any) -> dict[str, Any]:
    fact = stance_fact(now=now)
    out: dict[str, Any] = {
        "stance": fact.get("text", ""),
        "set_at": fact.get("set_at", ""),
        "age": fact.get("age", ""),
        "max_chars": MAX_CHARS,
        "reason": reason,
    }
    out.update(extra)
    return out


def set_stance(
    text: Any,
    *,
    session: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replace the stance. Over the cap is refused with the length, not clipped."""
    shaped = shape_text(text)
    if not shaped:
        return stance_view(reason="read", now=now)
    if len(shaped) > MAX_CHARS:
        return stance_view(reason="refused_too_long", now=now, len=len(shaped))
    current = read_stance()
    if current and current.get("text") == shaped:
        return stance_view(reason="duplicate", now=now)
    stamp = _now(now).isoformat(timespec="seconds")
    _save({"text": shaped, "set_at": stamp, "session": str(session or "")})
    return stance_view(reason="ok", now=now)


def clear_stance() -> dict[str, Any]:
    path = stance_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        logger.debug("stance clear failed", exc_info=True)
    return stance_view(reason="cleared")


def _save(row: dict[str, Any]) -> None:
    path = stance_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(row, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("stance write failed", exc_info=True)
