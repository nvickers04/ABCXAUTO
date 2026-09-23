"""Append-only look billing ledger.

One JSON line per finished model call. Session and week spend are sums of
those rows. No $0.20 estimate, turn count, tool count, or verdict.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
DEFAULT_PATH = _STATE_DIR / "look_ledger.jsonl"

# F10 dollars. Preferred is an ops tripwire; hard refuses new risk.
F10_PREFERRED_USD = 10.0
F10_HARD_USD = 15.0

_ET = ZoneInfo("America/New_York")


def default_path() -> Path:
    return DEFAULT_PATH


def _resolve(path: Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_PATH


def _finite_usd(raw: Any) -> float | None:
    """Finite billed USD ≥ 0, or None (missing / unreadable)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.lower() == "unknown":
            return None
        try:
            val = float(text)
        except ValueError:
            return None
    else:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(val) or val < 0:
        return None
    return val


def _store_usd(raw: Any) -> float | str:
    parsed = _finite_usd(raw)
    return parsed if parsed is not None else "unknown"


def _int_ge0(raw: Any) -> int:
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _parse_asof(asof: str) -> datetime | None:
    text = str(asof or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, tzinfo=_ET)
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt.astimezone(_ET)


def _iso_week_of(asof: str) -> str | None:
    dt = _parse_asof(asof)
    if dt is None:
        return None
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("look_ledger read failed", exc_info=True)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            blob = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(blob, dict):
            rows.append(blob)
    return rows


def _spend_flags(total: float | None, *, unknown: bool) -> dict[str, Any]:
    if unknown:
        return {
            "usd": None,
            "unknown": True,
            "preferred": False,
            "hard": True,
        }
    amount = float(total or 0.0)
    return {
        "usd": amount,
        "unknown": False,
        "preferred": amount > F10_PREFERRED_USD,
        "hard": amount > F10_HARD_USD,
    }


def _sum_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum finite usd rows. Skip unknown. Fail-closed only if all unknown."""
    if not rows:
        return _spend_flags(0.0, unknown=False)
    total = 0.0
    finite = 0
    for row in rows:
        parsed = _finite_usd(row.get("usd"))
        if parsed is None:
            continue
        total += parsed
        finite += 1
    if finite == 0:
        return _spend_flags(None, unknown=True)
    return _spend_flags(total, unknown=False)


def append_call(
    *,
    asof: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    usd: Any,
    session: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one usage row. Returns the written object."""
    target = _resolve(path)
    row: dict[str, Any] = {
        "asof": str(asof or "").strip(),
        "model": str(model or "").strip(),
        "input_tokens": _int_ge0(input_tokens),
        "output_tokens": _int_ge0(output_tokens),
        "usd": _store_usd(usd),
        "session": str(session or "").strip(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return row


def session_spend(session_day: str, *, path: Path | None = None) -> dict[str, Any]:
    """Sum usd for rows whose asof starts with ``session_day`` (YYYY-MM-DD)."""
    day = str(session_day or "").strip()
    rows = [
        row
        for row in _read_rows(_resolve(path))
        if str(row.get("asof") or "").startswith(day)
    ]
    return _sum_rows(rows)


def week_spend(iso_week: str, *, path: Path | None = None) -> dict[str, Any]:
    """Sum usd for rows whose asof falls in ``iso_week`` (ET, ``YYYY-Www``)."""
    week = str(iso_week or "").strip()
    rows = [
        row
        for row in _read_rows(_resolve(path))
        if _iso_week_of(str(row.get("asof") or "")) == week
    ]
    return _sum_rows(rows)


def latest_row(path: Path | None = None) -> dict[str, Any] | None:
    """Last JSON object in the ledger, or None when empty/missing."""
    rows = _read_rows(_resolve(path))
    return rows[-1] if rows else None
