"""This-flight working memory — Grok-owned one-liners, not facts or playbook.

Cap is a short list. Clerk does not invent conclusions. Hard reset /
overnight park / research↔RTH chat drop clear the shard (same as
``_reset_chat``). Not SYSTEM_PROMPT. Not a 40k residue bus.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_LINES = 20
MAX_LINE_CHARS = 160
MAX_RAW_CHARS = 400

_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
WORKING_MEMORY_PATH = _STATE_DIR / "working_memory.json"

_MATERIAL_TOOLS = frozenset({
    "book",
    "status",
    "quote",
    "fills",
    "news",
    "odds",
    "scan",
    "candles",
    "option_chain",
    "option_quote",
    "option_facts",
    "web",
    "send",
    "self_tune",
})


def working_memory_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_WORKING_MEMORY_PATH") or "").strip()
    return Path(raw) if raw else WORKING_MEMORY_PATH


def working_memory_lines() -> list[str]:
    row = _load()
    return list(row.get("lines") or [])


def material_beat(
    tool_trace: list[Any] | None = None,
    text: str = "",
) -> bool:
    """True after a fact-tool / send beat or a spoken/think sentence this look.

    The current ``note`` chip does not count — Grok must have done work.
    """
    prior = [
        str(t or "").strip().lower()
        for t in (tool_trace or [])
        if str(t or "").strip().lower() not in {"", "note"}
    ]
    if any(name in _MATERIAL_TOOLS for name in prior):
        return True
    blob = " ".join(str(text or "").replace("\n", " ").split())
    return bool(blob) and blob not in {"?", ".", "!"}


def shape_line(raw: str) -> str:
    """One short sentence. Empty means refuse (dump, blank, or unshaped)."""
    blob = " ".join(str(raw or "").replace("\n", " ").replace("\r", " ").split())
    if not blob:
        return ""
    if len(blob) > MAX_RAW_CHARS:
        return ""
    for sep in (". ", "? ", "! "):
        if sep in blob:
            blob = blob.split(sep, 1)[0] + sep[0]
            break
    blob = blob.strip()
    if len(blob) > MAX_LINE_CHARS:
        blob = blob[:MAX_LINE_CHARS].rstrip()
    return blob


def remember(
    line: str,
    *,
    tool_trace: list[Any] | None = None,
    text: str = "",
) -> dict[str, Any]:
    """Persist one Grok-owned sentence after a material beat. Clerk never invents."""
    shaped = shape_line(line)
    raw = " ".join(str(line or "").replace("\n", " ").split())
    if not str(line or "").strip():
        return _view(reason="read")
    if len(raw) > MAX_RAW_CHARS or not shaped:
        return _view(reason="refused_dump")
    if not material_beat(tool_trace, text):
        return _view(reason="refused_no_material")
    lines = working_memory_lines()
    if lines and lines[-1] == shaped:
        return _view(reason="duplicate")
    lines.append(shaped)
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    _save(lines)
    return _view(reason="ok")


def clear_working_memory(*, reason: str = "clear") -> dict[str, Any]:
    """Drop this-flight lines. Overnight / park / hard reset / desk-mode roll."""
    _ = reason
    path = working_memory_path()
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        logger.debug("working_memory clear failed", exc_info=True)
        try:
            _save([])
        except Exception:
            logger.debug("working_memory empty rewrite failed", exc_info=True)
    return _view(reason=reason or "clear")


def _view(*, reason: str) -> dict[str, Any]:
    lines = working_memory_lines()
    return {
        "working_memory": lines,
        "n": len(lines),
        "max": MAX_LINES,
        "reason": reason,
    }


def _load() -> dict[str, Any]:
    path = working_memory_path()
    if not path.is_file():
        return {"lines": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"lines": []}
    if not isinstance(raw, dict):
        return {"lines": []}
    lines: list[str] = []
    for item in list(raw.get("lines") or []):
        bit = shape_line(str(item or ""))
        if bit and bit not in lines:
            lines.append(bit)
        if len(lines) >= MAX_LINES:
            break
    return {"lines": lines}


def _save(lines: list[str]) -> None:
    path = working_memory_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"lines": list(lines)[:MAX_LINES]}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("working_memory write failed", exc_info=True)
