"""Research news package: mark prior headlines vs price asof.

Color only. Keep headline text. Never replace with error=clipped.
Do not grow SYSTEM_PROMPT.
"""

from __future__ import annotations

from typing import Any

from abcxauto.prints import parse_asof

PRIOR_S = 15 * 60.0


def mark_headlines(items: list, *, price_asof: str) -> list:
    """Stamp prior=True when published is more than 15m before price_asof."""
    clock = parse_asof(price_asof)
    out: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        published = parse_asof(row.get("published"))
        prior = False
        if clock is not None and published is not None:
            prior = (clock - published).total_seconds() > PRIOR_S
        row["prior"] = bool(prior)
        out.append(row)
    return out


def _unavailable(*, price_asof: str) -> dict[str, Any]:
    return {
        "news_asof": price_asof,
        "headlines": [],
        "news": "unavailable",
    }


def news_package(payload: dict | None, *, price_asof: str) -> dict:
    """Package news vs price clock. Timeout/empty/error => unavailable."""
    if not isinstance(payload, dict):
        return _unavailable(price_asof=price_asof)
    if payload.get("error"):
        return _unavailable(price_asof=price_asof)
    if payload.get("ok") is False:
        return _unavailable(price_asof=price_asof)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return _unavailable(price_asof=price_asof)
    headlines = mark_headlines(items, price_asof=price_asof)
    if not headlines:
        return _unavailable(price_asof=price_asof)
    return {
        "news_asof": price_asof,
        "headlines": headlines,
        "news": "ok",
    }
