"""Persist xAI ``response.id`` for server-side conversation continuation.

``chat.create(store_messages=True)`` stores the turn on xAI. The next
``chat.create(previous_response_id=...)`` continues that thread without
resending the old message list. The cursor survives park, research↔RTH,
empty looks, and process restarts — there is no clear path.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
_CURSOR_NAME = "chat_cursor.json"


def cursor_path() -> Path:
    return _STATE_DIR / _CURSOR_NAME


def load_previous_response_id() -> str:
    """Return the stored response id, or ``\"\"`` when absent/unreadable."""
    path = cursor_path()
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.debug("chat_cursor read failed", exc_info=True)
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("previous_response_id") or "").strip()


def save_previous_response_id(response_id: str | None) -> None:
    """Persist a non-empty response id. Empty/None is a no-op."""
    rid = str(response_id or "").strip()
    if not rid:
        return
    path = cursor_path()
    blob: dict[str, Any] = {"previous_response_id": rid}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("chat_cursor write failed", exc_info=True)
