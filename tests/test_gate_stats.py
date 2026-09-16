"""Gate rejection, fill-quality, and model-spend rollups on a seeded journal."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from abcxauto.gate_stats import fill_quality, gate_rejections, model_spend
from abcxauto.memory import TradeJournal, reset_journal
from abcxauto.memory.journal_support import _fill_multiplier, _split_gate_stage


NOW = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
# Wednesday 14:00 ET. RTH bell 13:30Z; ET day starts 04:00Z (EDT).
PRE_RTH = "2026-09-16T12:00:00.000Z"
IN_RTH = "2026-09-16T14:00:00.000Z"
LATER = "2026-09-16T16:30:00.000Z"


@pytest.fixture
def journal(tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    j = TradeJournal(path=str(db), enabled=True)
    yield j
    reset_journal(path=str(db), enabled=True)


def _seed_gates(journal: TradeJournal) -> None:
    pre = journal.record_proposal(
        source="clerk_block",
        strategy="vertical_spread",
        symbol="NVDA",
        validation_ok=False,
        validation_reason="kill_look: F10 spent",
        ts=PRE_RTH,
    )
    journal.record_gate_decision(pre, False, "kill_look: F10 spent", ts=PRE_RTH)
    a = journal.record_proposal(
        source="clerk_block",
        strategy="bracket",
        symbol="AAPL",
        validation_ok=False,
        validation_reason="size: above max_risk_per_trade_pct",
        ts=IN_RTH,
    )
    journal.record_gate_decision(a, False, "size: above max_risk_per_trade_pct", ts=IN_RTH)
    b = journal.record_proposal(
        source="clerk_block",
        strategy="bracket",
        symbol="MSFT",
        validation_ok=False,
        validation_reason="size: above max_risk_per_trade_pct",
        ts=LATER,
    )
    journal.record_gate_decision(b, False, "size: above max_risk_per_trade_pct", ts=LATER)
    c = journal.record_proposal(
        source="executor",
        strategy="market_bracket",
        symbol="SPY",
        validation_ok=True,
        ts=LATER,
    )
    journal.record_gate_decision(c, False, "daily loss", ts=LATER)
    ok = journal.record_proposal(strategy="hold", symbol="QQQ", ts=LATER)
    journal.record_gate_decision(ok, True, "ok", ts=LATER)


def _seed_fills(journal: TradeJournal) -> None:
    pid = journal.record_proposal(strategy="bracket", symbol="AAPL", ts=IN_RTH)
    did = journal.record_dispatch(pid, True, {"success": True, "order_id": 88}, ts=IN_RTH)
    journal.record_send_marks(
        proposal_id=pid,
        dispatch_id=did,
        marks={
            "order_id": 88,
            "symbol": "AAPL",
            "strategy": "bracket",
            "side": "BUY",
            "ibkr_last": 100.0,
            "sent_price": 100.10,
            "fill_price": 100.20,
            "signed_slippage": 0.10,
            "status": "filled",
        },
        result={"order_id": 88},
        ts=IN_RTH,
    )
    journal.record_fills(
        [
            {
                "ts": IN_RTH,
                "exec_id": "stk-1",
                "order_id": 88,
                "symbol": "AAPL",
                "sec_type": "STK",
                "side": "BOT",
                "quantity": 10,
                "price": 100.20,
                "ibkr_last": 100.0,
                "sent_price": 100.10,
                "signed_slippage": 0.10,
            },
            {
                "ts": LATER,
                "exec_id": "opt-1",
                "order_id": 501,
                "symbol": "NVDA",
                "sec_type": "OPT",
                "side": "BOT",
                "quantity": 2,
                "price": 1.20,
                "sent_price": 1.10,
                "ibkr_last": 1.10,
                "signed_slippage": 0.10,
                "multiplier": 100,
            },
            {
                "ts": PRE_RTH,
                "exec_id": "pre-1",
                "order_id": 9,
                "symbol": "MSFT",
                "sec_type": "STK",
                "side": "BOT",
                "quantity": 1,
                "price": 50.05,
                "sent_price": 50.00,
                "signed_slippage": 0.05,
            },
        ]
    )


def test_split_gate_stage_and_multiplier_helpers():
    assert _split_gate_stage("kill_look: no bag") == ("kill_look", "no bag")
    assert _split_gate_stage("daily loss") == ("", "daily loss")
    assert _fill_multiplier({"sec_type": "OPT"}) == 100.0
    assert _fill_multiplier({"sec_type": "STK"}) == 1.0
    assert _fill_multiplier({"sec_type": "OPT", "multiplier": 1000}) == 1000.0


def test_record_gate_decision_stores_parsed_stage(journal):
    pid = journal.record_proposal(strategy="bracket", symbol="X", ts=IN_RTH)
    journal.record_gate_decision(pid, False, "look_numbers: last missing", ts=IN_RTH)
    journal.record_gate_decision(pid, False, "daily loss", ts=IN_RTH, stage="risk_gate")
    import sqlite3

    with sqlite3.connect(journal.path) as conn:
        rows = conn.execute(
            "SELECT reason, stage FROM gate_decisions ORDER BY id"
        ).fetchall()
    assert rows[0] == ("look_numbers: last missing", "look_numbers")
    assert rows[1] == ("daily loss", "risk_gate")


def test_gate_rejections_session_and_day(journal):
    _seed_gates(journal)
    out = gate_rejections(journal=journal, now=NOW)
    session = out["session"]
    day = out["day"]
    assert session["session_date"] == "2026-09-16"
    assert session["total"] == 3
    assert day["total"] == 4
    size = next(r for r in session["by_reason"] if r["reason"] == "above max_risk_per_trade_pct")
    assert size["count"] == 2
    assert size["stage"] == "size"
    assert size["latest"]["symbol"] == "MSFT"
    daily = next(r for r in session["by_reason"] if r["reason"] == "daily loss")
    assert daily["stage"] == ""
    assert daily["count"] == 1
    stages = {row["stage"]: row["count"] for row in session["by_stage"]}
    assert stages["size"] == 2
    kill = next(r for r in day["by_reason"] if r["stage"] == "kill_look")
    assert kill["count"] == 1
    assert kill["latest"]["symbol"] == "NVDA"


def test_fill_quality_applies_option_multiplier(journal):
    _seed_fills(journal)
    out = fill_quality(journal=journal, now=NOW)
    session = out["session"]
    day = out["day"]
    by_exec = {row["exec_id"]: row for row in session["fills"]}
    assert set(by_exec) == {"stk-1", "opt-1"}
    stk = by_exec["stk-1"]
    assert stk["multiplier"] == 1.0
    assert stk["benchmark"] == "limit"
    assert stk["signed_slippage"] == pytest.approx(0.10)
    assert stk["slippage_usd"] == pytest.approx(1.0)
    opt = by_exec["opt-1"]
    assert opt["multiplier"] == 100.0
    assert opt["slippage_usd"] == pytest.approx(20.0)
    assert session["aggregate"]["n"] == 2
    assert session["aggregate"]["sum_slippage_usd"] == pytest.approx(21.0)
    assert day["aggregate"]["n"] == 3
    pre = next(row for row in day["fills"] if row["exec_id"] == "pre-1")
    assert pre["slippage_usd"] == pytest.approx(0.05)


def test_record_fills_persists_multiplier(journal):
    journal.record_fills(
        [
            {
                "ts": IN_RTH,
                "exec_id": "m1",
                "order_id": 1,
                "symbol": "QQQ",
                "sec_type": "OPT",
                "quantity": 1,
                "price": 2.0,
            }
        ]
    )
    import sqlite3

    with sqlite3.connect(journal.path) as conn:
        mult = conn.execute(
            "SELECT multiplier FROM fills WHERE exec_id = 'm1'"
        ).fetchone()[0]
    assert float(mult) == 100.0


def test_model_spend_reuses_scorecard_session(journal):
    journal.record_snapshot(
        account={"NetLiquidation": 10_000.0, "DailyPnL": 0.0},
        ts="2026-09-16T13:35:00.000Z",
    )
    journal.ensure_model_session("grok-4.6", net_liquidation=10_000.0, ts="2026-09-16T13:35:00.000Z")
    journal.record_snapshot(
        account={"NetLiquidation": 10_100.0, "DailyPnL": 100.0},
        ts="2026-09-16T17:00:00.000Z",
    )
    journal.record_model_usage(
        stage="grok",
        model="grok-4.6",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=4.0,
        ts=IN_RTH,
    )
    out = model_spend(journal=journal, equity=10_100.0, now=NOW)
    session = out["session"]
    day = out["day"]
    assert session["session_date"] == "2026-09-16"
    assert session["model_cost_usd"] == pytest.approx(4.0)
    assert session["model_calls"] == 1
    assert session["book_pnl"] == pytest.approx(100.0)
    assert session["book_return_pct"] == pytest.approx(1.0)
    assert session["edge_usd"] == pytest.approx(96.0)
    assert session["beating_model"] is True
    assert day["day"] == "2026-09-16"
    assert day["model_cost_usd"] == pytest.approx(4.0)
    assert day["book_pnl"] == pytest.approx(100.0)
    assert day["beating_model"] is True


def test_cockpit_helpers_are_plain_functions(journal):
    _seed_gates(journal)
    _seed_fills(journal)
    journal.record_snapshot(account={"NetLiquidation": 1_000.0}, ts=IN_RTH)
    assert callable(gate_rejections)
    assert callable(fill_quality)
    assert callable(model_spend)
    rolled = gate_rejections(journal=journal, now=NOW)
    assert "session" in rolled and "day" in rolled
    assert "by_reason" in rolled["session"]
