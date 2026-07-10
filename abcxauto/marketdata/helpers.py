"""MarketData.app helpers — key normalize, candle resolution, error formatting."""

from __future__ import annotations

from typing import List, Optional

import httpx

API_BASE = "https://api.marketdata.app/v1"
# 429 is excluded: it means credits exhausted or per-second throttle — retry
# just burns more credits. Circuit breaker handles recovery via reset time.
OPTIONS_RETRY_STATUSES = {500, 502, 503, 504}
OPTIONS_MAX_CONCURRENCY = 3
OPTIONS_RETRY_ATTEMPTS = 3
MDA_MAX_CONCURRENT = 45  # MDA hard limit is 50; keep 5 headroom

# US regular session length used to convert "calendar lookback days" → bar count.
RTH_MINUTES = 390  # ~6.5h

def _normalize_mda_api_key(raw: Optional[str]) -> Optional[str]:
    """Strip whitespace; drop accidental ``Bearer `` prefix; trim wrapping quotes.

    A literal ``Bearer abc`` in ``.env`` would otherwise become
    ``Authorization: Bearer Bearer abc`` → HTTP 403 from MarketData.app.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("bearer "):
        s = s[7:].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s or None


def _format_mda_denied_body(response: httpx.Response) -> str:
    """Best-effort parse of MDA JSON error (e.g. multi-IP policy) for logs."""
    try:
        data = response.json()
    except Exception:
        return (response.text or "")[:500]
    if not isinstance(data, dict):
        return str(data)[:500]
    parts: List[str] = []
    if data.get("errmsg"):
        parts.append(str(data["errmsg"]))
    if data.get("authorizedIP") is not None or data.get("blockedIP") is not None:
        parts.append(
            f"authorizedIP={data.get('authorizedIP')!r} blockedIP={data.get('blockedIP')!r}"
        )
    if data.get("troubleshootingGuide"):
        parts.append(str(data["troubleshootingGuide"]))
    return " | ".join(parts) if parts else str(data)[:500]
# API hard limit: intraday range ≤ 1 year; keep a generous cap on countback.
_MAX_INTRADAY_COUNTBACK = 80_000


def normalize_mda_candle_resolution(resolution: str) -> str:
    """Map internal names (1min, 5min, 1h) to MarketData.app URL tokens.

    Docs: https://www.marketdata.app/docs/api/stocks/candles/
    Minute examples: ``1``, ``5``, ``15`` — not ``5min``.
    """
    if not resolution:
        return "D"
    key = resolution.strip().lower()
    aliases = {
        "1min": "1",
        "1m": "1",
        "minutely": "1",
        "3min": "3",
        "5min": "5",
        "5m": "5",
        "15min": "15",
        "15m": "15",
        "30min": "30",
        "30m": "30",
        "45min": "45",
        "45m": "45",
        "1h": "H",
        "60min": "H",
        "60m": "H",
        "hourly": "H",
        "daily": "D",
        "day": "D",
        "d": "D",
    }
    if key in aliases:
        return aliases[key]
    return resolution.strip()


def _resolution_countback_is_bars(norm: str) -> bool:
    """True when MDA interprets ``countback`` as N candles (intraday/time bars)."""
    n = norm.strip().upper()
    if n.isdigit():
        return True
    if n == "H" or (len(n) >= 2 and n.endswith("H") and n[:-1].isdigit()):
        return True
    return False


def _bars_per_rth_session(norm: str) -> int:
    """Approximate finished RTH bars per session for one symbol."""
    n = norm.strip().upper()
    if n == "H":
        return max(1, RTH_MINUTES // 60)
    if n.endswith("H") and n[:-1].isdigit():
        hrs = max(1, int(n[:-1]))
        return max(1, RTH_MINUTES // (60 * hrs))
    if n.isdigit():
        mins = max(1, int(n))
        return max(1, RTH_MINUTES // mins)
    # Unknown token — safest generic intraday assumption (~5m)
    return max(1, RTH_MINUTES // 5)


def intraday_countback_from_calendar_days(norm: str, calendar_days: int) -> int:
    """Turn ``return_lookback_days`` intent into MarketData ``countback`` (bars).

    Without this, ``countback=5`` on 5‑minute candles is only *five bars*
    (~25 minutes), which makes mature forward-return pairing look like endless
    "zombies" (score timestamps fall before the candle window).
    """
    cd = max(1, int(calendar_days))
    sessions = max(1, int(round(cd * 252 / 365)))
    per = _bars_per_rth_session(norm)
    base = sessions * per
    headroom = max(80, per * 4)
    return min(base + headroom, _MAX_INTRADAY_COUNTBACK)

