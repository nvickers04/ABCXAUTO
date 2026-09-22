"""Research brief stays color on wake. Regime and watchlist machinery are gone."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from abcxauto.config import clear_runtime_overrides, get_config
from abcxauto.desk_mode import (
    load_research_brief,
    note_research_tool,
    research_brief_look_payload,
    research_brief_stale,
    rth_research_color,
    write_research_brief,
)
from abcxauto.look_snapshot import REASON_CODE, begin_look, check_ticket_numbers
from abcxauto.world_state import format_wake


_FIXTURE_BRIEF = (
    Path(__file__).resolve().parent / "fixtures" / "research_brief_2026_09_09.json"
)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(tmp_path / "risk.json"))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    clear_runtime_overrides()
    get_config.cache_clear()
    yield
    clear_runtime_overrides()
    get_config.cache_clear()


def test_no_background_universe_timer():
    root = Path(__file__).resolve().parents[1] / "abcxauto"
    watched = [
        root / "universe.py",
        root / "self_tune.py",
        root / "opportunity_scan.py",
        root / "desk_mode.py",
    ]
    banned = (
        "threading.Timer",
        "AsyncIOScheduler",
        "BackgroundScheduler",
        "schedule.every",
        "loop.call_later",
    )
    for path in watched:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, (path.name, needle)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"Timer", "call_later"}:
                raise AssertionError(f"timer in {path.name}")


def test_real_2026_09_09_brief_parses_without_regime(tmp_path, monkeypatch):
    dest = tmp_path / "research_brief.json"
    dest.write_bytes(_FIXTURE_BRIEF.read_bytes())
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(dest))
    brief = load_research_brief()
    assert brief
    assert "regime" not in brief
    assert brief.get("as_of", "").startswith("2026-09-09")
    assert isinstance(brief.get("expectancy"), list)
    row = (brief.get("expectancy") or [{}])[0]
    assert "invalidate" in row
    color = rth_research_color(full=True)
    assert "prior_session_research" in color
    assert "stale" in color
    assert "send" not in color.lower() or "trigger" in color
    assert "regime=" not in color
    assert "gap_risk" not in color
    stale = research_brief_stale(brief, now=datetime.now(timezone.utc))
    assert stale is True
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "send=allowed" in wake
    assert "desk_mode=rth" in wake
    assert "stale" in wake
    assert "watch=" not in wake
    assert "tape=" not in wake
    payload = research_brief_look_payload(brief, now=datetime.now(timezone.utc))
    assert payload["stale"] is True
    assert payload["send_geometry"] is False
    prior = payload.get("prior_session") or {}
    assert "expectancy" not in prior
    assert "tickets" not in prior
    assert "facts" not in prior
    assert "prove_window_id" not in prior
    assert "gate_verdict" not in prior
    assert set(prior) <= {"as_of", "session"}
    assert prior.get("as_of", "").startswith("2026-09-09")
    assert prior.get("session") == "premarket"


def test_write_research_brief_does_not_stamp_regime():
    items = [
        {
            "symbol": "JPM",
            "headline": "Fed September decision preview",
            "publisher": "MDA",
        }
    ]
    snap = {
        "regime": {
            "theme": "rate-sensitive",
            "catalyst": "announcement",
            "source": "odds/Fed September",
            "arenas": ["financials"],
            "invalidate": "FOMC holds",
        },
        "news_items": items,
    }
    note_research_tool(snap, "news", {"items": items})
    write_research_brief(
        session="premarket",
        snap=snap,
        now=datetime.now(timezone.utc),
    )
    disk = load_research_brief()
    assert "regime" not in disk
    assert "expectancy" not in disk
    assert "tickets" not in disk
    color = rth_research_color(full=True)
    assert "regime=" not in color
    assert "JPM" in color
    assert "expectancy=" not in color


def test_scan_hits_are_not_send_geometry():
    snap: dict = {}
    begin_look(snap)
    snap["scan_hits"] = {
        "source": "ibkr",
        "arena": "top_gainers",
        "rows": [{"symbol": "SPY", "arena": "top_gainers", "gap%": 1.2}],
    }
    ok, code, _msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "SPY", "price_hint": 500.12, "stop_price": 495.0},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE


def test_mid_look_brief_sees_candles_on_snap_not_as_prior():
    """Candles stamped session_range this look — need clears; disk is not prior."""
    now = datetime.now(timezone.utc)
    snap = {
        "session_range": {
            "AVGO": {
                "open": 360.0,
                "high": 362.0,
                "low": 359.0,
                "last": 361.3,
                "n": 12,
                "today": True,
            }
        },
        "candle_source": "ibkr",
    }
    brief = {
        "as_of": now.isoformat(),
        "session": "premarket",
        "mode": "research",
        "symbols": ["AVGO"],
        "facts": [
            {"source": "marks", "text": "leftover $214"},
            {"source": "candles", "text": "AVGO 361.3 vs_open=0.1 src=ibkr"},
        ],
        "uncertainties": [],
    }
    payload = research_brief_look_payload(brief, snap=snap, now=now)
    assert "need" not in payload
    assert "candles" in (payload["this_look"].get("tools") or [])
    prior = payload.get("prior_session") or {}
    assert "facts" not in prior
    assert prior.get("as_of") == brief["as_of"]
    assert prior.get("session") == "premarket"
    # Prior brief must not satisfy new-risk research_thin.
    from abcxauto.desk_mode import new_risk_research_error

    assert new_risk_research_error("AVGO", {}, strat="bracket")


def test_candle_session_only_notes_a_fact():
    snap: dict = {}
    note_research_tool(
        snap,
        "candles",
        {
            "source": "ibkr",
            "symbol": "AVGO",
            "bars": [],
            "session": {"last": 361.3, "vs_open": -0.4, "open": 362.0},
        },
    )
    facts = snap["_research_bag"]["facts"]
    assert facts
    assert "AVGO 361.3" in facts[0]["text"]
    payload = research_brief_look_payload({}, snap=snap)
    assert "need" not in payload
    assert "candles" in payload["this_look"]["tools"]


def test_empty_snap_still_needs_color_despite_prior_scan():
    now = datetime.now(timezone.utc)
    brief = {
        "as_of": now.isoformat(),
        "session": "premarket",
        "symbols": ["NVDA"],
        "facts": [{"source": "scan", "text": "hits=10 src=ibkr"}],
    }
    payload = research_brief_look_payload(brief, snap={}, now=now)
    assert payload["need"]
    assert "scan|news|candles" in payload["need"]
    assert payload["this_look"]["tools"] == []
    # True prior keeps research facts when this look is empty.
    assert (payload.get("prior_session") or {}).get("facts") == brief["facts"]