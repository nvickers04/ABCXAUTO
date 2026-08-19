"""Wake tape seed + empty scan seed (not a premarket SOP)."""

from __future__ import annotations

import json

import pytest

from abcxauto.opportunity_scan import (
    format_scan_tape,
    merge_tape,
    tape_seed_symbols,
)
from abcxauto.universe import reset_universe_cache, save_allowlist
from abcxauto.world_state import day_facts, format_wake


@pytest.fixture
def legal_tape(tmp_path, monkeypatch):
    path = tmp_path / "universe.json"
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(path))
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": [],
            "exclude_symbols": [],
            "legal_symbols": ["ZZZZ", "MSFT", "NVDA"],
        }
    )
    reset_universe_cache()
    monkeypatch.setattr(
        "abcxauto.universe.legal_symbols",
        lambda use_cache=True: ["ZZZZ", "MSFT", "NVDA"],
    )
    yield
    reset_universe_cache()


def test_tape_seed_book_then_legal_not_open_lot_only(legal_tape):
    seed = tape_seed_symbols([{"symbol": "AAPL"}], cap=10)
    assert seed[0] == "AAPL"
    assert "ZZZZ" in seed
    assert "MSFT" in seed
    assert seed != sorted(seed)
    assert seed != ["AAPL"]


def test_merge_tape_preserves_seed_order():
    merged = merge_tape(
        [{"symbol": "NVDA", "mda_last": 1}],
        [{"symbol": "AAPL", "mda_last": 2}, {"symbol": "ZZZZ", "mda_last": 3}],
    )
    assert [r["symbol"] for r in merged] == ["NVDA", "AAPL", "ZZZZ"]
    assert [r["symbol"] for r in merged] != sorted(r["symbol"] for r in merged)


def test_format_scan_tape_does_not_alphabetize():
    text = format_scan_tape(
        [
            {"symbol": "ZZZZ", "mda_last": 1, "source": "mda", "freshness": "delayed"},
            {"symbol": "AAPL", "mda_last": 2, "source": "mda", "freshness": "delayed"},
        ]
    )
    assert text.index("ZZZZ") < text.index("AAPL")


def test_day_facts_carry_tape_and_minutes(legal_tape):
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
    assert day["tape_seed"][0] == "AAPL"
    assert "ZZZZ" in day["tape_seed"]
    assert day["tape_seed"] != ["AAPL"]


def test_format_wake_surfaces_tape_and_minutes():
    from abcxauto.wake_bus import note_wake

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
            "capacity": {"open_count": 1, "max_open_positions": 15},
            "open_lots": ["AAPL STK long 20"],
            "tape_seed": ["AAPL", "ZZZZ", "MSFT"],
            "minutes_to_open": 59,
        },
    )
    assert "session=premarket" in text
    assert "minutes_to_open=59" in text
    assert "tape=AAPL,ZZZZ,MSFT" in text
    assert "open_lots=AAPL STK long 20" in text
    assert "session_prep" not in text
    # Facts only — no SOP / checklist lecture.
    assert "estimate" not in text.lower()
    assert "you must" not in text.lower()
    assert "pick" not in text.lower()
    assert text.rstrip().endswith("send|set_wake.")


def test_format_wake_rth_fill_delta_still_carries_tape_when_flat():
    """After flatten, kind=fill must not leave an empty chair — tape facts stay on."""
    from abcxauto.wake_bus import BookEvent, note_wake

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
                "capacity": {"open_count": 0, "max_open_positions": 15},
                "mix": {},
                "tape_seed": ["ZZZZ", "MSFT", "NVDA"],
            },
        )
    finally:
        note_wake(None)
    assert text.startswith("event=fill AAPL target filled.")
    assert "session=regular flat=True" in text
    assert "tape=ZZZZ,MSFT,NVDA" in text
    assert "This is a delta" in text
    assert "session_prep" not in text
    assert "Cycle 6." not in text
    assert "you must" not in text.lower()
    assert text.rstrip().endswith("send|set_wake.")


@pytest.mark.parametrize("kind", ["fill", "order_change", "book_move"])
def test_format_wake_rth_delta_kinds_carry_tape(kind):
    from abcxauto.wake_bus import BookEvent, note_wake

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
                "capacity": {"open_count": 0, "max_open_positions": 15},
                "tape_seed": ["SPY", "QQQ", "IWM"],
            },
        )
    finally:
        note_wake(None)
    assert f"event={kind}" in text
    assert "tape=SPY,QQQ,IWM" in text


def test_format_wake_non_rth_fill_delta_omits_tape():
    """Premarket fill delta stays lean; non-delta wake already prints tape=."""
    from abcxauto.wake_bus import BookEvent, note_wake

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
                "capacity": {"open_count": 0, "max_open_positions": 15},
                "tape_seed": ["SPY", "QQQ"],
                "minutes_to_open": 40,
            },
        )
    finally:
        note_wake(None)
    assert "event=fill" in text
    assert "tape=" not in text
    assert "This is a delta" in text


@pytest.mark.asyncio
async def test_scan_empty_seeds_universe(monkeypatch, legal_tape):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def fake_scan(positions=None, **_k):
        return [
            {"symbol": s, "source": "mda", "freshness": "delayed_daily", "mda_last": 1.0}
            for s in tape_seed_symbols(positions)
        ]

    monkeypatch.setattr(
        "abcxauto.opportunity_scan.scan_opportunities",
        fake_scan,
    )

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
    assert data["symbols"][0] == "AAPL"
    assert "ZZZZ" in data["symbols"]
    assert data["symbols"] != ["AAPL"]
    assert world.scan_fetched == data["symbols"]
