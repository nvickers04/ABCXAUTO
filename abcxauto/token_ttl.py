"""KEEP-4: unused dry-run / approval / place tokens expire fail-closed.

KEEP-3 (dry-run preview tokens) should call these helpers rather than
invent a second clock. A token that sits past TTL cannot authorize
``send``. Exits are never blocked. Looking and 7496 stay off.
"""

from __future__ import annotations

import logging
import math
import os
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

# Two minutes. Long enough for a preview → place, short enough that an
# unused authorization cannot sit a session. Operator env is clamped.
DEFAULT_PLACE_TOKEN_TTL_S = 120.0
MIN_PLACE_TOKEN_TTL_S = 5.0
MAX_PLACE_TOKEN_TTL_S = 900.0
TOKEN_TTL_ENV = "ABCXAUTO_PLACE_TOKEN_TTL_S"
TOKEN_TTL_ENV_ALIAS = "ABCXAUTO_TOKEN_TTL_S"

REASON_TOKEN_EXPIRED = "token_expired"
REASON_TOKEN_INVALID = "token_invalid"
REASON_TOKEN_USED = "token_used"

TOKEN_KINDS = frozenset({"dry_run", "preview", "approval", "place"})
TOKEN_ACTION_KEYS = (
    "place_token",
    "approval_token",
    "dry_run_token",
    "preview_token",
    "_place_token",
)

# Management / cover — token TTL must not hold an exit.
_EXIT_OR_MANAGE = frozenset({
    "oca",
    "modify_stop",
    "modify_target",
    "cancel_order",
    "close_option",
    "trailing_stop",
    "trailing_stop_limit",
    "roll_option",
})

_ABSENT = object()
_store: dict[str, dict[str, Any]] = {}


def reset_place_tokens_for_tests() -> None:
    """Drop the in-process token store. Tests only."""
    _store.clear()


def _finite_positive(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n <= 0:
        return None
    return n


def _unix(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def token_ttl_seconds(override: Any = None) -> float:
    """Configured unused-token TTL. Unreadable / 0 / off → safe default.

    Never returns 0 or infinity. A huge override clamps to
    ``MAX_PLACE_TOKEN_TTL_S`` so authorization cannot sit forever.
    """
    raw: Any
    if override is not None:
        raw = override
    else:
        raw = os.environ.get(TOKEN_TTL_ENV)
        if raw in (None, ""):
            raw = os.environ.get(TOKEN_TTL_ENV_ALIAS)
    parsed = _finite_positive(raw) if raw not in (None, "") else None
    if parsed is None:
        return DEFAULT_PLACE_TOKEN_TTL_S
    return min(MAX_PLACE_TOKEN_TTL_S, max(MIN_PLACE_TOKEN_TTL_S, parsed))


def expires_at(issued_at: Any, ttl_s: Any = None) -> float:
    """Unix expiry for an issued token. Unreadable issue time → already dead."""
    issued = _unix(issued_at)
    if issued is None:
        return 0.0
    return issued + token_ttl_seconds(ttl_s)


def token_expired(
    issued_at: Any = None,
    *,
    expires_at: Any = None,
    now: Any = None,
    ttl_s: Any = None,
) -> bool:
    """True when the token is dead. Missing / unreadable clock → expired."""
    clock = _unix(now) if now is not None else time.time()
    if clock is None:
        return True
    exp = _unix(expires_at)
    if exp is None:
        issued = _unix(issued_at)
        if issued is None:
            return True
        exp = issued + token_ttl_seconds(ttl_s)
    return clock >= exp


def stamp_token(
    record: dict[str, Any] | None,
    *,
    now: Any = None,
    ttl_s: Any = None,
) -> dict[str, Any]:
    """Write issued_at / ttl_s / expires_at onto a KEEP-3 token record."""
    blob = dict(record) if isinstance(record, dict) else {}
    clock = _unix(now)
    if clock is None:
        clock = time.time()
    ttl = token_ttl_seconds(ttl_s if ttl_s is not None else blob.get("ttl_s"))
    issued = _unix(blob.get("issued_at"))
    if issued is None:
        issued = clock
    blob["issued_at"] = issued
    blob["ttl_s"] = ttl
    exp = _unix(blob.get("expires_at"))
    if exp is None:
        exp = issued + ttl
    blob["expires_at"] = exp
    return blob


def issue_place_token(
    *,
    kind: str = "place",
    payload: Any = None,
    ttl_s: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Mint a one-shot token into the process store. KEEP-3 may call this."""
    label = str(kind or "place").strip().lower() or "place"
    if label not in TOKEN_KINDS:
        label = "place"
    clock = _unix(now)
    if clock is None:
        clock = time.time()
    ttl = token_ttl_seconds(ttl_s)
    token_id = secrets.token_hex(16)
    rec = {
        "id": token_id,
        "kind": label,
        "issued_at": clock,
        "ttl_s": ttl,
        "expires_at": clock + ttl,
        "used": False,
        "payload": payload,
    }
    _store[token_id] = rec
    return dict(rec)


def peek_place_token(token_id: Any) -> dict[str, Any] | None:
    """Copy of a stored token, or None. Does not consume."""
    key = str(token_id or "").strip()
    if not key:
        return None
    rec = _store.get(key)
    return dict(rec) if isinstance(rec, dict) else None


def _resolve_token(token: Any) -> dict[str, Any] | None:
    if token is None or token is _ABSENT:
        return None
    if isinstance(token, str):
        key = token.strip()
        if not key:
            return None
        rec = _store.get(key)
        return dict(rec) if isinstance(rec, dict) else None
    if not isinstance(token, dict):
        return None
    key = str(token.get("id") or "").strip()
    if key and key in _store:
        stored = _store[key]
        return dict(stored) if isinstance(stored, dict) else None
    if not token:
        return None
    return stamp_token(token)


def evaluate_place_token(token: Any, *, now: Any = None) -> dict[str, Any]:
    """Verdict for a store id or inline record. Fail-closed on garbage."""
    record = _resolve_token(token)
    if record is None:
        return {
            "ok": False,
            "reason": REASON_TOKEN_INVALID,
            "record": None,
        }
    if record.get("used") is True:
        return {
            "ok": False,
            "reason": REASON_TOKEN_USED,
            "record": record,
        }
    if token_expired(
        record.get("issued_at"),
        expires_at=record.get("expires_at"),
        now=now,
        ttl_s=record.get("ttl_s"),
    ):
        return {
            "ok": False,
            "reason": REASON_TOKEN_EXPIRED,
            "record": record,
        }
    return {"ok": True, "reason": "ok", "record": record}


def consume_place_token(token: Any, *, now: Any = None) -> dict[str, Any]:
    """One-shot consume. Expired / used / missing cannot place."""
    verdict = evaluate_place_token(token, now=now)
    if not verdict.get("ok"):
        return verdict
    record = verdict.get("record")
    if isinstance(record, dict):
        key = str(record.get("id") or "").strip()
        if key and key in _store:
            _store[key]["used"] = True
            record = dict(_store[key])
            verdict["record"] = record
    return verdict


def extract_action_token(action: Any) -> Any:
    """Token on the ticket, or a sentinel when no key was offered."""
    if not isinstance(action, dict):
        return _ABSENT
    for key in TOKEN_ACTION_KEYS:
        if key in action:
            return action[key]
    params = action.get("params")
    if isinstance(params, dict):
        for key in TOKEN_ACTION_KEYS:
            if key in params:
                return params[key]
    return _ABSENT


def ticket_is_exit(action: Any) -> bool:
    """True for cover / manage. Token TTL must not hold these."""
    if not isinstance(action, dict):
        return False
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    if params.get("closing_position") is True:
        return True
    strat = str(action.get("strategy") or action.get("action") or "").strip().lower()
    return strat in _EXIT_OR_MANAGE


def place_token_block(action: Any, *, now: Any = None) -> dict[str, Any] | None:
    """Send-path gate. No token on the ticket → None (KEEP-3 not required).

    A presented token that is expired, used, or unreadable blocks place.
    Exits skip the gate.
    """
    marker = extract_action_token(action)
    if marker is _ABSENT:
        return None
    if ticket_is_exit(action):
        return None
    verdict = consume_place_token(marker, now=now)
    if verdict.get("ok"):
        return None
    reason = str(verdict.get("reason") or REASON_TOKEN_INVALID)
    strat = ""
    if isinstance(action, dict):
        strat = str(action.get("strategy") or action.get("action") or "")
    notes = {
        REASON_TOKEN_EXPIRED: (
            "place token expired — unused authorization is dead"
        ),
        REASON_TOKEN_USED: "place token already used",
        REASON_TOKEN_INVALID: "place token missing or unreadable — fail-closed",
    }
    logger.warning("KEEP-4 token gate %s — not placing", reason)
    return {
        "status": "blocked",
        "reason_code": reason,
        "note": notes.get(reason, "place token refused"),
        "strategy": strat or "blocked",
    }


__all__ = [
    "DEFAULT_PLACE_TOKEN_TTL_S",
    "MAX_PLACE_TOKEN_TTL_S",
    "MIN_PLACE_TOKEN_TTL_S",
    "REASON_TOKEN_EXPIRED",
    "REASON_TOKEN_INVALID",
    "REASON_TOKEN_USED",
    "TOKEN_ACTION_KEYS",
    "TOKEN_KINDS",
    "TOKEN_TTL_ENV",
    "consume_place_token",
    "evaluate_place_token",
    "expires_at",
    "extract_action_token",
    "issue_place_token",
    "peek_place_token",
    "place_token_block",
    "reset_place_tokens_for_tests",
    "stamp_token",
    "ticket_is_exit",
    "token_expired",
    "token_ttl_seconds",
]
