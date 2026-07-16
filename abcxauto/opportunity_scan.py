"""Ideas-only opportunity strip for the agent cycle prompt.

Ranks a small liquid universe from MDA candles/quotes. Never places orders —
Grok still chooses hold vs bracket.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "ideas": []}
_CACHE_TTL_S = 150.0

_CORE = ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA")


def _universe(positions: list[dict] | None, *, cap: int = 10) -> list[str]:
    out: list[str] = []
    for p in positions or []:
        sym = str((p or {}).get("symbol") or "").upper().strip()
        if sym and sym not in out:
            out.append(sym)
    for sym in _CORE:
        if sym not in out:
            out.append(sym)
        if len(out) >= cap:
            break
    return out[:cap]


def _closes(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for row in candles or []:
        try:
            c = float(row.get("c"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            out.append(c)
    return out


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    window = values[-n:]
    return sum(window) / float(n)


def score_symbol(candles: list[dict], symbol: str) -> dict[str, Any] | None:
    """SMA pullback / short momentum score. Returns None if insufficient data."""
    closes = _closes(candles)
    if len(closes) < 30:
        return None
    last = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50) if len(closes) >= 50 else _sma(closes, 30)
    if sma20 is None or last <= 0:
        return None
    ret5 = (last / closes[-6] - 1.0) if len(closes) >= 6 else 0.0
    dist20 = (last - sma20) / sma20
    # Prefer mild pullback toward rising SMA (SPEC paper lane vibe).
    score = 0.0
    bias = "LONG"
    note = ""
    if sma50 and sma20 >= sma50 and -0.04 <= dist20 <= 0.01 and ret5 > -0.03:
        score = 0.55 + max(0.0, 0.04 + dist20) * 5.0 + max(0.0, ret5) * 2.0
        bias = "LONG"
        note = "SMA pullback / uptrend support"
    elif dist20 > 0.03 and ret5 > 0.01:
        score = 0.35 + min(0.25, ret5 * 3.0)
        bias = "LONG"
        note = "short momentum continuation"
    elif sma50 and sma20 < sma50 and dist20 > 0.02:
        score = 0.30 + min(0.2, dist20)
        bias = "SHORT"
        note = "below SMA50; fade extension"
    else:
        score = 0.15 + max(0.0, -abs(dist20)) * 0.5
        bias = "LONG" if dist20 >= 0 else "SHORT"
        note = "neutral / weak setup"
    # Prefer liquid index names slightly.
    if symbol in ("SPY", "QQQ"):
        score += 0.08
    stop_hint = 0.008 if bias == "LONG" else 0.008
    target_hint = 0.016 if bias == "LONG" else 0.016
    return {
        "symbol": symbol,
        "score": round(min(1.0, score), 3),
        "bias": bias,
        "note": note,
        "last": round(last, 4),
        "sma20": round(sma20, 4),
        "stop_hint_pct": stop_hint,
        "target_hint_pct": target_hint,
    }


def format_opportunities(ideas: list[dict[str, Any]], *, limit: int = 5) -> str:
    if not ideas:
        return "OPPORTUNITIES: (none — MDA thin or no setups)"
    lines = ["OPPORTUNITIES (ideas only — you choose hold or bracket):"]
    for i, idea in enumerate(ideas[:limit], 1):
        lines.append(
            f"{i}. {idea.get('symbol')} {idea.get('bias')} "
            f"score={idea.get('score')} — {idea.get('note')} "
            f"(stop~{float(idea.get('stop_hint_pct') or 0)*100:.1f}% / "
            f"target~{float(idea.get('target_hint_pct') or 0)*100:.1f}%)"
        )
    return "\n".join(lines)


async def scan_opportunities(
    positions: list[dict] | None = None,
    *,
    force: bool = False,
    cap: int = 10,
) -> list[dict[str, Any]]:
    """Fetch candles, score, return ranked ideas (cached ~2.5 min)."""
    symbols = _universe(positions, cap=cap)
    key = ",".join(symbols)
    now = time.monotonic()
    if (
        not force
        and _CACHE["ideas"]
        and _CACHE.get("key") == key
        and (now - float(_CACHE["ts"])) < _CACHE_TTL_S
    ):
        return list(_CACHE["ideas"])

    ideas: list[dict[str, Any]] = []
    try:
        from abcxauto.marketdata.client import get_marketdata_client

        client = get_marketdata_client()
        configured = getattr(client, "is_configured", False)
        if callable(configured):
            configured = configured()
        if not configured:
            _CACHE.update(ts=now, key=key, ideas=[])
            return []
        for sym in symbols:
            try:
                candles = await client.get_stock_candles(sym, days_back=120)
            except Exception:
                logger.exception("opportunity_scan candles failed for %s", sym)
                candles = []
            scored = score_symbol(candles or [], sym)
            if scored:
                ideas.append(scored)
    except Exception:
        logger.exception("scan_opportunities failed")
        ideas = []

    ideas.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    _CACHE.update(ts=now, key=key, ideas=list(ideas))
    return ideas


def reset_opportunity_cache() -> None:
    """Tests."""
    _CACHE.update(ts=0.0, key="", ideas=[])
