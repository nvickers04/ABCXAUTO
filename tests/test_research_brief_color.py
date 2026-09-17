"""Research brief stays color on wake. Regime and watchlist machinery are gone."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from abcxauto.config import clear_runtime_overrides, get_config
from abcxauto.desk_mode import (
    load_research_brief,
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
    assert "send" not in color.lower() or "trigger" in color
    assert "regime=" not in color
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
    assert "expectancy" not in (payload.get("brief") or {})
    assert "facts" not in (payload.get("brief") or {})
    assert "prove_window_id" not in (payload.get("brief") or {})
    assert payload["brief"].get("as_of", "").startswith("2026-09-09")
    assert payload["brief"].get("session") == "premarket"


def test_write_research_brief_does_not_stamp_regime():
    write_research_brief(
        session="premarket",
        snap={
            "regime": {
                "theme": "rate-sensitive",
                "catalyst": "announcement",
                "source": "odds/Fed September",
                "arenas": ["financials"],
                "invalidate": "FOMC holds",
            },
            "news_items": [
                {
                    "symbol": "JPM",
                    "headline": "Fed September decision preview",
                    "publisher": "MDA",
                }
            ],
        },
        now=datetime.now(timezone.utc),
    )
    disk = load_research_brief()
    assert "regime" not in disk
    color = rth_research_color(full=True)
    assert "regime=" not in color
    assert "JPM" in color or "expectancy" in color


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
