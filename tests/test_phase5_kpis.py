"""Phase 5 KPI rollup helpers."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from abcxauto.phase5_kpis import (
    build_day_report,
    format_day_report,
    hunt_symbols,
    structure_counts,
)


def test_structure_counts():
    events = [
        {"outcome": "scrape_suspect", "reason_code": "scrape_suspect", "ts": "2026-07-20T12:00:00Z"},
        {"outcome": "geometry_stop_wrong_side", "reason_code": "geometry_stop_wrong_side"},
        {"outcome": "ok", "strategy": "bracket", "source": "cycle"},
    ]
    c = structure_counts(events)
    assert c["scrapes"] == 1
    assert c["geometry"] == 1
    assert c["ok_hunts"] == 1


def test_hunt_symbols_from_decisions():
    decisions = [
        {
            "strategy": "bracket",
            "outcome": {
                "judgment": {"intent": {"symbol": "IWM", "kind": "hunt"}},
            },
        },
        {
            "strategy": "market_bracket",
            "outcome": {
                "judgment": {"intent": {"symbol": "QQQ", "kind": "hunt"}},
            },
        },
        {"strategy": "hold", "outcome": {}},
    ]
    assert hunt_symbols(decisions) == ["IWM", "QQQ"]


def test_build_day_report_filters_by_day(tmp_path):
    events_path = tmp_path / "structure_events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-07-20T15:00:00+00:00",
                        "outcome": "scrape_suspect",
                        "reason_code": "scrape_suspect",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-07-19T15:00:00+00:00",
                        "outcome": "ok",
                        "strategy": "bracket",
                        "source": "cycle",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    class J:
        def recent_judgments(self, limit=80):
            return [
                {
                    "ts": "2026-07-20T14:00:00+00:00",
                    "stance": "idle",
                    "dismissed": "QQQ chop",
                    "cycle": 1,
                },
                {
                    "ts": "2026-07-19T14:00:00+00:00",
                    "stance": "hunt",
                    "dismissed": "",
                    "cycle": 2,
                },
            ]

        def recent_decisions(self, limit=80):
            return [
                {
                    "ts": "2026-07-20T14:05:00+00:00",
                    "strategy": "bracket",
                    "outcome": {
                        "judgment": {"intent": {"symbol": "IWM"}},
                    },
                },
                {
                    "ts": "2026-07-20T14:06:00+00:00",
                    "strategy": "blocked",
                    "outcome": {"status": "blocked", "note": "max size"},
                    "rationale": "gate",
                },
            ]

        def strategy_diversity(self, limit=40):
            return {"n_distinct": 1, "strategies": ["bracket"], "limit": limit}

        def get_working_thesis(self):
            return "IWM long structure"

    rep = build_day_report(
        journal=J(),
        day=date(2026, 7, 20),
        structure_path=events_path,
    )
    assert rep["n_judgments"] == 1
    assert rep["entries"] == 1
    assert rep["structure"]["scrapes"] == 1
    assert rep["hunt_symbols"] == ["IWM"]
    assert rep["idle"]["idle_with_dismiss"] == 1
    assert any("max size" in b for b in rep["gate_blocks"])
    text = format_day_report(rep)
    assert "5B paste skeleton" in text
    assert "2026-07-20" in text
