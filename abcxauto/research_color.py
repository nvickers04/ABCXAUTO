"""Scan / web / odds / spend color fields. Not send geometry. Not a brief."""

from __future__ import annotations

from typing import Any


def _sym(raw: Any) -> str:
    return str(raw or "").upper().strip()


def _finite(raw: Any) -> float | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return val


def _scan_asof(row: dict[str, Any]) -> str:
    for key in ("scan_asof", "asof_iso", "asof"):
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        return str(raw)
    ibkr = row.get("ibkr")
    if isinstance(ibkr, dict):
        for key in ("asof_iso", "asof"):
            raw = ibkr.get(key)
            if raw is None or raw == "":
                continue
            return str(raw)
    return ""


def _row_gap(row: dict[str, Any]) -> Any:
    """Real open_gap_pct / gap% only. Missing is unavailable, not 0."""
    for key in ("open_gap_pct", "gap%"):
        if key not in row:
            continue
        val = _finite(row.get(key))
        if val is None:
            continue
        return val
    return "unavailable"


def _row_vs_open(row: dict[str, Any]) -> Any:
    if "vs_open" not in row:
        return "unavailable"
    val = _finite(row.get("vs_open"))
    if val is None:
        return "unavailable"
    return val


def scan_gap(rows: list, symbol: str) -> dict:
    """Gap color from a scan row. Never copies row last into the result."""
    want = _sym(symbol)
    hit: dict[str, Any] | None = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if _sym(row.get("symbol")) == want:
            hit = row
            break
    if hit is None:
        return {"scan_asof": "", "gap": "unavailable", "vs_open": "unavailable"}
    return {
        "scan_asof": _scan_asof(hit),
        "gap": _row_gap(hit),
        "vs_open": _row_vs_open(hit),
    }


def color_unavailable() -> dict:
    return {
        "web": "unavailable",
        "web_asof": "",
        "odds": "unavailable",
        "odds_asof": "",
        "citations": [],
    }


def spend_slot(ledger_row: dict | None) -> dict:
    """Map a look-ledger row to spend color. Does not read research_brief.json."""
    if not isinstance(ledger_row, dict):
        return {"spend_asof": "", "session_usd": "unavailable"}
    asof = ledger_row.get("asof")
    usd = ledger_row.get("usd")
    if asof is None or asof == "" or usd is None:
        return {"spend_asof": "", "session_usd": "unavailable"}
    return {"spend_asof": asof, "session_usd": usd}
