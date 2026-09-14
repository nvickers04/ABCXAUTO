"""Shell-side token lever: one full copy of each live fact on the wire.

A think re-asks ``book`` / ``status`` / ``quote SYM`` as it moves. Every
earlier copy still rode every later turn of the look — the single largest
input-token sink, and pure waste: the model already has the newer number in
the same list. When a read lands whose key matches an earlier tool result,
the earlier result's content is replaced in place by a short stub. The
tool_call / tool_result pairing is untouched (same message, same
``tool_call_id``) so the API never sees an unanswered call.

Never touches ``send`` / ``self_tune`` results — those are the record of what
the book did, not a snapshot that goes stale. Never stubs an earlier fact in
favour of a later error or deferred read.

Knob: ``ABCXAUTO_PRUNE_TOOL_RESULTS=0`` turns it off. Default on.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Whole-book reads: any later call supersedes the earlier one regardless of
# arguments, because the payload is "the book now", not "the book for args".
_STATE_TOOLS = frozenset({"book", "status", "fills", "option_facts"})
# The ticket record. A stub here would erase what Grok did to the book.
_KEEP_TOOLS = frozenset({"send", "self_tune"})
_LEDGER_ATTR = "_abcx_prune_ledger"


def enabled() -> bool:
    raw = (os.environ.get("ABCXAUTO_PRUNE_TOOL_RESULTS") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def supersede_key(name: str, args: dict[str, Any] | None) -> str | None:
    """Which earlier results this call makes stale. ``None`` = never prune."""
    key = str(name or "").strip().lower()
    if not key or key in _KEEP_TOOLS:
        return None
    if key in _STATE_TOOLS:
        return key
    try:
        blob = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return f"{key}:{blob}"


def stub_text(name: str) -> str:
    return json.dumps({
        "superseded": True,
        "tool": str(name or ""),
        "note": f"superseded by a later {name} call; see the newer result",
    })


def _ledger(chat: Any) -> dict[str, dict[str, str]] | None:
    """``tool_call_id -> {key, tool}`` for every full result on this chat."""
    led = getattr(chat, _LEDGER_ATTR, None)
    if isinstance(led, dict):
        return led
    led = {}
    try:
        setattr(chat, _LEDGER_ATTR, led)
    except (AttributeError, TypeError):
        return None
    return led


def _is_tool_message(msg: Any) -> bool:
    try:
        from xai_sdk.proto import chat_pb2

        return int(getattr(msg, "role", -1)) == int(chat_pb2.MessageRole.ROLE_TOOL)
    except Exception:
        # proto enum unavailable — tool_call_id is the same role signal.
        return bool(getattr(msg, "tool_call_id", ""))


def _content_chars(msg: Any) -> int:
    total = 0
    for part in getattr(msg, "content", None) or []:
        total += len(str(getattr(part, "text", "") or ""))
    return total


def _replace_content(msg: Any, new_text: str) -> bool:
    from xai_sdk.chat import text

    content = getattr(msg, "content", None)
    if content is None:
        return False
    try:
        del content[:]
        content.append(text(new_text))
    except Exception:
        logger.debug("prune: content replace failed", exc_info=True)
        return False
    return True


def payload_chars(chat: Any) -> int:
    """Characters of message text on the wire for the next turn."""
    total = 0
    for msg in getattr(chat, "messages", None) or []:
        total += _content_chars(msg)
        for tc in getattr(msg, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            total += len(str(getattr(fn, "arguments", "") or ""))
    return total


def prune_superseded(
    chat: Any,
    *,
    name: str,
    args: dict[str, Any] | None,
    tool_call_id: str | None,
    is_fact: bool = True,
) -> tuple[int, int]:
    """Register this result; stub every earlier result it supersedes.

    Returns ``(results_stubbed, chars_saved)``. Zero when off, when the chat
    is a test double without ``messages``, when the call has no id, or when
    this result is not a fact (an error must not erase a real read).
    """
    if not enabled():
        return 0, 0
    cid = str(tool_call_id or "").strip()
    if not cid:
        return 0, 0
    key = supersede_key(name, args)
    if key is None:
        return 0, 0
    led = _ledger(chat)
    if led is None:
        return 0, 0
    messages = getattr(chat, "messages", None)
    if messages is None:
        return 0, 0
    stale_ids = {
        old_id
        for old_id, row in led.items()
        if old_id != cid and row.get("key") == key
    }
    led[cid] = {"key": key, "tool": str(name or "")}
    if not is_fact or not stale_ids:
        return 0, 0
    stubbed = 0
    saved = 0
    for msg in messages:
        if not _is_tool_message(msg):
            continue
        mid = str(getattr(msg, "tool_call_id", "") or "")
        if mid not in stale_ids:
            continue
        before = _content_chars(msg)
        stub = stub_text(str(led[mid].get("tool") or name))
        if before <= len(stub):
            led.pop(mid, None)
            continue
        if _replace_content(msg, stub):
            stubbed += 1
            saved += before - len(stub)
            # Stubbed once; a third call must not count it again.
            led.pop(mid, None)
    return stubbed, saved
