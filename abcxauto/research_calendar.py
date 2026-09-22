"""Parse IBKR CalendarReport text; thin async fetch. Never invent dates."""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{8})")
_EARNINGS_RE = re.compile(r"earnings", re.IGNORECASE)
# Window around "earnings" — a bare word alone is not a date.
_NEAR = 48


def _unknown(*, calendar_asof: str = "") -> dict[str, Any]:
    return {
        "earnings": "unknown",
        "earnings_in": None,
        "ex_div": None,
        "calendar_asof": calendar_asof,
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ymd(raw: str) -> Optional[date]:
    text = str(raw or "").strip()
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text)
        if len(text) == 8 and text.isdigit():
            return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None
    return None


def _session_distance(earnings_day: date, today_day: date) -> int:
    """NYSE-ish session distance ≈ calendar-day difference // 1."""
    delta = (earnings_day - today_day).days
    return delta if delta < 0 else max(0, delta)


def parse_calendar_report(text: str, *, today: str) -> dict:
    """Pull an earnings date only when YYYY-MM-DD / YYYYMMDD sits near 'earnings'."""
    blob = str(text or "")
    today_day = _parse_ymd(today)
    if today_day is None:
        return _unknown(calendar_asof="")

    for hit in _EARNINGS_RE.finditer(blob):
        lo = max(0, hit.start() - _NEAR)
        hi = min(len(blob), hit.end() + _NEAR)
        window = blob[lo:hi]
        for dm in _DATE_RE.finditer(window):
            earnings_day = _parse_ymd(dm.group(1))
            if earnings_day is None:
                continue
            return {
                "earnings": earnings_day.isoformat(),
                "earnings_in": _session_distance(earnings_day, today_day),
                "ex_div": None,
                "calendar_asof": "",
            }

    return _unknown(calendar_asof="")


async def fetch_calendar(connector: Any, symbol: str, *, today: str) -> dict:
    """Call connector fundamental CalendarReport when present; never invent a date."""
    miss = _unknown(calendar_asof=_iso_now())
    if connector is None:
        return miss

    fn = getattr(connector, "req_fundamental", None) or getattr(
        connector, "get_fundamental", None
    )
    if not callable(fn):
        return miss

    try:
        try:
            raw = fn(str(symbol or "").strip().upper(), report="CalendarReport")
        except TypeError:
            raw = fn(str(symbol or "").strip().upper(), "CalendarReport")
        if inspect.isawaitable(raw):
            raw = await raw
    except Exception:
        return miss

    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("data") or raw.get("report") or "")
        if not text:
            text = str(raw)
    else:
        text = str(raw or "")

    if not text.strip():
        return miss

    try:
        parsed = parse_calendar_report(text, today=today)
    except Exception:
        return miss
    parsed["calendar_asof"] = _iso_now()
    return parsed


def implied_move_needed(earnings_in: Any) -> bool:
    """True only when earnings_in is an int and 0 <= earnings_in <= 5."""
    return isinstance(earnings_in, int) and not isinstance(earnings_in, bool) and 0 <= earnings_in <= 5
