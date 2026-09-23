"""Persist xAI ``response.id`` for server-side conversation continuation.

``chat.create(store_messages=True)`` stores the turn on xAI. The next
``chat.create(previous_response_id=...)`` continues that thread without
resending the old message list. The cursor survives park, research↔RTH,
empty looks, and process restarts — there is no clear path.

``prompt_hash`` (sha256 of ``SYSTEM_PROMPT``) rides beside the id. A
matching hash keeps creates id-only; a missing or stale hash lets the
caller attach the current system message once, then save the new hash.
"""

from __future__ import annotations

import hashlib
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


def system_prompt_hash() -> str:
    """sha256 of ``SYSTEM_PROMPT`` plus the order-type lines.

    An order-type change refreshes the system message once on the same thread.
    """
    from abcxauto.llm import SYSTEM_PROMPT
    from abcxauto.order_examples import order_type_lines

    blob = SYSTEM_PROMPT + "\n" + order_type_lines()
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_cursor() -> dict[str, Any]:
    path = cursor_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.debug("chat_cursor read failed", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def load_previous_response_id() -> str:
    """Return the stored response id, or ``\"\"`` when absent/unreadable."""
    return str(_read_cursor().get("previous_response_id") or "").strip()


def load_prompt_hash() -> str:
    """Return the stored prompt hash, or ``\"\"`` when absent/unreadable."""
    return str(_read_cursor().get("prompt_hash") or "").strip()


def prompt_needs_refresh() -> bool:
    """True when an id exists but the stored hash is missing or stale."""
    if not load_previous_response_id():
        return False
    return load_prompt_hash() != system_prompt_hash()


def save_previous_response_id(response_id: str | None) -> None:
    """Persist a non-empty response id and the current prompt hash.

    Empty/None is a no-op (never clears the cursor).
    """
    rid = str(response_id or "").strip()
    if not rid:
        return
    path = cursor_path()
    blob: dict[str, Any] = {
        "previous_response_id": rid,
        "prompt_hash": system_prompt_hash(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("chat_cursor write failed", exc_info=True)
