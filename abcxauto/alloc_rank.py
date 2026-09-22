"""Relative-strength style ranking from closes vs SPY. Pure functions only.

Not an IBD 1-99 rating. No broker I/O.
"""

from __future__ import annotations

import math
from typing import Any

# Lookback sessions and fixed weights when the full year is present.
_WINDOWS: tuple[tuple[int, float], ...] = (
    (63, 0.4),
    (126, 0.2),
    (189, 0.2),
    (252, 0.2),
)
_HEAT_TAIL = 60


def _common_sessions(
    closes_by_date: dict[str, float],
    spy_by_date: dict[str, float],
) -> list[str]:
    return sorted(set(closes_by_date) & set(spy_by_date))


def _asof_close(series: dict[str, float], on_or_before: str) -> float | None:
    """Close on ``on_or_before``, or the series' latest date at or before it."""
    if on_or_before in series:
        try:
            v = float(series[on_or_before])
        except (TypeError, ValueError):
            return None
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    best: str | None = None
    for d in series:
        if d <= on_or_before and (best is None or d > best):
            best = d
    if best is None:
        return None
    try:
        v = float(series[best])
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _roc_at(
    series: dict[str, float],
    sessions: list[str],
    t_idx: int,
    n: int,
) -> float | None:
    """ROC(n) = close[t] / close[t-n] - 1 using session calendar + asof lookup."""
    if n <= 0 or t_idx < n:
        return None
    t = sessions[t_idx]
    anchor = sessions[t_idx - n]
    close_t = _asof_close(series, t)
    close_n = _asof_close(series, anchor)
    if close_t is None or close_n is None or close_n == 0.0:
        return None
    return close_t / close_n - 1.0


def _score_on_windows(
    series: dict[str, float],
    sessions: list[str],
    t_idx: int,
    windows: list[int],
) -> float | None:
    wmap = dict(_WINDOWS)
    parts: list[tuple[float, float]] = []
    for n in windows:
        roc = _roc_at(series, sessions, t_idx, n)
        if roc is None:
            continue
        parts.append((wmap[n], roc))
    if not parts:
        return None
    wsum = sum(w for w, _ in parts)
    if wsum <= 0:
        return None
    return sum(w * r for w, r in parts) / wsum


def score_name(
    closes_by_date: dict[str, float],
    spy_by_date: dict[str, float],
) -> dict[str, Any]:
    """Weighted multi-horizon ROC vs SPY on shared session dates.

    ROC(n) uses the latest date present in both maps as ``t``. ``t-n`` is each
    series' own close on or before the session n trading days earlier. Sessions
    are the sorted common dates. Missing long windows set ``partial=True``.
    """
    sessions = _common_sessions(closes_by_date, spy_by_date)
    if not sessions:
        return {"vs_spy": None, "partial": True, "windows": [], "score": None}

    t_idx = len(sessions) - 1
    shared: list[int] = []
    for n, _weight in _WINDOWS:
        if (
            _roc_at(closes_by_date, sessions, t_idx, n) is not None
            and _roc_at(spy_by_date, sessions, t_idx, n) is not None
        ):
            shared.append(n)

    partial = t_idx < 252 or len(shared) < len(_WINDOWS)
    if not shared:
        return {"vs_spy": None, "partial": True, "windows": [], "score": None}

    name_score = _score_on_windows(closes_by_date, sessions, t_idx, shared)
    spy_score = _score_on_windows(spy_by_date, sessions, t_idx, shared)
    if name_score is None or spy_score is None:
        return {"vs_spy": None, "partial": True, "windows": [], "score": None}

    return {
        "vs_spy": name_score - spy_score,
        "partial": partial,
        "windows": shared,
        "score": name_score,
    }


def rank_board(scores: dict[str, dict]) -> list[dict[str, Any]]:
    """Sort names by ``vs_spy`` descending. Rank starts at 1. No 1-99 rating."""
    universe = len(scores)
    rows: list[tuple[float, str, dict]] = []
    for symbol, payload in scores.items():
        if not isinstance(payload, dict):
            continue
        vs = payload.get("vs_spy")
        if vs is None:
            continue
        try:
            vs_f = float(vs)
        except (TypeError, ValueError):
            continue
        if vs_f != vs_f or vs_f in (float("inf"), float("-inf")):
            continue
        rows.append((vs_f, str(symbol), payload))
    rows.sort(key=lambda r: (-r[0], r[1]))
    out: list[dict[str, Any]] = []
    for i, (vs_f, symbol, payload) in enumerate(rows, start=1):
        out.append(
            {
                "symbol": symbol,
                "vs_spy": vs_f,
                "partial": bool(payload.get("partial")),
                "rank": i,
                "universe": universe,
            }
        )
    return out


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 2 or n != len(b):
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    num = 0.0
    da = 0.0
    db = 0.0
    for i in range(n):
        xa = a[i] - ma
        xb = b[i] - mb
        num += xa * xb
        da += xa * xa
        db += xb * xb
    if da <= 0.0 or db <= 0.0:
        return None
    return num / math.sqrt(da * db)


def _tail(xs: list[float]) -> list[float]:
    if len(xs) > _HEAT_TAIL:
        return list(xs[-_HEAT_TAIL:])
    return list(xs)


def heat_groups(
    returns_by_symbol: dict[str, list[float]],
    threshold: float = 0.7,
) -> list[list[str]]:
    """Connected components of names with Pearson corr > threshold (last 60)."""
    symbols = sorted(str(s) for s in returns_by_symbol)
    series: dict[str, list[float]] = {}
    for sym, raw in returns_by_symbol.items():
        key = str(sym)
        if not raw:
            series[key] = []
            continue
        try:
            series[key] = _tail([float(x) for x in raw])
        except (TypeError, ValueError):
            series[key] = []

    parent = {s: s for s in symbols}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(symbols):
        sa = series.get(a) or []
        if len(sa) < 2:
            continue
        for b in symbols[i + 1 :]:
            sb = series.get(b) or []
            if len(sb) < 2:
                continue
            m = min(len(sa), len(sb))
            corr = _pearson(sa[-m:], sb[-m:])
            if corr is not None and corr > threshold:
                union(a, b)

    buckets: dict[str, list[str]] = {}
    for s in symbols:
        buckets.setdefault(find(s), []).append(s)
    groups = [sorted(members) for members in buckets.values()]
    groups.sort(key=lambda g: (g[0], len(g)))
    return groups
