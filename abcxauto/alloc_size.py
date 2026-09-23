"""Allocation share targets from NetLiq risk and notional caps.

Bet size is RISK_PCT / NOTIONAL_CAP_PCT here — not a desk risk-config knob.
These are trim facts only; they do not arm send refuses.
"""

from __future__ import annotations

import math
from typing import Any

RISK_PCT = 1.0
NOTIONAL_CAP_PCT = 25.0
HEAT_CAP_PCT = 6.0


def _pos_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def target_shares(
    *,
    nl: float,
    last: float,
    stop: float | None,
    risk_pct: float = RISK_PCT,
    notional_cap_pct: float = NOTIONAL_CAP_PCT,
) -> int:
    """Floor shares by 1% risk and 25% notional; lesser wins, floor at 0."""
    nl_v = _pos_float(nl)
    last_v = _pos_float(last)
    if nl_v is None or last_v is None:
        return 0
    notional = int(math.floor(nl_v * float(notional_cap_pct) / 100.0 / last_v))
    if notional < 0:
        notional = 0

    stop_v = _finite(stop)
    if stop_v is None:
        return notional

    dist = abs(last_v - stop_v)
    if dist <= 0:
        return notional

    risk = int(math.floor(nl_v * float(risk_pct) / 100.0 / dist))
    if risk < 0:
        risk = 0
    return min(risk, notional)


def heat_pct_of(lots: list[dict], nl: float) -> float:
    """Stop-to-last dollar heat as a percent of NetLiq (0–100 scale)."""
    nl_v = _pos_float(nl)
    if nl_v is None:
        return 0.0
    dollars = 0.0
    for lot in lots or []:
        if not isinstance(lot, dict):
            continue
        qty = _finite(lot.get("qty"))
        last = _finite(lot.get("last"))
        stop = _finite(lot.get("stop"))
        if qty is None or last is None or stop is None:
            continue
        dollars += abs(qty) * abs(last - stop)
    return 100.0 * dollars / nl_v


def heat_pct(lots: list[dict], nl: float) -> float:
    """Alias: heat dollars / nl as percent. ``nl`` is passed separately."""
    return heat_pct_of(lots, nl)


def sized_book(
    snapshot: dict,
    scores: dict[str, dict],
    groups: list[list[str]] | None = None,
) -> dict:
    """Per held name: target shares, excess to trim, or None when unaligned.

    ``groups`` is accepted for callers that already have heat cohorts; sizing
    here is per-name (risk + notional). Heat is measured via ``heat_pct_of``.
    """
    _ = groups
    snap = snapshot if isinstance(snapshot, dict) else {}
    names = snap.get("names") if isinstance(snap.get("names"), dict) else {}
    nl = _pos_float(snap.get("nl"))
    score_map = scores if isinstance(scores, dict) else {}

    out: dict[str, dict] = {}
    for symbol, row in names.items():
        if not isinstance(row, dict):
            continue
        qty_raw = _finite(row.get("qty"))
        held = int(qty_raw) if qty_raw is not None else 0
        if held <= 0:
            continue

        last = _finite(row.get("last"))
        stop = _finite(row.get("stop"))
        aligned = bool(row.get("aligned"))
        score = score_map.get(symbol) if isinstance(score_map.get(symbol), dict) else {}
        vs_spy = _finite(score.get("vs_spy")) if score else None

        if not aligned or last is None or nl is None:
            out[str(symbol)] = {
                "held": held,
                "sized": None,
                "excess": None,
                "vs_spy": vs_spy,
                "last": last,
                "stop": stop,
            }
            continue

        sized = target_shares(nl=nl, last=last, stop=stop)
        excess = max(0, held - sized)
        out[str(symbol)] = {
            "held": held,
            "sized": sized,
            "excess": excess,
            "vs_spy": vs_spy,
            "last": last,
            "stop": stop,
        }
    return out
