"""Arena freshness, refresh-on-retune, and research-brief regime."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from abcxauto.config import clear_runtime_overrides, get_config
from abcxauto.desk_mode import (
    load_research_brief,
    research_brief_stale,
    rth_research_color,
    write_research_brief,
)
from abcxauto.look_snapshot import REASON_CODE, begin_look, check_ticket_numbers
from abcxauto.self_tune import apply_self_tune
from abcxauto.universe import (
    load_allowlist,
    refresh_legal_set,
    reset_universe_cache,
    save_allowlist,
)
from abcxauto.world_state import day_facts, format_wake


_FIXTURE_BRIEF = (
    Path(__file__).resolve().parent / "fixtures" / "research_brief_2026_09_09.json"
)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe.json"))
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(tmp_path / "risk.json"))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    reset_universe_cache()
    clear_runtime_overrides()
    get_config.cache_clear()
    yield
    reset_universe_cache()
    clear_runtime_overrides()
    get_config.cache_clear()


def test_membership_watch_marks_sort_vs_bucket_and_age():
    from abcxauto.universe import (
        SORT_MEMBERSHIP_STALE_S,
        membership_watch_line,
    )

    save_allowlist(
        {
            "enabled_arenas": ["top_gainers", "mega_cap"],
            "custom_symbols": ["SPY"],
            "exclude_symbols": [],
            "refreshed_at": (datetime.now(timezone.utc) - timedelta(hours=4)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    )
    line = membership_watch_line()
    assert "age=" in line
    assert "4h" in line or "3.9h" in line or "4.0h" in line
    assert "top_gainers:sort:stale" in line
    assert "mega_cap:bucket" in line
    assert ":stale" not in line.split("mega_cap:bucket")[0][-6:] or "mega_cap:bucket:stale" not in line
    assert SORT_MEMBERSHIP_STALE_S == 2 * 3600


def test_fresh_sort_is_not_marked_stale():
    from abcxauto.universe import membership_watch_line

    save_allowlist(
        {
            "enabled_arenas": ["top_gainers"],
            "refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    line = membership_watch_line()
    assert "top_gainers:sort" in line
    assert "top_gainers:sort:stale" not in line


@pytest.mark.asyncio
async def test_scan_and_day_facts_surface_watch(monkeypatch):
    from abcxauto.opportunity_scan import criteria_scan
    from abcxauto.universe import membership_watch_line
    from abcxauto.world_state import WorldState

    save_allowlist(
        {
            "enabled_arenas": ["hot_by_volume", "index_etfs"],
            "refreshed_at": (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    )

    async def fake_pull(_connector=None, **_k):
        return {
            "ok": True,
            "arena_id": "hot_by_volume",
            "scan_code": "HOT_BY_VOLUME",
            "source": "ibkr",
            "symbols": ["AAPL"],
            "rows": [{"symbol": "AAPL", "rank": 0, "distance": "1.0"}],
            "applied": {},
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)
    out = await criteria_scan(arena="hot_by_volume", connector=object())
    assert out["ok"] is True
    watch = out.get("watch") or ""
    assert "age=" in watch
    assert "hot_by_volume:sort:stale" in watch
    assert "index_etfs:bucket" in watch
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    day = day_facts(world)
    assert "age=" in str(day.get("watch") or "")
    assert "hot_by_volume:sort" in str(day.get("watch") or "")
    assert membership_watch_line() == watch or "stale" in str(day.get("watch") or "")


@pytest.mark.asyncio
async def test_self_tune_arenas_take_effect_on_next_scan_not_desk_relaunch():
    apply_self_tune({"enabled_arenas": ["index_etfs", "commodities"]}, persist=True)
    al = load_allowlist()
    assert al.get("refresh_pending") is True
    assert "SPY" not in (al.get("legal_symbols") or [])

    from abcxauto.opportunity_scan import criteria_scan

    await criteria_scan(arena="most_active", connector=None)
    al = load_allowlist()
    assert al.get("refresh_pending") is False
    assert "SPY" in al["legal_symbols"]
    assert "GLD" in al["legal_symbols"]
    assert al["enabled_arenas"] == ["index_etfs", "commodities"]


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


def test_regime_round_trips_to_wake():
    write_research_brief(
        session="premarket",
        snap={
            "regime": {
                "theme": "rate-sensitive",
                "catalyst": "announcement",
                "source": "odds/Fed September",
                "arenas": ["financials", "commodities"],
                "invalidate": "FOMC holds and 2s10s unchanged",
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
    reg = disk.get("regime") or {}
    assert reg["theme"] == "rate-sensitive"
    assert reg["catalyst"] == "announcement"
    assert reg["source"] == "odds/Fed September"
    assert reg["arenas"] == ["financials", "commodities"]
    assert "FOMC" in reg["invalidate"]
    color = rth_research_color(full=True)
    assert "regime=rate-sensitive" in color
    assert "financials" in color
    assert "age=" in color
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "send=allowed" in wake
    assert "rate-sensitive" in wake


def test_regime_arenas_are_not_auto_applied():
    before = load_allowlist()
    write_research_brief(
        session="premarket",
        snap={
            "regime": {
                "theme": "rate-sensitive",
                "catalyst": "announcement",
                "source": "odds",
                "arenas": ["financials"],
                "invalidate": "odds reprice",
            }
        },
        now=datetime.now(timezone.utc),
    )
    after = load_allowlist()
    assert after["enabled_arenas"] == before["enabled_arenas"]
    assert "financials" not in after["enabled_arenas"]
    apply_self_tune({"enabled_arenas": ["financials"]}, persist=True)
    tuned = load_allowlist()
    assert tuned["enabled_arenas"] == ["financials"]


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


@pytest.mark.asyncio
async def test_membership_and_scan_hits_are_not_send_geometry():
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": [],
            "exclude_symbols": [],
        }
    )
    al = await refresh_legal_set(connector=None, persist=True)
    for row in al["membership"]:
        assert "last" not in row
        assert set(row) <= {"symbol", "arena", "source"}
    snap: dict = {}
    begin_look(snap)
    snap["scan_hits"] = {
        "source": "ibkr",
        "arena": "top_gainers",
        "watch": "age=4h top_gainers:sort:stale",
        "rows": [{"symbol": "SPY", "arena": "top_gainers", "gap%": 1.2}],
    }
    ok, code, _msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "SPY", "price_hint": 500.12, "stop_price": 495.0},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE


def test_self_tune_writes_regime_persists_to_wake():
    before = load_allowlist()
    out = apply_self_tune(
        {
            "regime": {
                "theme": "rate-sensitive",
                "catalyst": "announcement",
                "source": "odds/Fed September",
                "arenas": ["financials", "commodities"],
                "invalidate": "FOMC holds and 2s10s unchanged",
            }
        },
        persist=True,
    )
    assert out["status"] == "ok"
    applied = (out.get("applied") or {}).get("regime") or {}
    assert applied["theme"] == "rate-sensitive"
    assert applied["arenas"] == ["financials", "commodities"]
    assert "invalidate" in applied
    disk = load_research_brief()
    reg = disk.get("regime") or {}
    assert reg["theme"] == "rate-sensitive"
    assert reg["as_of"]
    assert reg["invalidate"]
    after = load_allowlist()
    assert after["enabled_arenas"] == before["enabled_arenas"]
    assert "financials" not in after["enabled_arenas"]
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "rate-sensitive" in wake
    assert "FOMC" in wake or "invalidate=" in wake
    assert "send=allowed" in wake


def test_self_tune_regime_unknown_arena_names_catalog():
    from abcxauto.universe import ARENA_CATALOG

    before = load_allowlist()
    out = apply_self_tune(
        {
            "regime": {
                "theme": "rates",
                "catalyst": "announcement",
                "source": "odds",
                "arenas": ["financials", "not_a_real_arena"],
                "invalidate": "odds reprice",
            }
        },
        persist=True,
    )
    rejected = out.get("rejected") or {}
    err = str(rejected.get("regime") or "")
    assert "not_a_real_arena" in err
    for name in ARENA_CATALOG:
        assert name in err
    assert "financials" not in (load_allowlist()["enabled_arenas"])
    assert load_allowlist()["enabled_arenas"] == before["enabled_arenas"]
    assert "regime" not in (load_research_brief() or {})


def test_self_tune_regime_rejects_lecture():
    before = load_allowlist()
    out = apply_self_tune(
        {
            "regime": {
                "theme": "Always fade financials into the close",
                "catalyst": "announcement",
                "source": "odds",
                "arenas": ["financials"],
                "invalidate": "You must hold through the print",
            }
        },
        persist=True,
    )
    err = str((out.get("rejected") or {}).get("regime") or "")
    assert err
    assert "lecture" in err.lower() or "imperative" in err.lower()
    assert load_allowlist()["enabled_arenas"] == before["enabled_arenas"]
    assert "regime" not in (load_research_brief() or {})


def test_wake_carries_compact_watch_for_stale_sort():
    from abcxauto.universe import WAKE_WATCH_MAX_CHARS, membership_wake_bit

    save_allowlist(
        {
            "enabled_arenas": ["top_gainers", "mega_cap"],
            "refreshed_at": (datetime.now(timezone.utc) - timedelta(hours=4)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    )
    bit = membership_wake_bit()
    added = f" watch={bit}."
    assert "4h" in bit
    assert "top_gainers" in bit
    assert "old=" in bit
    assert "age=" not in bit
    assert "stale=" not in bit
    assert len(added) <= WAKE_WATCH_MAX_CHARS
    # ~4 chars/token; keep the wake add under 25 tokens.
    tokens = max(1, (len(added) + 3) // 4)
    assert tokens <= 25
    wake = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "watch=" in wake
    assert "top_gainers" in wake
    assert "old=" in wake
    assert "desk_mode=rth" in wake

