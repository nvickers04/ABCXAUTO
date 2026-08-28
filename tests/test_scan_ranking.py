"""IBKR scan ranking: skip-class is not ``deepest``; one screen per look args."""

from __future__ import annotations

import asyncio
import json

import pytest

from abcxauto.brain import BrainTurn, _run_tool, _scan_gate_facts, _scan_look_key
from abcxauto.lab_playbook import PROTECTED_CARD_NAMES, SKIP_CARD_NAMES, skip_cards_on_book
from abcxauto.universe import scan_skip_class
from abcxauto.world_state import WorldState


def _world(**kwargs) -> WorldState:
    base = dict(
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
    base.update(kwargs)
    return WorldState(**base)


def _skip_book() -> dict:
    return {
        "types": {
            "market_bracket": {
                "cards": [
                    {
                        "name": "mega-cap earnings-flush bounce",
                        "status": "testing",
                        "scan": "most_active + top_losers; mega/large only",
                    },
                    {
                        "name": "levered-crypto and micro gap chase",
                        "status": "retired",
                        "scan": "top_gainers, high_open_gap",
                    },
                ]
            },
            "buy_option": {
                "cards": [
                    {
                        "name": "naked / short-dated option spray",
                        "status": "retired",
                    }
                ]
            },
        }
    }


def _flush_only_book() -> dict:
    return {
        "types": {
            "market_bracket": {
                "cards": [
                    {
                        "name": "mega-cap earnings-flush bounce",
                        "status": "testing",
                        "scan": "most_active + top_losers; mega/large only",
                    }
                ]
            }
        }
    }


def _tape():
    return [
        {"symbol": "PSQL", "open_gap_pct": 73.4, "last": 4.2},
        {"symbol": "TQQQ", "open_gap_pct": 12.0, "last": 88.0},
        {"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0},
    ]


def test_skip_card_names_are_the_existing_skip_classes():
    assert SKIP_CARD_NAMES <= PROTECTED_CARD_NAMES
    assert "levered-crypto and micro gap chase" in SKIP_CARD_NAMES
    assert "naked / short-dated option spray" in SKIP_CARD_NAMES
    assert skip_cards_on_book(_skip_book()) is True
    assert skip_cards_on_book(_flush_only_book()) is False
    assert skip_cards_on_book({"types": {}}) is False


def test_scan_skip_class_is_levered_or_micro_not_a_send_gate():
    assert scan_skip_class({"symbol": "PSQL", "last": 4.2}) == "micro"
    assert scan_skip_class({"symbol": "TQQQ", "last": 88.0}) == "levered"
    assert scan_skip_class({"symbol": "SOXL", "last": 40.0}) == "levered"
    assert scan_skip_class({"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5}) == ""
    assert scan_skip_class({"symbol": "AAPL", "last": 230.0}) == ""
    # Explicit tag for a name that is micro by cap, not by last.
    assert (
        scan_skip_class({"symbol": "PSQL", "last": 40.0, "market_cap": 80_000_000})
        == "micro"
    )


def test_skip_class_micro_is_not_deepest_when_skip_cards_exist():
    """Lottery print stays on the tape; trophy is the next non-skip name."""
    gate = _scan_gate_facts(_tape(), book=_skip_book())
    assert gate["deepest_symbol"] == "SNDK"
    assert gate["deepest_open_gap_pct"] == pytest.approx(-6.5)


def test_raw_deepest_keeps_the_lottery_print_without_skip_cards():
    gate = _scan_gate_facts(_tape(), book=_flush_only_book())
    assert gate["deepest_symbol"] == "PSQL"
    assert gate["deepest_open_gap_pct"] == pytest.approx(73.4)


@pytest.mark.asyncio
async def test_scan_tool_does_not_pin_skip_class_as_deepest(monkeypatch, tmp_path):
    from abcxauto.lab_playbook import _lab_path, _write

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    _write(_lab_path(), _skip_book())

    async def _fake_scan(**_kw):
        return {
            "ok": True,
            "source": "ibkr",
            "arena": "top_gainers",
            "scan_code": "TOP_PERC_GAIN",
            "symbols": ["PSQL", "TQQQ", "SNDK"],
            "hits": _tape(),
            "quoted": 3,
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    data = json.loads(
        await _run_tool(
            "scan",
            {"scan_code": "TOP_PERC_GAIN"},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert "PSQL" in data["symbols"]
    assert data["hits"][0]["symbol"] == "SNDK"
    assert data["deepest_symbol"] == "SNDK"
    assert data["deepest_open_gap_pct"] == pytest.approx(-6.5)
    assert "card_gap_met" not in data


@pytest.mark.asyncio
async def test_identical_scan_args_this_look_hit_the_cache(monkeypatch):
    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        return {
            "ok": True,
            "source": "ibkr",
            "arena": "top_gainers",
            "scan_code": "TOP_PERC_GAIN",
            "symbols": ["SNDK"],
            "hits": [{"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0}],
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    first = json.loads(
        await _run_tool(
            "scan",
            {"scan_code": "TOP_PERC_GAIN"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    second = json.loads(
        await _run_tool(
            "scan",
            {"scan_code": "TOP_PERC_GAIN"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert n["calls"] == 1
    assert first.get("reused") is not True
    assert first["screens_this_look"] == 1
    assert second["reused"] is True
    assert second["screens_this_look"] == 1
    # Alias of the same screen is the same look key.
    alias = json.loads(
        await _run_tool(
            "scan",
            {"arena": "top_gainers"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert n["calls"] == 1
    assert alias["reused"] is True
    assert _scan_look_key({"scan_code": "TOP_PERC_GAIN"}) == _scan_look_key(
        {"arena": "top_gainers"}
    )


@pytest.mark.asyncio
async def test_stay_up_poke_does_not_refetch_the_same_scan(monkeypatch):
    from abcxauto.brain import _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        return {
            "ok": True,
            "source": "ibkr",
            "scan_code": "TOP_PERC_GAIN",
            "symbols": ["SNDK"],
            "hits": [{"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0}],
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    async def _fresh(_connector):
        return {
            "positions": [],
            "net_liquidation": 37000.0,
            "account": {},
            "protection": {},
            "market_hours": {"session": {"status": "regular"}},
        }

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    monkeypatch.setattr("abcxauto.agent_loop.snap", _fresh)

    class _Conn:
        connected = True

    class _Chat:
        def append(self, *_a, **_k):
            pass

    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    first = json.loads(
        await _run_tool(
            "scan",
            {"scan_code": "TOP_PERC_GAIN"},
            connector=_Conn(),
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert n["calls"] == 1
    assert first.get("reused") is not True
    turn.tool_cache["scan:{}"] = json.dumps({"ok": True})
    clear_interrupt()
    note_interrupt(BookEvent("fill", "NVDA"))
    try:
        ok = await _inject_live_poke(
            _Chat(), connector=_Conn(), world=world, snap=snap, turn=turn
        )
        assert ok is True
        assert turn.tool_cache == {}
        assert turn.scan_cache
        again = json.loads(
            await _run_tool(
                "scan",
                {"scan_code": "TOP_PERC_GAIN"},
                connector=_Conn(),
                world=world,
                snap=snap,
                turn=turn,
            )
        )
    finally:
        clear_interrupt()
    assert n["calls"] == 1
    assert again["reused"] is True
    assert again["screens_this_look"] == 1


def _gain_payload():
    return {
        "ok": True,
        "source": "ibkr",
        "arena": "top_gainers",
        "scan_code": "TOP_PERC_GAIN",
        "symbols": ["SNDK"],
        "hits": [{"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0}],
        "quoted": 1,
    }


@pytest.mark.asyncio
async def test_parallel_identical_scan_args_fetch_once(monkeypatch):
    """The keep-file path: four scan() in one round, same screen, one IBKR pull."""
    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        await asyncio.sleep(0.05)
        return _gain_payload()

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    raw = await asyncio.gather(
        *[
            _run_tool(
                "scan",
                args,
                connector=None,
                world=world,
                snap=snap,
                turn=turn,
            )
            for args in (
                {"scan_code": "TOP_PERC_GAIN"},
                {"scan_code": "TOP_PERC_GAIN"},
                {"arena": "top_gainers"},
                {"scan_code": "TOP_PERC_GAIN"},
            )
        ]
    )
    rows = [json.loads(r) for r in raw]
    assert n["calls"] == 1
    assert rows[0].get("reused") is not True
    assert rows[0]["screens_this_look"] == 1
    assert sum(1 for r in rows[1:] if r.get("reused") is True) == 3
    for row in rows:
        assert row["screens_this_look"] == 1


@pytest.mark.asyncio
async def test_parallel_different_scan_args_still_fetch(monkeypatch):
    n = {"calls": 0}

    async def _fake_scan(**kw):
        n["calls"] += 1
        await asyncio.sleep(0.02)
        code = str(kw.get("scan_code") or "")
        return {
            "ok": True,
            "source": "ibkr",
            "scan_code": code or "MOST_ACTIVE",
            "symbols": ["SNDK"],
            "hits": [{"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0}],
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    await asyncio.gather(
        _run_tool(
            "scan",
            {"scan_code": "TOP_PERC_GAIN"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        ),
        _run_tool(
            "scan",
            {"scan_code": "MOST_ACTIVE"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        ),
    )
    assert n["calls"] == 2


@pytest.mark.asyncio
async def test_dispatch_one_round_same_scan_fetches_once(monkeypatch):
    """Running path: gather of reads used to launch four IBKR screens."""
    from abcxauto.brain import _dispatch_tool_calls

    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        await asyncio.sleep(0.05)
        return _gain_payload()

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)

    class _Fn:
        def __init__(self, args):
            self.name = "scan"
            self.arguments = json.dumps(args)

    class _Call:
        def __init__(self, cid, args):
            self.id = cid
            self.function = _Fn(args)

    class _Chat:
        def append(self, *_a, **_k):
            pass

    turn = BrainTurn()
    await _dispatch_tool_calls(
        [
            _Call("1", {"scan_code": "TOP_PERC_GAIN"}),
            _Call("2", {"scan_code": "TOP_PERC_GAIN"}),
            _Call("3", {"arena": "top_gainers"}),
            _Call("4", {"scan_code": "TOP_PERC_GAIN"}),
        ],
        chat=_Chat(),
        connector=None,
        world=_world(),
        snap={},
        turn=turn,
    )
    assert n["calls"] == 1
    assert turn.tool_trace.count("scan") >= 1
