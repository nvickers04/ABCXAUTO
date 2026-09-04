"""Per-session look and token ceilings.

A flat stay-up grind can think all RTH and still lose the scorecard.
These caps still paint on the wake (looks/tokens left). Overnight /
closed / postmarket may idle / park-ready on a hit. Paper RTH /
premarket stay-up does not sit the desk on this cap. Chat is kept.
No sit clock. Overnight park stays park_clock.

Session key is ET date + market label (premarket / regular). Premarket
and RTH each get a budget so stay-up through the open still trades.
Grok may tighten ``session_look_cap`` via self_tune; it cannot raise it.
``session_token_cap`` is an operator disk knob — file wins.
Remaining looks/tokens feed the wake worst-fact line. Knob names stay off
the system prompt.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO / "data" / "state" / "session_caps.json"

# Conservative RTH ceiling: a working desk at ~2.5 min/look can finish
# regular hours. A 45s no-ticket grind stops in about two hours.
DEFAULT_LOOK_CAP = 160
DEFAULT_TOKEN_CAP = 2_500_000
LOOK_CAP_RANGE = (1, 400)
TOKEN_CAP_RANGE = (50_000, 10_000_000)

_cache: dict[str, Any] | None = None
_cache_path: str = ""


def _path() -> Path:
    raw = (os.environ.get("ABCXAUTO_SESSION_CAPS_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def _et_date(now: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo

    clock = now or datetime.now(ZoneInfo("America/New_York"))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        clock = clock.astimezone(ZoneInfo("America/New_York"))
    return clock.date().isoformat()


def session_key(session: str = "", *, now: datetime | None = None) -> str:
    """One budget per ET date + stay-up label (premarket / regular)."""
    from abcxauto.park_clock import resolve_stay_up_session

    sess = resolve_stay_up_session(session, now=now)
    sess = str(sess or session or "").strip().lower()
    if sess in ("", "unknown"):
        sess = "unknown"
    return f"{_et_date(now)}:{sess}"


def _empty(key: str) -> dict[str, Any]:
    return {"key": key, "looks": 0, "tokens": 0}


def _row_of(raw: Any, key: str = "") -> dict[str, Any]:
    blob = raw if isinstance(raw, dict) else {}
    try:
        looks = max(0, int(blob.get("looks") or 0))
    except (TypeError, ValueError):
        looks = 0
    try:
        tokens = max(0, int(blob.get("tokens") or 0))
    except (TypeError, ValueError):
        tokens = 0
    return {"key": str(blob.get("key") or key), "looks": looks, "tokens": tokens}


def _load_table() -> dict[str, dict[str, Any]]:
    """All session rows. Premarket and RTH keep separate budgets."""
    global _cache, _cache_path
    p = str(_path())
    if _cache is not None and _cache_path == p:
        return _cache
    table: dict[str, dict[str, Any]] = {}
    path = _path()
    blob: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                blob = raw
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            blob = {}
    sessions = blob.get("sessions") if isinstance(blob.get("sessions"), dict) else None
    if sessions:
        for raw_key, raw_row in sessions.items():
            key = str(raw_key or "")
            if not key:
                continue
            table[key] = _row_of(raw_row, key)
    elif str(blob.get("key") or ""):
        # Legacy single-row file from before dual-mode.
        row = _row_of(blob)
        table[str(blob.get("key"))] = row
    _cache = table
    _cache_path = p
    return table


def _save_table(table: dict[str, dict[str, Any]]) -> None:
    global _cache, _cache_path
    path = _path()
    clean: dict[str, dict[str, Any]] = {}
    for raw_key, raw_row in dict(table or {}).items():
        key = str(raw_key or "")
        if not key:
            continue
        clean[key] = _row_of(raw_row, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"sessions": clean}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug("session_caps write failed", exc_info=True)
    _cache = clean
    _cache_path = str(path)


def reset_session_caps() -> None:
    """Drop the in-memory cache (tests)."""
    global _cache, _cache_path
    _cache = None
    _cache_path = ""


def _caps() -> tuple[int, int]:
    from abcxauto.config import get_config

    cfg = get_config()
    lo_l, hi_l = LOOK_CAP_RANGE
    lo_t, hi_t = TOKEN_CAP_RANGE
    try:
        looks = int(getattr(cfg, "session_look_cap", DEFAULT_LOOK_CAP) or DEFAULT_LOOK_CAP)
    except (TypeError, ValueError):
        looks = DEFAULT_LOOK_CAP
    try:
        tokens = int(
            getattr(cfg, "session_token_cap", DEFAULT_TOKEN_CAP) or DEFAULT_TOKEN_CAP
        )
    except (TypeError, ValueError):
        tokens = DEFAULT_TOKEN_CAP
    return (
        max(lo_l, min(hi_l, looks)),
        max(lo_t, min(hi_t, tokens)),
    )


def _state_for(session: str = "", *, now: datetime | None = None) -> dict[str, Any]:
    key = session_key(session, now=now)
    table = _load_table()
    row = table.get(key)
    if not isinstance(row, dict) or str(row.get("key") or "") != key:
        row = _empty(key)
        table[key] = row
        _save_table(table)
    return row


def billed_tokens_now() -> int:
    """Journal billed tokens (input + output + cached). 0 when the journal is dark."""
    try:
        from abcxauto.memory import get_journal

        used = get_journal().model_usage_totals() or {}
        return (
            max(0, int(used.get("input_tokens") or 0))
            + max(0, int(used.get("output_tokens") or 0))
            + max(0, int(used.get("cached_tokens") or 0))
        )
    except Exception:
        return 0


def usage(session: str = "", *, now: datetime | None = None) -> dict[str, Any]:
    """Operator snapshot plus remaining counts for the wake worst-fact line."""
    look_cap, token_cap = _caps()
    state = _state_for(session, now=now)
    looks = int(state.get("looks") or 0)
    tokens = int(state.get("tokens") or 0)
    hit = looks >= look_cap or tokens >= token_cap
    why = ""
    if looks >= look_cap:
        why = "looks"
    if tokens >= token_cap:
        why = "tokens" if not why else "looks+tokens"
    return {
        "key": str(state.get("key") or ""),
        "looks": looks,
        "tokens": tokens,
        "look_cap": look_cap,
        "token_cap": token_cap,
        "looks_left": max(0, look_cap - looks),
        "tokens_left": max(0, token_cap - tokens),
        "hit": hit,
        "why": why,
    }


def is_capped(session: str = "", *, now: datetime | None = None) -> bool:
    return bool(usage(session, now=now).get("hit"))


def note_look(
    session: str = "",
    *,
    tokens: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Count one finished look (and its billed tokens) against this session."""
    try:
        add = max(0, int(tokens or 0))
    except (TypeError, ValueError):
        add = 0
    state = _state_for(session, now=now)
    state["looks"] = int(state.get("looks") or 0) + 1
    state["tokens"] = int(state.get("tokens") or 0) + add
    table = _load_table()
    table[str(state.get("key") or "")] = state
    _save_table(table)
    return usage(session, now=now)
