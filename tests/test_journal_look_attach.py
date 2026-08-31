"""Look snap + LLM usage land on the live journal."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from abcxauto.brain import BrainTurn
from abcxauto.memory import get_journal
from abcxauto.path_math import conservative_trade_pnl
from abcxauto.pro_engine import ProEngine
from abcxauto.scorecard import compute_scorecard, estimate_tokens


def _conn():
    class Conn:
        connected = True

        async def get_positions(self):
            return []

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 50000, "dailypnl": 12.0}

    return Conn()


def _fill(exec_id, oid, *, side, price, bid, ask, qty=1, comm=1.0, ts="2026-08-31T14:00:00Z"):
    return {
        "exec_id": exec_id,
        "order_id": oid,
        "symbol": "SPY",
        "sec_type": "STK",
        "side": side,
        "quantity": qty,
        "price": price,
        "commission": comm,
        "bid": bid,
        "ask": ask,
        "ts": ts,
    }


@pytest.mark.asyncio
async def test_host_think_ingests_look_snap_fills(monkeypatch):
    async def grok(*_a, **_k):
        return BrainTurn(text="watching the book")

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok)
    snap = {
        "account": {"netliquidation": 50000.0, "dailypnl": 12.0, "totalcashvalue": 40000.0},
        "positions": [{"symbol": "SPY", "quantity": 1, "sec_type": "STK"}],
        "open_orders": [],
        "fills": [
            _fill("e-bot", 101, side="BOT", price=500.1, bid=500.0, ask=500.2),
        ],
        "protection": {},
        "reality_pulse": {"session": {"status": "regular"}},
        "taken_at": "2026-08-31T14:00:00Z",
    }
    eng = ProEngine()
    eng.conn = _conn()
    await eng._host_think(1, None, snap)
    j = get_journal()
    with sqlite3.connect(j.path) as conn:
        snaps = conn.execute("SELECT net_liquidation, daily_pnl FROM snapshots").fetchall()
        fills = conn.execute(
            "SELECT exec_id, price, bid, ask, commission FROM fills"
        ).fetchall()
    assert snaps
    assert snaps[-1][0] == 50000.0
    assert fills
    assert fills[-1][0] == "e-bot"
    assert fills[-1][1] == 500.1
    assert fills[-1][2] == 500.0
    assert fills[-1][3] == 500.2


@pytest.mark.asyncio
async def test_spoken_look_bills_model_cost_when_sdk_usage_empty(monkeypatch):
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt
    from abcxauto.world_state import WorldState

    clear_interrupt()

    async def fake_stream(chat, **_k):
        return "watching IWM. No ticket this look.", SimpleNamespace(tool_calls=[]), "ok"

    monkeypatch.setattr("abcxauto.brain.stream_round", fake_stream)

    class Chat:
        def append(self, *_a, **_k):
            pass

        async def stream(self):
            if False:
                yield None

    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: Chat())),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=None,
        _wake_n=0,
    )
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=50000.0,
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
    before = get_journal().model_usage_totals()
    turn = await grok_turn(
        g, connector=None, world=world, snap={}, wake="session=regular send."
    )
    assert "watching IWM" in (turn.text or "")
    after = get_journal().model_usage_totals()
    assert after["calls"] > before["calls"]
    assert after["cost_usd"] > before["cost_usd"]
    assert after["output_tokens"] >= estimate_tokens("watching IWM. No ticket this look.")


def test_ingest_look_fills_give_honest_conservative_pnl_not_mids():
    entry = _fill("e-open", 201, side="BOT", price=500.0, bid=499.8, ask=500.2, comm=1.0)
    closer = _fill(
        "e-close",
        202,
        side="SLD",
        price=510.0,
        bid=509.8,
        ask=510.2,
        comm=1.0,
        ts="2026-08-31T15:00:00Z",
    )
    get_journal().ingest_look(
        {
            "account": {"netliquidation": 50000.0},
            "positions": [],
            "open_orders": [],
            "fills": [entry, closer],
        }
    )
    pnl = conservative_trade_pnl([entry, closer])
    assert pnl is not None
    # BUY at ask 500.2, SELL at bid 509.8, minus $2 commission. Not the $10 mid.
    assert pnl != 10.0
    assert pnl < 10.0


def test_compute_scorecard_reads_journal_cost_and_book_return():
    j = get_journal()
    j.ingest_look(
        {
            "account": {"netliquidation": 1000.0, "dailypnl": 0.0},
            "positions": [],
            "open_orders": [],
            "fills": [],
            "taken_at": "2026-08-31T13:30:00Z",
        }
    )
    j.record_model_usage(
        stage="grok",
        model="grok-4.6",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.005,
    )
    j.ingest_look(
        {
            "account": {"netliquidation": 1100.0, "dailypnl": 100.0},
            "positions": [],
            "open_orders": [],
            "fills": [],
            "taken_at": "2026-08-31T20:00:00Z",
        }
    )
    sc = compute_scorecard(equity=1100.0, journal=j)
    assert sc["model_cost_usd"] is not None
    assert sc["model_cost_usd"] >= 0.005
    assert sc.get("book_return_pct") is not None


def test_run_cycle_is_not_the_journal_writer():
    import abcxauto.agent_loop as agent_loop

    assert not hasattr(agent_loop, "run_cycle")
    assert hasattr(get_journal(), "ingest_look")
