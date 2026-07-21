"""Phase 5C KPI rollups from journal + structure_events (operator aids)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from abcxauto.structure_grade import recent_structure_lessons

_REPO = Path(__file__).resolve().parents[1]
_ENTRY_STRATS = frozenset({"bracket", "market_bracket"})
_SCRAPE_MARKERS = ("scrape",)
_GEOMETRY_MARKERS = ("geometry_",)


def _parse_day(ts: str | None) -> date | None:
    if not ts:
        return None
    raw = str(ts).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    except ValueError:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return None


def _in_window(ts: str | None, start: date, end: date) -> bool:
    d = _parse_day(ts)
    return d is not None and start <= d <= end


def load_structure_events(
    path: Path | None = None, *, limit: int = 500
) -> list[dict[str, Any]]:
    p = path or (_REPO / "structure_events.jsonl")
    if not p.is_file():
        return recent_structure_lessons(limit)
    rows: list[dict[str, Any]] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return recent_structure_lessons(limit)
    for line in lines[-max(1, limit) :]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def structure_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    scrapes = 0
    geometry = 0
    ok_hunts = 0
    for e in events:
        blob = " ".join(
            str(e.get(k) or "")
            for k in ("outcome", "reason_code", "message", "source")
        ).lower()
        if any(m in blob for m in _SCRAPE_MARKERS):
            scrapes += 1
        if any(m in blob for m in _GEOMETRY_MARKERS):
            geometry += 1
        if str(e.get("outcome") or "").lower() in ("ok", "pass", "accepted"):
            strat = str(e.get("strategy") or "").lower()
            if strat in _ENTRY_STRATS or e.get("source") == "cycle":
                ok_hunts += 1
    return {
        "n_events": len(events),
        "scrapes": scrapes,
        "geometry": geometry,
        "ok_hunts": ok_hunts,
    }


def hunt_symbols(decisions: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for d in decisions:
        strat = str(d.get("strategy") or d.get("action") or "").lower()
        if strat not in _ENTRY_STRATS:
            continue
        sym = ""
        outcome = d.get("outcome") or {}
        if isinstance(outcome, dict):
            j = outcome.get("judgment") or {}
            intent = j.get("intent") if isinstance(j, dict) else {}
            if isinstance(intent, dict):
                sym = str(intent.get("symbol") or "")
        if not sym:
            port = d.get("portfolio_snapshot") or {}
            if isinstance(port, dict):
                sym = str(port.get("symbol") or "")
        rat = str(d.get("rationale") or "")
        if not sym and rat:
            # last resort: uppercase ticker-like token in rationale
            for tok in rat.replace(",", " ").split():
                t = tok.strip(".:;()[]").upper()
                if 1 < len(t) <= 5 and t.isalpha():
                    sym = t
                    break
        sym = sym.upper().strip()
        if sym and sym not in seen:
            seen.append(sym)
    return seen


def gate_block_notes(decisions: list[dict[str, Any]], limit: int = 12) -> list[str]:
    notes: list[str] = []
    for d in decisions:
        strat = str(d.get("strategy") or "").lower()
        outcome = d.get("outcome") or {}
        status = ""
        note = ""
        if isinstance(outcome, dict):
            status = str(outcome.get("status") or "")
            note = str(outcome.get("note") or outcome.get("reason") or "")
        rat = str(d.get("rationale") or "")
        blob = f"{strat} {status} {note} {rat}".lower()
        if "blocked" in blob or "rejected" in blob or "gate" in blob:
            line = (note or rat or status or strat)[:120]
            if line and line not in notes:
                notes.append(line)
        if len(notes) >= limit:
            break
    return notes


def idle_quality(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    idle = [j for j in judgments if str(j.get("stance") or "").lower() == "idle"]
    with_dismiss = sum(1 for j in idle if str(j.get("dismissed") or "").strip())
    return {
        "idle_n": len(idle),
        "idle_with_dismiss": with_dismiss,
        "idle_dismiss_pct": (
            round(100.0 * with_dismiss / len(idle), 1) if idle else None
        ),
    }


def stance_counts(judgments: list[dict[str, Any]]) -> dict[str, int]:
    c: Counter[str] = Counter(
        str(j.get("stance") or "?").lower() for j in judgments
    )
    return dict(c)


def build_day_report(
    *,
    journal: Any,
    day: date | None = None,
    structure_path: Path | None = None,
    decision_limit: int = 80,
    judgment_limit: int = 80,
) -> dict[str, Any]:
    """Roll up Phase 5 KPIs for one UTC calendar day (default: today)."""
    day = day or datetime.now(timezone.utc).date()
    judgments = [
        j
        for j in (journal.recent_judgments(judgment_limit) or [])
        if _in_window(j.get("ts"), day, day)
    ]
    decisions = [
        d
        for d in (journal.recent_decisions(decision_limit) or [])
        if _in_window(d.get("ts"), day, day)
    ]
    events = [
        e
        for e in load_structure_events(structure_path, limit=400)
        if _in_window(e.get("ts"), day, day)
    ]
    sc = structure_counts(events)
    entries = sum(
        1
        for d in decisions
        if str(d.get("strategy") or d.get("action") or "").lower() in _ENTRY_STRATS
    )
    hunts = hunt_symbols(decisions)
    scrape_rate = (
        round(100.0 * sc["scrapes"] / entries, 1) if entries else None
    )
    return {
        "day": day.isoformat(),
        "n_judgments": len(judgments),
        "n_decisions": len(decisions),
        "stances": stance_counts(judgments),
        "entries": entries,
        "structure": sc,
        "scrape_rate_pct": scrape_rate,
        "hunt_symbols": hunts,
        "gate_blocks": gate_block_notes(decisions),
        "idle": idle_quality(judgments),
        "strategy_diversity": journal.strategy_diversity(limit=40),
        "thesis": (journal.get_working_thesis() or "")[:240],
    }


def build_week_report(
    *,
    journal: Any,
    end: date | None = None,
    days: int = 7,
    structure_path: Path | None = None,
) -> dict[str, Any]:
    end = end or datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, days) - 1)
    judgments = [
        j
        for j in (journal.recent_judgments(200) or [])
        if _in_window(j.get("ts"), start, end)
    ]
    decisions = [
        d
        for d in (journal.recent_decisions(200) or [])
        if _in_window(d.get("ts"), start, end)
    ]
    events = [
        e
        for e in load_structure_events(structure_path, limit=800)
        if _in_window(e.get("ts"), start, end)
    ]
    sc = structure_counts(events)
    entries = sum(
        1
        for d in decisions
        if str(d.get("strategy") or d.get("action") or "").lower() in _ENTRY_STRATS
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_judgments": len(judgments),
        "n_decisions": len(decisions),
        "stances": stance_counts(judgments),
        "entries": entries,
        "structure": sc,
        "scrape_rate_pct": (
            round(100.0 * sc["scrapes"] / entries, 1) if entries else None
        ),
        "hunt_symbols": hunt_symbols(decisions),
        "gate_blocks": gate_block_notes(decisions, limit=20),
        "idle": idle_quality(judgments),
        "strategy_diversity": journal.strategy_diversity(limit=80),
    }


def format_day_report(report: dict[str, Any]) -> str:
    sc = report.get("structure") or {}
    idle = report.get("idle") or {}
    div = report.get("strategy_diversity") or {}
    lines = [
        f"=== Phase 5 day report ({report.get('day')}) ===",
        f"Judgments: {report.get('n_judgments')}  Decisions: {report.get('n_decisions')}",
        f"Stances: {report.get('stances')}",
        f"Entries (bracket*): {report.get('entries')}",
        (
            f"Structure: scrapes={sc.get('scrapes')} geometry={sc.get('geometry')} "
            f"ok_hunts={sc.get('ok_hunts')} events={sc.get('n_events')}"
        ),
        (
            f"Scrape rate: {report.get('scrape_rate_pct') if report.get('scrape_rate_pct') is not None else 'n/a'}"
            f" (target falling; <20% by window end)"
        ),
        f"Hunt symbols: {report.get('hunt_symbols') or '(none)'}",
        (
            f"Idle: {idle.get('idle_n')} with dismiss={idle.get('idle_with_dismiss')} "
            f"({idle.get('idle_dismiss_pct') if idle.get('idle_dismiss_pct') is not None else 'n/a'}%)"
        ),
        f"Strategy mix: {div.get('n_distinct')} - {div.get('strategies')}",
        f"Thesis: {report.get('thesis') or '(none)'}",
        "Gate/block notes:",
    ]
    blocks = report.get("gate_blocks") or []
    if not blocks:
        lines.append("  (none)")
    else:
        for b in blocks:
            lines.append(f"  - {b}")
    rate = report.get("scrape_rate_pct")
    rate_s = f"{rate}%" if rate is not None else "n/a"
    lines.extend(
        [
            "",
            "--- 5B paste skeleton ---",
            f"Date: {report.get('day')} | Cycles: {report.get('n_judgments')} | Posture: (card)",
            "1. Survive: ",
            (
                f"2. Structure: scrapes={sc.get('scrapes')} "
                f"geometry={sc.get('geometry')} ok_hunts={sc.get('ok_hunts')} "
                f"rate={rate_s}"
            ),
            "3. Judgment: ",
            "4. Book: ",
            "5. Next: ",
        ]
    )
    return "\n".join(lines)


def format_week_report(report: dict[str, Any]) -> str:
    sc = report.get("structure") or {}
    lines = [
        f"=== Phase 5 week report ({report.get('start')} → {report.get('end')}) ===",
        f"Judgments: {report.get('n_judgments')}  Decisions: {report.get('n_decisions')}",
        f"Stances: {report.get('stances')}",
        f"Entries: {report.get('entries')}  scrape_rate={report.get('scrape_rate_pct')}%",
        (
            f"Structure: scrapes={sc.get('scrapes')} geometry={sc.get('geometry')} "
            f"ok_hunts={sc.get('ok_hunts')}"
        ),
        f"Hunt symbols (≥2 if #1 cooling): {report.get('hunt_symbols') or '(none)'}",
        f"Idle quality: {report.get('idle')}",
        f"Strategy diversity: {report.get('strategy_diversity')}",
        "",
        "--- 5D paste skeleton ---",
        f"Week of: {report.get('start')}",
        "10-cycle sample: (pick from Activity — World→Judgment→Action coherent?)",
        "One change for next week: ",
        "Why: ",
    ]
    return "\n".join(lines)
