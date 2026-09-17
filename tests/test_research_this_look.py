"""This-look research is gathered color, not a stale expectancy dump."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from abcxauto.desk_mode import (
    THIS_LOOK_NEED,
    load_research_brief,
    note_research_tool,
    research_brief_look_payload,
    write_research_brief,
)

_FIXTURE_BRIEF = (
    Path(__file__).resolve().parent / "fixtures" / "research_brief_2026_09_09.json"
)


def test_empty_snap_need_capability_line():
    payload = research_brief_look_payload({}, snap={})
    assert payload["use"] == "color, never a live trigger"
    assert payload["send_geometry"] is False
    assert payload["missing"] is True
    assert payload["stale"] is True
    assert payload["prior_session"] == {}
    assert payload["this_look"]["tools"] == []
    assert payload["this_look"]["facts"] == []
    assert payload["need"] == THIS_LOOK_NEED
    assert "scan|news|candles|web|odds|recall" in payload["need"]


def test_this_look_tools_after_scan_and_news():
    snap: dict = {}
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "AMD", "open_gap_pct": 4.2}], "source": "ibkr"},
    )
    note_research_tool(
        snap,
        "news",
        {"items": [{"symbol": "NVDA", "headline": "NVDA prints after hours"}]},
    )
    payload = research_brief_look_payload({}, snap=snap)
    tools = payload["this_look"]["tools"]
    assert "scan" in tools
    assert "news" in tools
    assert "need" not in payload
    opens = payload["this_look"]["open"]
    assert "scan: arena|scan_code|symbols[]" not in opens
    assert "news: symbols[]" not in opens
    assert "candles: symbol+resolution" in opens
    assert "web: url" in opens
    assert "odds: query|symbols[]" in opens
    assert "recall: notes|cards" in opens


def test_stale_prior_file_does_not_leak_expectancy(tmp_path, monkeypatch):
    dest = tmp_path / "research_brief.json"
    dest.write_bytes(_FIXTURE_BRIEF.read_bytes())
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(dest))
    brief = load_research_brief()
    assert brief.get("expectancy")
    payload = research_brief_look_payload(brief, snap={}, now=datetime.now(timezone.utc))
    assert payload["stale"] is True
    prior = payload["prior_session"]
    assert "expectancy" not in prior
    assert "tickets" not in prior
    assert "prove_window_id" not in prior
    assert "gate_verdict" not in prior
    assert set(prior) <= {"as_of", "session"}
    assert payload["need"] == THIS_LOOK_NEED


def test_write_omits_expectancy_and_tickets(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    out = write_research_brief(session="premarket", snap={}, now=datetime.now(timezone.utc))
    assert "expectancy" not in out
    assert "tickets" not in out
    assert out["facts"] == []
    assert any("nothing was gathered" in str(u) for u in out["uncertainties"])
    disk = load_research_brief()
    assert "expectancy" not in disk
    assert "tickets" not in disk


def test_scan_fact_line_ignores_aliased_gap_pct():
    snap: dict = {}
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "FOO", "gap_pct": 12.0}], "source": "scan"},
    )
    text = snap["_research_bag"]["facts"][0]["text"]
    assert "deepest=" not in text
    assert "12" not in text
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "AMD", "open_gap_pct": 4.2}], "source": "scan"},
    )
    open_line = snap["_research_bag"]["facts"][1]["text"]
    assert "deepest=AMD 4.2%" in open_line
