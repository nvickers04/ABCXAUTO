"""IBKR scan ranking: skip-class is not ``deepest``; one tape per look."""

from __future__ import annotations

import asyncio
import json

import pytest

from abcxauto.brain import BrainTurn, _run_tool, _scan_gate_facts, _scan_look_key
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




def _tape():
    return [
        {"symbol": "PSQL", "open_gap_pct": 73.4, "last": 4.2},
        {"symbol": "TQQQ", "open_gap_pct": 12.0, "last": 88.0},
        {"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0},
    ]



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
    gate = _scan_gate_facts(_tape())
    assert gate["deepest_symbol"] == "SNDK"
    assert gate["deepest_open_gap_pct"] == pytest.approx(-6.5)


def test_skip_class_never_occupies_deepest_even_without_skip_cards():
    """Levered / micro never headline. Playbook when_on is not a floor."""
    gate = _scan_gate_facts(_tape())
    assert gate["deepest_symbol"] == "SNDK"
    assert gate["deepest_open_gap_pct"] == pytest.approx(-6.5)
    assert any(r["symbol"] == "PSQL" for r in _tape())


@pytest.mark.asyncio
async def test_scan_tool_does_not_pin_skip_class_as_deepest(monkeypatch, tmp_path):

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
    assert n["calls"] == 3
    assert first.get("reused") is not True
    assert "screens_this_look" not in first
    assert first.get("repeat_of_this_think") is not True
    assert second["reused"] is True
    assert second.get("repeat_of_this_think") is True
    assert "screens_this_look" not in second
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
    assert n["calls"] == 3
    assert alias["reused"] is True
    assert _scan_look_key({"scan_code": "TOP_PERC_GAIN"}) == _scan_look_key(
        {"arena": "top_gainers"}
    )
    assert _scan_look_key({}) == "look"
    assert _scan_look_key({"scan_code": "MOST_ACTIVE"}) == "look"


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
    assert n["calls"] == 3
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
    assert n["calls"] == 3
    assert again["reused"] is True
    assert again.get("repeat_of_this_think") is True
    assert "screens_this_look" not in again


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
    """Four scan() in one round, same flush page — one trio, then repeats."""
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
    assert n["calls"] == 3
    assert rows[0].get("reused") is not True
    assert "screens_this_look" not in rows[0]
    assert sum(1 for r in rows[1:] if r.get("reused") is True) == 3
    for row in rows:
        assert "screens_this_look" not in row
        assert row["deepest_symbol"] == rows[0]["deepest_symbol"]


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
    assert n["calls"] == 3


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
    assert n["calls"] == 3
    assert turn.tool_trace.count("scan") >= 1


def _flush_rows(code: str) -> list[dict]:
    rows = {
        "MOST_ACTIVE": [
            {"symbol": "MRVL", "open_gap_pct": -6.8, "last": 72.0},
            {"symbol": "AAPL", "open_gap_pct": -0.4, "last": 230.0},
        ],
        "TOP_PERC_LOSE": [
            {"symbol": "PSQL", "open_gap_pct": 73.4, "last": 4.2},
            {"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0},
        ],
        "TOP_PERC_GAIN": [
            {"symbol": "TQQQ", "open_gap_pct": 12.0, "last": 88.0},
        ],
        "HOT_BY_VOLUME": [
            {"symbol": "MU", "open_gap_pct": -2.1, "last": 165.0},
        ],
    }
    return list(rows.get(code, [{"symbol": "MRVL", "open_gap_pct": -6.8, "last": 72.0}]))


@pytest.mark.asyncio
async def test_bare_scan_runs_the_flush_trio(monkeypatch):
    seen: list[str] = []

    async def _fake_scan(**kw):
        code = str(kw.get("scan_code") or "").upper()
        seen.append(code)
        hits = _flush_rows(code)
        return {
            "ok": True,
            "source": "ibkr",
            "arena": kw.get("arena"),
            "scan_code": code,
            "symbols": [r["symbol"] for r in hits],
            "hits": hits,
            "quoted": len(hits),
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    data = json.loads(
        await _run_tool(
            "scan",
            {},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert seen == ["MOST_ACTIVE", "TOP_PERC_LOSE", "TOP_PERC_GAIN"]
    assert "PSQL" in data["symbols"]
    assert "MRVL" in data["symbols"]
    assert data["deepest_symbol"] == "MRVL"
    assert data["deepest_open_gap_pct"] == pytest.approx(-6.8)
    assert "screens_this_look" not in data
    arenas = data.get("arenas") or []
    assert "most_active" in arenas
    assert "top_losers" in arenas
    assert "top_gainers" in arenas


@pytest.mark.asyncio
async def test_four_arenas_one_merged_tape_and_repeat_skips_ibkr(monkeypatch, tmp_path):
    """Four different arena calls collapse to one bag; same args skip IBKR."""
    from abcxauto.opportunity_scan import overlay_hits as _real_overlay

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    ibkr = {"calls": 0}

    async def _fake_ibkr(_connector, spec):
        ibkr["calls"] += 1
        cap = spec.get("marketCapAbove")
        if cap is not None:
            assert float(cap) == pytest.approx(10_000_000_000.0)
            assert float(cap) < 200_000_000_000.0
        code = str((spec or {}).get("scanCode") or "").upper()
        hits = _flush_rows(code)
        return {
            "ok": True,
            "symbols": [r["symbol"] for r in hits],
            "rows": hits,
        }

    def _overlay(symbols, **kw):
        rows = _real_overlay(symbols, **kw)
        facts = {
            str(r.get("symbol") or "").upper(): r
            for r in (kw.get("scanner_rows") or [])
            if isinstance(r, dict)
        }
        for row in rows:
            extra = facts.get(str(row.get("symbol") or "").upper()) or {}
            for key in ("last", "open_gap_pct"):
                if extra.get(key) is not None:
                    row[key] = extra[key]
        return rows

    async def _no_quotes(rows, **_kw):
        return sum(1 for r in rows if r.get("last") is not None)

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", _fake_ibkr)
    monkeypatch.setattr("abcxauto.opportunity_scan.overlay_hits", _overlay)
    monkeypatch.setattr("abcxauto.opportunity_scan.attach_live_quotes", _no_quotes)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)

    class _Conn:
        connected = True

    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    from abcxauto.think_stream import reset_speaker, subscribe, unsubscribe

    painted: list[tuple[str, str]] = []

    def cap(kind: str, text: str, *_a) -> None:
        painted.append((kind, text))

    reset_speaker()
    subscribe(cap)
    try:
        raw = await asyncio.gather(
            *[
                _run_tool(
                    "scan",
                    args,
                    connector=_Conn(),
                    world=world,
                    snap=snap,
                    turn=turn,
                )
                for args in (
                    {"arena": "most_active"},
                    {"arena": "top_losers"},
                    {"arena": "top_gainers"},
                    {"arena": "hot_by_volume"},
                )
            ]
        )
    finally:
        unsubscribe(cap)
        reset_speaker()

    rows = [json.loads(r) for r in raw]
    bag = rows[-1]
    assert bag["ok"] is True
    names = set(bag.get("symbols") or [])
    for row in rows:
        assert row["ok"] is True
        assert row.get("deepest_symbol") == bag.get("deepest_symbol")
    assert "PSQL" in names or any(
        (r.get("symbol") == "PSQL") for r in (bag.get("hits") or [])
    )
    assert bag["deepest_symbol"] != "PSQL"
    assert bag["deepest_symbol"] != "TQQQ"
    hits_lines = [t for k, t in painted if k == "tool" and "hits=" in t]
    assert len(hits_lines) == 1
    assert "screens=" not in hits_lines[0]
    first_ibkr = ibkr["calls"]
    assert first_ibkr >= 3
    again = json.loads(
        await _run_tool(
            "scan",
            {"arena": "most_active"},
            connector=_Conn(),
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert ibkr["calls"] == first_ibkr
    assert again.get("repeat_of_this_think") is True
    assert again["deepest_symbol"] == bag["deepest_symbol"]
