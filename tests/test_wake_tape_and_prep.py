"""Wake has no canned tape=; scan requires criteria (not a premarket SOP)."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("stub_agent_loop_import")

from abcxauto.opportunity_scan import (
    merge_tape,
    tape_seed_symbols,
)
from abcxauto.world_state import day_facts, format_wake


def test_tape_seed_is_book_only():
    seed = tape_seed_symbols([{"symbol": "AAPL"}, {"symbol": "MSFT"}], cap=10)
    assert seed[0] == "AAPL"
    assert seed == ["AAPL", "MSFT"]
    assert tape_seed_symbols([], cap=10) == []
    assert "SPY" not in tape_seed_symbols([])
    assert "ZZZZ" not in seed


def test_merge_tape_preserves_seed_order():
    merged = merge_tape(
        [{"symbol": "NVDA", "mda_last": 1}],
        [{"symbol": "AAPL", "mda_last": 2}, {"symbol": "ZZZZ", "mda_last": 3}],
    )
    assert [r["symbol"] for r in merged] == ["NVDA", "AAPL", "ZZZZ"]
    assert [r["symbol"] for r in merged] != sorted(r["symbol"] for r in merged)


def test_day_facts_carry_tape_and_minutes():
    from abcxauto.world_state import WorldState

    world = WorldState(
        cycle=1,
        session_status="premarket",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
        daily_pnl=0.0,
        positions=[{"symbol": "AAPL", "sec_type": "STK", "position": 20}],
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
        pulse={
            "session": {
                "status": "premarket",
                "countdown_to": "open",
                "countdown_s": 45 * 60,
            }
        },
    )
    day = day_facts(world, {})
    assert "session_prep" not in day
    assert day["minutes_to_open"] == 45
    assert "tape_seed" not in day
    assert "watch" not in day


def test_day_facts_flat_book_does_not_invent_a_tape():
    """Flattened book has no watchlist leftover. Wake must not print tape=."""
    from abcxauto.world_state import WorldState

    world = WorldState(
        cycle=6,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
        daily_pnl=12.0,
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
    day = day_facts(world, {})
    assert "tape_seed" not in day
    assert "watch" not in day


def test_wake_contains_no_watch_or_tape():
    from abcxauto.park_clock import note_wake
    from abcxauto.world_state import WorldState

    note_wake(None)
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
        daily_pnl=12.0,
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
    day = day_facts(world, {})
    assert "watch" not in day
    assert "tape_seed" not in day
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day=day,
    )
    assert "watch=" not in text
    assert "tape=" not in text


def test_format_wake_no_tape_keeps_lots_and_minutes():
    from abcxauto.park_clock import note_wake

    note_wake(None)
    text = format_wake(
        cycle=1,
        session="premarket",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "nl": 100_000.0,
            "daily_pnl": 0.0,
            "risk_per_trade_pct": 5.0,
            "sizing_floors": False,
            "capacity": {"open_count": 1, "max_open_positions": 0},
            "open_lots": ["AAPL STK long 20"],
            "mix": {"stk": 1},
            "tape_seed": ["AAPL", "ZZZZ", "MSFT"],
            "minutes_to_open": 59,
        },
    )
    assert "session=premarket" in text
    assert "minutes_to_open=59" in text
    assert "tape=" not in text
    assert "options=live" not in text
    assert "open_lots=AAPL STK long 20" in text
    assert "max_risk=5.0% floors=off" in text
    assert "risk/trade=" not in text
    assert "mix=" in text
    assert "session_prep" not in text
    assert "estimate" not in text.lower()
    assert "you must" not in text.lower()
    assert text.rstrip().split()[-1] != "send."
    assert not text.rstrip().endswith("send.")


def test_format_wake_rth_fill_delta_no_tape_when_flat():
    """After flatten, kind=fill keeps book facts — no leftover / substitute name dump."""
    from abcxauto.think_stream import write_desk_brief
    from abcxauto.park_clock import BookEvent, note_wake

    write_desk_brief({"strat": "market_bracket", "sends": 2})
    note_wake(BookEvent(kind="fill", detail="AAPL target filled"))
    try:
        text = format_wake(
            cycle=6,
            session="regular",
            flat=True,
            unprotected=[],
            ibkr_up=True,
            day={
                "nl": 100_000.0,
                "daily_pnl": 12.0,
                "names": 0,
                "lots": 0,
                "open_lots": [],
                "capacity": {"open_count": 0, "max_open_positions": 0},
                "mix": {},
                "tape_seed": ["ZZZZ", "MSFT", "NVDA"],
            },
        )
    finally:
        note_wake(None)
    assert "event=fill AAPL target filled." in text
    assert "session=regular flat=True" in text
    assert "tape=" not in text
    assert "This is a delta" not in text
    assert "yield resume" not in text
    assert "session_prep" not in text
    assert "Cycle 6." not in text
    assert "you must" not in text.lower()
    assert "chain" not in text.lower()
    assert "next=" not in text
    assert "playbook rev=" not in text
    assert "set_wake" not in text


@pytest.mark.parametrize("kind", ["fill", "order_change", "book_move"])
def test_format_wake_rth_delta_kinds_no_tape(kind):
    from abcxauto.park_clock import BookEvent, note_wake

    note_wake(BookEvent(kind=kind, detail="marks"))
    try:
        text = format_wake(
            cycle=2,
            session="regular",
            flat=True,
            unprotected=[],
            ibkr_up=True,
            day={
                "nl": 50_000.0,
                "capacity": {"open_count": 0, "max_open_positions": 0},
                "tape_seed": ["SPY", "QQQ", "IWM"],
            },
        )
    finally:
        note_wake(None)
    assert f"event={kind}" in text
    assert "tape=" not in text
    assert "This is a delta" not in text
    assert "yield resume" not in text


@pytest.mark.parametrize("kind", ["alarm", "boot", "operator"])
def test_format_wake_alarm_boot_operator_no_tape_no_options_live(kind):
    from abcxauto.park_clock import BookEvent, note_wake

    note_wake(BookEvent(kind=kind, detail="wake"))
    try:
        text = format_wake(
            cycle=3,
            session="regular",
            flat=True,
            unprotected=[],
            ibkr_up=True,
            day={
                "nl": 50_000.0,
                "names": 0,
                "lots": 0,
                "capacity": {"open_count": 0, "max_open_positions": 0},
                "tape_seed": ["SPY", "QQQ", "IWM", "DIA", "AAPL"],
            },
        )
    finally:
        note_wake(None)
    assert "tape=" not in text
    assert "options=live" not in text
    assert "SPY,QQQ" not in text


def test_format_wake_open_lots_and_mix_still_print():
    from abcxauto.park_clock import note_wake

    note_wake(None)
    text = format_wake(
        cycle=4,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "nl": 80_000.0,
            "names": 1,
            "lots": 1,
            "capacity": {"open_count": 1, "max_open_positions": 0},
            "risk_per_trade_pct": 25.0,
            "sizing_floors": False,
            "open_lots": ["NVDA STK long 5"],
            "mix": {"stk": 1},
            "tape_seed": ["SPY", "QQQ"],
        },
    )
    assert "open_lots=NVDA STK long 5" in text
    assert "max_risk=25.0% floors=off" in text
    assert "risk/trade=" not in text
    assert "mix=" in text
    assert "tape=" not in text
    assert "next=" not in text
    assert "playbook rev=" not in text


def test_format_wake_non_rth_fill_delta_omits_tape_and_options():
    from abcxauto.park_clock import BookEvent, note_wake

    note_wake(BookEvent(kind="fill", detail="AAPL filled"))
    try:
        text = format_wake(
            cycle=2,
            session="premarket",
            flat=True,
            unprotected=[],
            ibkr_up=True,
            day={
                "nl": 50_000.0,
                "capacity": {"open_count": 0, "max_open_positions": 0},
                "tape_seed": ["SPY", "QQQ"],
                "minutes_to_open": 40,
            },
        )
    finally:
        note_wake(None)
    assert "event=fill" in text
    assert "tape=" not in text
    assert "options=live" not in text
    assert "This is a delta" not in text


@pytest.mark.asyncio
async def test_scan_empty_is_not_canned_tape(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def boom(*_a, **_k):
        raise AssertionError("empty scan must not fetch MDA / seed legal_symbols")

    monkeypatch.setattr("abcxauto.opportunity_scan.fetch_scan_metrics", boom)
    monkeypatch.setattr("abcxauto.opportunity_scan.scan_opportunities", boom)

    world = WorldState(
        cycle=1,
        session_status="premarket",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
        daily_pnl=0.0,
        positions=[{"symbol": "AAPL", "sec_type": "STK", "position": 20}],
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
    data = json.loads(
        await _run_tool(
            "scan",
            {},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data.get("ok") is False
    assert "IBKR" in str(data.get("error") or "")
    assert data.get("symbols") in (None, [])
    assert "tape" not in data or not data.get("tape")
    assert "SPY" not in (data.get("symbols") or [])
    assert "QQQ" not in (data.get("symbols") or [])
    assert "IWM" not in str(data)


@pytest.mark.asyncio
async def test_scan_arena_most_active_ibkr_order_overlay_no_watchlist_write(
    monkeypatch, tmp_path
):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    watch = tmp_path / "universe_allowlist.json"

    async def fake_pull(connector=None, *, arena=None, scan_code=None, filters=None):
        assert arena in ("most_active", "top_losers", "top_gainers")
        assert scan_code in ("MOST_ACTIVE", "TOP_PERC_LOSE", "TOP_PERC_GAIN")
        return {
            "ok": True,
            "arena_id": arena,
            "scan_code": scan_code,
            "source": "ibkr",
            "symbols": ["TSLA", "AAPL", "AMD"],
            "applied": {},
            "persisted": False,
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
        daily_pnl=0.0,
        positions=[{"symbol": "AAPL", "sec_type": "STK", "position": 10}],
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
    data = json.loads(
        await _run_tool(
            "scan",
            {"arena": "most_active"},
            connector=object(),
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert data["symbols"] == ["TSLA", "AAPL", "AMD"]
    assert data["ranked"] is False
    assert data["persisted"] is False
    assert data.get("thin") is True
    hits = {h["symbol"]: h for h in data["hits"]}
    from abcxauto.opportunity_scan import RANKED_ROW_KEYS

    for hit in data["hits"]:
        assert set(hit) <= RANKED_ROW_KEYS
        assert "skip_class" in hit
        assert hit.get("source") == "ibkr"
        assert "on_book" not in hit
    # Kill condition: scan must not start quoting.
    assert "bid" not in hits["AAPL"]
    assert "ask" not in hits["AAPL"]
    assert "tape" not in data
    assert not watch.is_file()


@pytest.mark.asyncio
async def test_scan_unknown_arena_rejected(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
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
    data = json.loads(
        await _run_tool(
            "scan",
            {"arena": "not_a_real_arena"},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data.get("ok") is False
    err = str(data.get("error") or "")
    assert "unknown" in err.lower()
    assert "valid=" in err or "most_active" in err


@pytest.mark.asyncio
async def test_scan_symbols_no_mda_candles_no_quotes(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def boom(*_a, **_k):
        raise AssertionError("scan must not fetch MDA daily-120")

    monkeypatch.setattr("abcxauto.opportunity_scan.fetch_scan_metrics", boom)

    class Conn:
        async def get_live_quotes(self, *_a, **_k):
            raise AssertionError("scan must not attach live quotes")

        async def get_live_quote(self, *_a, **_k):
            raise AssertionError("scan must not attach live quotes")

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
        daily_pnl=0.0,
        positions=[{"symbol": "NVDA", "sec_type": "STK", "position": 10}],
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
    data = json.loads(
        await _run_tool(
            "scan",
            {"symbols": ["NVDA", "XLE"]},
            connector=Conn(),
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert data["symbols"] == ["NVDA", "XLE"]
    assert data.get("thin") is False
    fat = {h["symbol"]: h for h in data["hits"]}
    assert fat["NVDA"]["on_book"] is True
    assert fat["XLE"]["on_book"] is False
    assert all("last" not in h and "bid" not in h for h in data["hits"])
    assert "mda_last" not in (data.get("hits") or [{}])[0]


@pytest.mark.asyncio
async def test_scan_ibkr_empty_does_not_dump_catalog_names(monkeypatch):
    """Connected IBKR + empty screen → empty hits, labeled empty."""
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def empty_scan(_connector, _spec):
        return {"ok": True, "symbols": [], "rows": [], "ibkr_rows": 0, "kept": 0}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", empty_scan)

    class Conn:
        connected = True

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
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
    data = json.loads(
        await _run_tool(
            "scan",
            {"arena": "mega_cap"},
            connector=Conn(),
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert data.get("source") in ("empty", "ibkr")
    assert data.get("empty") is True or data["symbols"] == []
    assert data["symbols"] == []
    assert data["hits"] == []
    for name in ("AAPL", "MSFT", "NVDA", "SPY", "QQQ"):
        assert name not in (data.get("symbols") or [])
