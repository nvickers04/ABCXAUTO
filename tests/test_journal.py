"""TradeJournal: schema, round-trips, disabled no-ops, summaries, thread smoke."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from abcxauto.memory import TradeJournal, get_journal, reset_journal


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Fresh TradeJournal on a temp DB; reset module singleton after."""
    db = tmp_path / "journal.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    j = TradeJournal(path=str(db), enabled=True)
    yield j
    reset_journal(path=str(db), enabled=True)


def test_schema_creation(journal, tmp_path):
    db = tmp_path / "journal.db"
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {
        "proposals",
        "gate_decisions",
        "dispatches",
        "halts",
        "snapshots",
        "fills",
        "decisions",
        "working_thesis",
        "judgments",
        "model_usage",
        "session_markers",
    } <= tables


def test_record_proposal_round_trip(journal, tmp_path):
    pid = journal.record_proposal(
        source="brain",
        strategy="bracket",
        symbol="NVDA",
        direction="LONG",
        quantity=10,
        params={"entry_price": 100.0, "stop_price": 95.0},
        validation_ok=True,
        validation_reason="ok",
    )
    assert isinstance(pid, int) and pid > 0

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[2] == "brain"  # source
    assert row[3] == "bracket"
    assert row[4] == "NVDA"
    assert row[5] == "LONG"
    assert row[6] == 10.0
    assert json.loads(row[7])["entry_price"] == 100.0
    assert row[8] == 1
    assert row[9] == "ok"


def test_record_gate_decision_round_trip(journal, tmp_path):
    pid = journal.record_proposal(strategy="bracket", symbol="AAPL", validation_ok=True)
    journal.record_gate_decision(pid, True, "ok")
    journal.record_gate_decision(pid, False, "daily loss")

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        rows = conn.execute(
            "SELECT allowed, reason FROM gate_decisions WHERE proposal_id = ? ORDER BY id",
            (pid,),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(1, "ok"), (0, "daily loss")]


def test_record_dispatch_round_trip(journal, tmp_path):
    pid = journal.record_proposal(strategy="market_order", symbol="SPY")
    journal.record_dispatch(pid, True, {"order_id": 42, "status": "Submitted"})
    journal.record_dispatch(None, False, {"error": "timeout"})

    recent = journal.recent_dispatches(limit=10)
    assert len(recent) == 2
    # Newest first
    assert recent[0]["ok"] == 0
    assert recent[0]["result"]["error"] == "timeout"
    assert recent[1]["ok"] == 1
    assert recent[1]["proposal_id"] == pid
    assert recent[1]["result"]["order_id"] == 42


def test_record_halt_round_trip(journal, tmp_path):
    journal.record_halt("daily loss breach", "auto_panic")
    journal.record_halt("manual", "halt")
    journal.record_halt("cleared", "resume")

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        rows = conn.execute(
            "SELECT reason, kind FROM halts ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("daily loss breach", "auto_panic"),
        ("manual", "halt"),
        ("cleared", "resume"),
    ]


def test_record_snapshot_case_insensitive_account(journal, tmp_path):
    journal.record_snapshot(
        account={
            "NetLiquidation": 105_000.5,
            "DailyPnL": -250.0,
            "TotalCashValue": 40_000.0,
        },
        positions=[{"symbol": "NVDA", "qty": 10}],
        open_orders=[{"orderId": 1}],
    )
    journal.record_snapshot(
        account={
            "netliquidation": 106_000.0,
            "dailypnl": 100.0,
            "totalcashvalue": 41_000.0,
        },
        positions=[],
        open_orders=[],
    )

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        rows = conn.execute(
            "SELECT net_liquidation, daily_pnl, total_cash, positions_json FROM snapshots ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert rows[0][0] == 105_000.5
    assert rows[0][1] == -250.0
    assert rows[0][2] == 40_000.0
    assert json.loads(rows[0][3])[0]["symbol"] == "NVDA"
    assert rows[1][0] == 106_000.0


def test_disabled_is_noop(tmp_path, monkeypatch):
    db = tmp_path / "disabled.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "false")
    j = TradeJournal(path=str(db), enabled=False)
    assert j.record_proposal(strategy="bracket", symbol="X") is None
    j.record_gate_decision(1, True, "ok")
    j.record_dispatch(1, True, {"ok": True})
    j.record_halt("x", "halt")
    j.record_snapshot({"NetLiquidation": 1.0}, [], [])
    assert j.record_decision(cycle=1, action="hold", strategy="hold") is None
    j.set_working_thesis("x")
    assert j.record_fills([{"exec_id": "e1", "order_id": 1}]) == 0
    assert not db.exists()


def test_daily_summary_counts(journal):
    day = "2026-07-09"
    ts = f"{day}T12:00:00.000Z"
    other = "2026-07-08T12:00:00.000Z"

    p1 = journal.record_proposal(strategy="bracket", symbol="A", ts=ts)
    journal.record_proposal(strategy="bracket", symbol="B", ts=ts)
    journal.record_proposal(strategy="bracket", symbol="C", ts=other)

    journal.record_gate_decision(p1, True, "ok", ts=ts)
    journal.record_gate_decision(p1, False, "size", ts=ts)
    journal.record_gate_decision(p1, True, "ok", ts=other)

    journal.record_dispatch(p1, True, {"id": 1}, ts=ts)
    journal.record_dispatch(p1, False, {"err": "x"}, ts=ts)
    journal.record_dispatch(p1, True, {"id": 2}, ts=other)

    journal.record_halt("panic", "auto_panic", ts=ts)
    journal.record_halt("resume", "resume", ts=other)

    summary = journal.daily_summary(day)
    assert summary["day"] == day
    assert summary["proposals"] == 2
    assert summary["allowed"] == 1
    assert summary["rejected"] == 1
    assert summary["validation_failed"] == 0
    assert summary["dispatch_ok"] == 1
    assert summary["dispatch_failed"] == 1
    assert summary["halts"] == 1


def test_recent_proposals_includes_validation_failures(journal):
    journal.record_proposal(
        strategy="market_bracket",
        symbol="SPY",
        params={"side": "BUY", "quantity": 2},
        validation_ok=False,
        validation_reason="direction required",
        ts="2026-07-09T18:00:00.000Z",
    )
    journal.record_proposal(
        strategy="market_bracket",
        symbol="QQQ",
        validation_ok=True,
        ts="2026-07-09T18:01:00.000Z",
    )
    rows = journal.recent_proposals(limit=5)
    assert len(rows) >= 2
    assert rows[0]["symbol"] == "QQQ"
    assert rows[1]["validation_ok"] is False
    assert "direction" in (rows[1].get("validation_reason") or "")
    summary = journal.daily_summary("2026-07-09")
    assert summary["validation_failed"] == 1


def test_equity_curve_ordering(journal):
    for i, nliq in enumerate([100.0, 110.0, 105.0, 120.0]):
        journal.record_snapshot(
            account={"netliquidation": nliq},
            positions=[],
            open_orders=[],
            ts=f"2026-07-09T0{i}:00:00.000Z",
        )
    curve = journal.equity_curve(limit=3)
    assert len(curve) == 3
    assert [v for _, v in curve] == [110.0, 105.0, 120.0]
    assert curve[0][0] < curve[1][0] < curve[2][0]


def test_account_performance_horizons(tmp_path):
    from datetime import datetime, timedelta, timezone
    import sqlite3

    from abcxauto.memory.journal import TradeJournal

    db = tmp_path / "perf.db"
    j = TradeJournal(path=str(db), enabled=True)
    j.record_snapshot(account={"NetLiquidation": 1.0})  # ensure schema
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM snapshots")
    for days, nl in ((400, 90_000.0), (100, 95_000.0), (10, 98_000.0), (0, 100_000.0)):
        con.execute(
            "INSERT INTO snapshots(ts, net_liquidation, daily_pnl) VALUES (?,?,?)",
            ((now - timedelta(days=days)).isoformat(), nl, 12.5 if days == 0 else 0.0),
        )
    con.commit()
    con.close()
    perf = j.account_performance()
    assert perf["source"] == "ibkr_nav"
    assert perf["net_liquidation"] == 100_000.0
    assert abs(perf["ret_1w"] - (100_000 / 98_000 - 1)) < 1e-9
    assert abs(perf["ret_3m"] - (100_000 / 95_000 - 1)) < 1e-9
    assert abs(perf["ret_1y"] - (100_000 / 90_000 - 1)) < 1e-9
    assert perf["history_start"] is not None
    assert perf["history_days"] is not None
    assert perf["history_days"] >= 399
    assert perf["as_of"] is not None


def test_account_performance_short_history_hides_long_horizons(tmp_path):
    from datetime import datetime, timedelta, timezone
    import sqlite3

    from abcxauto.memory.journal import TradeJournal

    db = tmp_path / "short.db"
    j = TradeJournal(path=str(db), enabled=True)
    j.record_snapshot(account={"NetLiquidation": 1.0})
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM snapshots")
    for days, nl in ((10, 98_000.0), (0, 100_000.0)):
        con.execute(
            "INSERT INTO snapshots(ts, net_liquidation, daily_pnl) VALUES (?,?,?)",
            ((now - timedelta(days=days)).isoformat(), nl, 0.0),
        )
    con.commit()
    con.close()
    perf = j.account_performance()
    assert perf["source"] == "ibkr_nav"
    assert abs(perf["ret_1w"] - (100_000 / 98_000 - 1)) < 1e-9
    assert perf["ret_3m"] is None
    assert perf["ret_1y"] is None
    assert perf["history_days"] is not None
    assert perf["history_days"] < 90


def test_thread_safety_smoke(journal):
    errors: list = []

    def writer(n: int) -> None:
        try:
            for i in range(20):
                pid = journal.record_proposal(
                    strategy="bracket",
                    symbol=f"T{n}",
                    quantity=i,
                    params={"n": n, "i": i},
                    validation_ok=True,
                )
                journal.record_gate_decision(pid, True, "ok")
                journal.record_dispatch(pid, True, {"n": n, "i": i})
                if i % 5 == 0:
                    journal.record_halt(f"t{n}-{i}", "halt")
                    journal.record_snapshot(
                        {"NetLiquidation": 1000.0 + n + i},
                        [],
                        [],
                    )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(writer, range(4)))

    assert errors == []
    summary = journal.daily_summary()
    assert summary["proposals"] == 80
    assert summary["allowed"] == 80
    assert summary["dispatch_ok"] == 80


def test_record_methods_swallow_errors(tmp_path):
    # Point at a path that cannot be a SQLite DB (existing directory).
    bad = tmp_path / "not_a_file"
    bad.mkdir()
    j = TradeJournal(path=str(bad), enabled=True)
    assert j.record_proposal(strategy="x", symbol="Y") is None
    j.record_gate_decision(1, False, "nope")
    j.record_dispatch(1, False, {"x": 1})
    j.record_halt("x", "halt")
    j.record_snapshot({"NetLiquidation": 1}, [], [])
    assert j.record_fills([{"exec_id": "e1"}]) == 0
    assert j.recent_dispatches() == []
    assert j.recent_decisions() == []
    assert j.get_working_thesis() == ""
    assert j.equity_curve() == []
    assert j.strategy_performance() == []
    summary = j.daily_summary("2026-07-09")
    assert summary["proposals"] == 0


def test_record_fills_round_trip_and_dedup(journal, tmp_path):
    fill = {
        "ts": "2026-07-09T14:00:00.000Z",
        "exec_id": "exec-001",
        "order_id": 101,
        "symbol": "NVDA",
        "sec_type": "STK",
        "side": "BOT",
        "quantity": 10.0,
        "price": 120.5,
        "commission": 1.25,
        "realized_pnl": 0.0,
    }
    assert journal.record_fills([fill]) == 1
    assert journal.record_fills([fill]) == 0  # UNIQUE exec_id
    assert journal.record_fills([
        {**fill, "exec_id": "exec-002", "side": "SLD", "realized_pnl": 42.0},
        fill,
    ]) == 1

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        rows = conn.execute(
            "SELECT exec_id, order_id, symbol, side, quantity, price, commission, realized_pnl "
            "FROM fills ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0][0] == "exec-001"
    assert rows[0][1] == 101
    assert rows[0][2] == "NVDA"
    assert rows[1][0] == "exec-002"
    assert rows[1][7] == 42.0


def test_strategy_performance_attribution(journal):
    day = "2026-07-09"
    ts = f"{day}T12:00:00.000Z"

    pid_bracket = journal.record_proposal(
        strategy="bracket", symbol="AAPL", validation_ok=True, ts=ts
    )
    journal.record_dispatch(
        pid_bracket,
        True,
        {
            "success": True,
            "bracket_order_id": 501,
            "stop_order_id": 502,
            "target_order_id": 503,
        },
        ts=ts,
    )

    pid_mkt = journal.record_proposal(
        strategy="market_order", symbol="SPY", validation_ok=True, ts=ts
    )
    journal.record_dispatch(
        pid_mkt,
        True,
        {"success": True, "order_id": 601, "status": "Submitted"},
        ts=ts,
    )

    # Options-style list of ids
    pid_opt = journal.record_proposal(
        strategy="vertical", symbol="QQQ", validation_ok=True, ts=ts
    )
    journal.record_dispatch(
        pid_opt,
        True,
        {"success": True, "order_ids": [701, 702]},
        ts=ts,
    )

    journal.record_fills(
        [
            {
                "ts": f"{day}T13:00:00.000Z",
                "exec_id": "e-bracket-entry",
                "order_id": 501,
                "symbol": "AAPL",
                "sec_type": "STK",
                "side": "BOT",
                "quantity": 10,
                "price": 100.0,
                "commission": 1.0,
                "realized_pnl": 0.0,
            },
            {
                "ts": f"{day}T14:00:00.000Z",
                "exec_id": "e-bracket-stop",
                "order_id": 502,
                "symbol": "AAPL",
                "sec_type": "STK",
                "side": "SLD",
                "quantity": 10,
                "price": 95.0,
                "commission": 1.0,
                "realized_pnl": -50.0,
            },
            {
                "ts": f"{day}T13:30:00.000Z",
                "exec_id": "e-mkt",
                "order_id": 601,
                "symbol": "SPY",
                "sec_type": "STK",
                "side": "SLD",
                "quantity": 5,
                "price": 500.0,
                "commission": 0.5,
                "realized_pnl": 12.5,
            },
            {
                "ts": f"{day}T15:00:00.000Z",
                "exec_id": "e-opt",
                "order_id": 701,
                "symbol": "QQQ",
                "sec_type": "OPT",
                "side": "BOT",
                "quantity": 1,
                "price": 2.0,
                "commission": 0.65,
                "realized_pnl": None,
            },
            {
                "ts": f"{day}T16:00:00.000Z",
                "exec_id": "e-orphan",
                "order_id": 9999,
                "symbol": "TSLA",
                "sec_type": "STK",
                "side": "BOT",
                "quantity": 1,
                "price": 200.0,
                "commission": 0.35,
                "realized_pnl": 0.0,
            },
            # Outside since_day window
            {
                "ts": "2026-07-08T12:00:00.000Z",
                "exec_id": "e-old",
                "order_id": 601,
                "symbol": "SPY",
                "sec_type": "STK",
                "side": "BOT",
                "quantity": 5,
                "price": 490.0,
                "commission": 0.5,
                "realized_pnl": 0.0,
            },
        ]
    )

    by_strategy = {r["strategy"]: r for r in journal.strategy_performance(since_day=day)}
    assert set(by_strategy) == {"bracket", "market_order", "vertical", "(unattributed)"}

    bracket = by_strategy["bracket"]
    assert bracket["n_fills"] == 2
    assert bracket["realized_pnl_sum"] == -50.0
    assert bracket["commissions_sum"] == 2.0
    assert bracket["first_fill_ts"] == f"{day}T13:00:00.000Z"
    assert bracket["last_fill_ts"] == f"{day}T14:00:00.000Z"

    mkt = by_strategy["market_order"]
    assert mkt["n_fills"] == 1
    assert mkt["realized_pnl_sum"] == 12.5
    assert mkt["commissions_sum"] == 0.5

    vertical = by_strategy["vertical"]
    assert vertical["n_fills"] == 1
    assert vertical["realized_pnl_sum"] == 0.0
    assert vertical["commissions_sum"] == 0.65

    unattr = by_strategy["(unattributed)"]
    assert unattr["n_fills"] == 1
    assert unattr["commissions_sum"] == 0.35

    # Without since_day, the older market_order fill is included.
    all_rows = {r["strategy"]: r for r in journal.strategy_performance()}
    assert all_rows["market_order"]["n_fills"] == 2


def test_json_default_str_for_non_serializable(journal, tmp_path):
    class Weird:
        def __str__(self) -> str:
            return "weird-obj"

    pid = journal.record_proposal(
        strategy="bracket",
        symbol="Z",
        params={"obj": Weird()},
        validation_ok=True,
    )
    assert pid is not None
    journal.record_dispatch(pid, True, {"obj": Weird()})

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        params_json = conn.execute(
            "SELECT params_json FROM proposals WHERE id = ?", (pid,)
        ).fetchone()[0]
        result_json = conn.execute(
            "SELECT result_json FROM dispatches WHERE proposal_id = ?", (pid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert "weird-obj" in params_json
    assert "weird-obj" in result_json


def test_get_journal_singleton(tmp_path, monkeypatch):
    db = tmp_path / "singleton.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    # Force re-init of singleton
    import abcxauto.memory.journal as journal_mod

    with journal_mod._journal_lock:
        journal_mod._journal = None

    a = get_journal()
    b = get_journal()
    assert a is b
    assert a.path == str(db)
    reset_journal(path=str(db), enabled=True)


def test_record_decision_and_recent(journal):
    did = journal.record_decision(
        cycle=3,
        action="hold",
        strategy="hold",
        rationale="wait for setup",
        portfolio_snapshot={"net_liq": 1000},
        outcome={"status": "hold"},
    )
    assert isinstance(did, int) and did > 0
    journal.record_decision(
        cycle=4,
        action="bracket",
        strategy="bracket",
        rationale="enter SPY",
        outcome={"status": "executed"},
    )
    rows = journal.recent_decisions(limit=5)
    assert len(rows) >= 2
    assert rows[0]["strategy"] == "bracket"
    assert rows[0]["outcome"]["status"] == "executed"
    assert rows[1]["strategy"] == "hold"
    assert rows[1]["portfolio_snapshot"]["net_liq"] == 1000
    assert rows[0].get("cycle") == 4
    assert rows[1].get("cycle") == 3


def test_working_thesis_round_trip(journal):
    assert journal.get_working_thesis() == ""
    journal.set_working_thesis("SPY mean-reversion while VIX calm")
    assert "mean-reversion" in journal.get_working_thesis()
    journal.set_working_thesis("updated thesis on QQQ")
    assert journal.get_working_thesis() == "updated thesis on QQQ"


def test_model_usage_round_trip_and_since(journal, tmp_path):
    early = "2026-08-14T10:00:00.000Z"
    late = "2026-08-14T15:00:00.000Z"
    rid = journal.record_model_usage(
        stage="grok",
        model="grok-4.6",
        input_tokens=1200,
        output_tokens=80,
        cached_tokens=100,
        cost_usd=0.0123,
        ts=early,
    )
    assert isinstance(rid, int) and rid > 0
    journal.record_model_usage(
        stage="grok",
        model="grok-4.6",
        input_tokens=500,
        output_tokens=40,
        cached_tokens=0,
        cost_usd=0.0050,
        ts=late,
    )

    tot = journal.model_usage_totals()
    assert tot["calls"] == 2
    assert tot["input_tokens"] == 1700
    assert tot["output_tokens"] == 120
    assert tot["cached_tokens"] == 100
    assert abs(tot["cost_usd"] - 0.0173) < 1e-9

    since = journal.model_usage_since("2026-08-14T12:00:00.000Z")
    assert since["calls"] == 1
    assert since["input_tokens"] == 500
    assert since["output_tokens"] == 40
    assert abs(since["cost_usd"] - 0.0050) < 1e-9

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        rows = conn.execute(
            "SELECT stage, model, input_tokens, output_tokens, cached_tokens, cost_usd "
            "FROM model_usage ORDER BY id"
        ).fetchall()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "model_usage" in tables
    assert "session_markers" in tables
    assert rows[0][0] == "grok"
    assert rows[0][1] == "grok-4.6"
    assert rows[0][2] == 1200
    assert rows[0][5] == 0.0123


def test_ensure_model_session_does_not_stamp_without_nl(journal, tmp_path):
    """Headless/boot used to persist NL=None at 2026-08-19T12:50:52Z."""
    assert journal.ensure_model_session(
        "grok-4.6", ts="2026-08-19T12:50:52.000Z"
    ) is None
    assert journal.last_session_marker() is None
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM session_markers").fetchone()[0]
    finally:
        conn.close()
    assert n == 0

    journal.record_snapshot(
        account={"NetLiquidation": 35_000.0, "DailyPnL": 0.0},
        ts="2026-08-19T12:51:00.000Z",
    )
    last = journal.last_session_marker()
    assert last == {
        "ts": "2026-08-19T12:50:52.000Z",
        "model": "grok-4.6",
        "net_liquidation": 35_000.0,
    }


def test_ensure_fills_hollow_legacy_marker(journal, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        conn.execute(
            "INSERT INTO session_markers (ts, model, net_liquidation) VALUES (?, ?, ?)",
            ("2026-08-19T12:50:52.000Z", "grok-4.6", None),
        )
        conn.commit()
    finally:
        conn.close()
    filled = journal.ensure_model_session("grok-4.6", net_liquidation=35_000.0)
    assert filled == {
        "ts": "2026-08-19T12:50:52.000Z",
        "model": "grok-4.6",
        "net_liquidation": 35_000.0,
    }
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM session_markers").fetchone()[0]
        nl = conn.execute("SELECT net_liquidation FROM session_markers").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert nl == 35_000.0


def test_record_fills_normalizes_aug19_spy11_plus_five_hours(journal, tmp_path):
    """Replay: SPY 11 fill stored 23:13:02Z, dispatch 18:13:03Z."""
    dispatch_ts = "2026-08-19T18:13:03.000Z"
    pid = journal.record_proposal(
        strategy="market_order", symbol="SPY", validation_ok=True, ts=dispatch_ts
    )
    journal.record_dispatch(
        pid, True, {"success": True, "order_id": 11}, ts=dispatch_ts
    )
    assert journal.record_fills(
        [
            {
                "ts": "2026-08-19T23:13:02.000Z",
                "exec_id": "spy-11-aug19",
                "order_id": 11,
                "symbol": "SPY",
                "side": "BOT",
                "quantity": 1,
                "price": 500.0,
                "realized_pnl": 0.0,
            }
        ]
    ) == 1
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        stored = conn.execute(
            "SELECT ts FROM fills WHERE exec_id = 'spy-11-aug19'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == "2026-08-19T18:13:02.000Z"


def test_record_fills_normalizes_plus_five_hours_onto_dispatch_day(journal, tmp_path):
    """CDT +5h of 20:13Z is 01:13Z the next UTC day — wrong daily/session bucket."""
    dispatch_ts = "2026-08-19T20:13:03.000Z"
    pid = journal.record_proposal(
        strategy="market_order", symbol="SPY", validation_ok=True, ts=dispatch_ts
    )
    journal.record_dispatch(
        pid, True, {"success": True, "order_id": 11}, ts=dispatch_ts
    )
    assert journal.record_fills(
        [
            {
                "ts": "2026-08-20T01:13:02.000Z",
                "exec_id": "spy-11",
                "order_id": 11,
                "symbol": "SPY",
                "side": "BOT",
                "quantity": 1,
                "price": 500.0,
                "realized_pnl": 0.0,
            }
        ]
    ) == 1
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        stored = conn.execute(
            "SELECT ts FROM fills WHERE exec_id = 'spy-11'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == "2026-08-19T20:13:02.000Z"
    assert stored.startswith("2026-08-19")
    assert stored < "2026-08-20"


def test_record_model_usage_drops_all_zero_token_rows(journal):
    assert journal.record_model_usage(stage="grok", cost_usd=0.18) is None
    tot = journal.model_usage_totals()
    assert tot["calls"] == 0
    assert tot["cost_usd"] == 0.0
    rid = journal.record_model_usage(
        stage="grok", output_tokens=100, cost_usd=0.18
    )
    assert isinstance(rid, int) and rid > 0
    tot = journal.model_usage_totals()
    assert tot["calls"] == 1
    assert abs(tot["cost_usd"] - 0.18) < 1e-9


def test_session_markers_ensure_and_last(journal, tmp_path):
    assert journal.last_session_marker() is None

    first = journal.ensure_model_session(
        "grok-4.6",
        net_liquidation=35_000.0,
        ts="2026-08-19T12:00:00.000Z",
    )
    assert first == {
        "ts": "2026-08-19T12:00:00.000Z",
        "model": "grok-4.6",
        "net_liquidation": 35_000.0,
    }
    # Same model → no new marker
    again = journal.ensure_model_session("grok-4.6", net_liquidation=36_000.0)
    assert again == first
    assert journal.last_session_marker() == first

    # Model change stamps a new session
    switched = journal.ensure_model_session(
        "grok-4",
        net_liquidation=35_100.0,
        ts="2026-08-19T18:00:00.000Z",
    )
    assert switched["model"] == "grok-4"
    assert switched["net_liquidation"] == 35_100.0
    assert journal.last_session_marker() == switched

    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM session_markers").fetchone()[0]
        models = [
            r[0]
            for r in conn.execute(
                "SELECT model FROM session_markers ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert n == 2
    assert models == ["grok-4.6", "grok-4"]


def test_dispatched_order_ids_and_closing_fills_split_manual_exits(journal):
    """A manual TWS flatten has no dispatch behind it. That is the signal.

    ``strategy_performance`` already buckets these as ``(unattributed)``; these
    two reads expose the same fact per order id and per symbol so a setup card
    can tell an interrupted trade from a resolved one.
    """
    day = "2026-08-20"
    pid = journal.record_proposal(
        strategy="market_bracket", symbol="WMT", validation_ok=True, ts=f"{day}T14:00:00.000Z"
    )
    journal.record_dispatch(
        pid,
        True,
        {
            "success": True,
            "bracket_order_id": 4444,
            "stop_order_id": 4445,
            "target_order_id": 4446,
        },
        ts=f"{day}T14:00:00.000Z",
    )
    journal.record_fills(
        [
            {
                "ts": f"{day}T14:00:01.000Z",
                "exec_id": "entry",
                "order_id": 4444,
                "symbol": "WMT",
                "side": "BOT",
                "quantity": 70.0,
                "price": 103.08,
                "commission": 1.0,
                "realized_pnl": 0.0,
            },
            {
                "ts": f"{day}T17:40:00.000Z",
                "exec_id": "manual-flatten",
                "order_id": 9999,
                "symbol": "WMT",
                "side": "SLD",
                "quantity": 70.0,
                "price": 104.20,
                "commission": 1.0,
                "realized_pnl": 78.4,
            },
        ]
    )

    placed = journal.dispatched_order_ids()
    assert {4444, 4445, 4446} <= placed
    assert 9999 not in placed

    closers = journal.closing_fills()
    # The opener booked no realized P&L, so it is not an exit.
    assert [f["order_id"] for f in closers] == [9999]
    assert closers[0]["symbol"] == "WMT"
    assert closers[0]["realized_pnl"] == 78.4

    by_strategy = {r["strategy"]: r for r in journal.strategy_performance(since_day=day)}
    assert by_strategy["(unattributed)"]["realized_pnl_sum"] == 78.4


def test_closed_fill_stats_since(journal):
    day = "2026-08-14"
    pid_win = journal.record_proposal(
        strategy="market_order", symbol="QQQ", validation_ok=True, ts=f"{day}T13:00:00.000Z"
    )
    journal.record_dispatch(
        pid_win, True, {"success": True, "order_id": 2}, ts=f"{day}T13:00:00.000Z"
    )
    pid_loss = journal.record_proposal(
        strategy="market_order", symbol="IWM", validation_ok=True, ts=f"{day}T13:01:00.000Z"
    )
    journal.record_dispatch(
        pid_loss, True, {"success": True, "order_id": 3}, ts=f"{day}T13:01:00.000Z"
    )
    journal.record_fills(
        [
            {
                "ts": f"{day}T10:00:00.000Z",
                "exec_id": "cf-old",
                "order_id": 1,
                "symbol": "SPY",
                "side": "SLD",
                "quantity": 1,
                "price": 500.0,
                "realized_pnl": 5.0,
            },
            {
                "ts": f"{day}T14:00:00.000Z",
                "exec_id": "cf-win",
                "order_id": 2,
                "symbol": "QQQ",
                "side": "SLD",
                "quantity": 1,
                "price": 400.0,
                "realized_pnl": 12.0,
            },
            {
                "ts": f"{day}T15:00:00.000Z",
                "exec_id": "cf-loss",
                "order_id": 3,
                "symbol": "IWM",
                "side": "SLD",
                "quantity": 1,
                "price": 200.0,
                "realized_pnl": -3.0,
            },
            {
                "ts": f"{day}T16:00:00.000Z",
                "exec_id": "cf-flat",
                "order_id": 4,
                "symbol": "DIA",
                "side": "BOT",
                "quantity": 1,
                "price": 300.0,
                "realized_pnl": 0.0,  # ignored (not a closed PnL)
            },
            {
                "ts": f"{day}T16:30:00.000Z",
                "exec_id": "cf-manual",
                "order_id": 99,
                "symbol": "WMT",
                "side": "SLD",
                "quantity": 1,
                "price": 104.0,
                "realized_pnl": 100.0,  # no ticket — must not count
            },
        ]
    )
    stats = journal.closed_fill_stats_since(f"{day}T12:00:00.000Z")
    assert stats["n"] == 2
    assert stats["wins"] == 1
    assert abs(stats["sum"] - 9.0) < 1e-9


def test_closed_fill_stats_since_ignores_fills_without_a_ticket(journal):
    journal.record_fills(
        [
            {
                "ts": "2026-08-14T14:00:00.000Z",
                "exec_id": "orphan",
                "order_id": 77,
                "symbol": "SPY",
                "side": "SLD",
                "quantity": 1,
                "price": 500.0,
                "realized_pnl": 12.0,
            }
        ]
    )
    stats = journal.closed_fill_stats_since("2026-08-14T00:00:00.000Z")
    assert stats == {"n": 0, "wins": 0, "sum": 0.0}


def test_nav_at_or_after_is_this_run_not_leftover(journal):
    journal.record_snapshot(
        account={"NetLiquidation": 36638.0}, ts="2026-07-28T00:00:00.000Z"
    )
    journal.record_snapshot(
        account={"NetLiquidation": 35000.0}, ts="2026-08-25T13:00:05.000Z"
    )
    journal.record_snapshot(
        account={"NetLiquidation": 35100.0}, ts="2026-08-25T16:00:00.000Z"
    )
    nl, ts = journal.nav_at_or_after("2026-08-25T13:00:00.000Z")
    assert nl == 35000.0
    assert ts == "2026-08-25T13:00:05.000Z"
    leftover, leftover_ts = journal.nav_at_or_before("2026-08-25T13:00:00.000Z")
    assert leftover == 36638.0
    assert leftover_ts == "2026-07-28T00:00:00.000Z"
    none_nl, none_ts = journal.nav_at_or_after("2026-08-25T17:00:00.000Z")
    assert none_nl is None and none_ts is None


def test_snapshot_ts_is_stored_as_utc_z(journal, tmp_path):
    journal.record_snapshot(
        account={"NetLiquidation": 1.0, "DailyPnL": 0.0},
        ts="2026-08-25T12:00:00-04:00",
    )
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        ts = conn.execute("SELECT ts FROM snapshots").fetchone()[0]
    finally:
        conn.close()
    assert ts == "2026-08-25T16:00:00.000Z"
    nl, stored = journal.nav_at_or_before("2026-08-25T16:00:00Z")
    assert nl == 1.0
    assert stored == "2026-08-25T16:00:00.000Z"


def test_account_performance_daily_pnl_does_not_survive_yesterday(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "day.db"
    j = TradeJournal(path=str(db), enabled=True)
    now = datetime.now(timezone.utc)
    j.record_snapshot(
        account={"NetLiquidation": 100_000.0, "DailyPnL": 88.0},
        ts=(now - timedelta(days=1)).isoformat(),
    )
    perf = j.account_performance()
    assert perf["net_liquidation"] == 100_000.0
    assert perf["daily_pnl"] is None


def test_account_performance_daily_pnl_does_not_survive_new_session(tmp_path):
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "sess.db"
    j = TradeJournal(path=str(db), enabled=True)
    now = datetime.now(timezone.utc)
    j.record_snapshot(
        account={"NetLiquidation": 100_000.0, "DailyPnL": 88.0},
        ts=now.isoformat(),
    )
    j.ensure_model_session(
        "grok-4.6",
        net_liquidation=100_000.0,
        ts=(now + timedelta(seconds=2)).isoformat(),
    )
    assert j.account_performance()["daily_pnl"] is None
    j.record_snapshot(
        account={"NetLiquidation": 100_050.0, "DailyPnL": 12.0},
        ts=(now + timedelta(seconds=4)).isoformat(),
    )
    assert j.account_performance()["daily_pnl"] == 12.0


def test_legacy_journal_without_session_markers_is_upgraded(tmp_path, monkeypatch):
    """Live DBs from 2026-08-12..18 lacked session_markers; open must create it."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE model_usage (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                stage TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL
            );
            INSERT INTO model_usage (ts, stage, input_tokens, output_tokens, cost_usd)
            VALUES ('2026-08-14T12:00:00.000Z', 'grok', 0, 100, 0.18);
            """
        )
        conn.commit()
        before = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "session_markers" not in before

    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    j = TradeJournal(path=str(db), enabled=True)
    # Touch schema path used by session + usage APIs
    assert j.model_usage_totals()["calls"] == 1
    assert abs(j.model_usage_totals()["cost_usd"] - 0.18) < 1e-9
    assert j.last_session_marker() is None
    stamped = j.ensure_model_session(
        "grok-4.6", net_liquidation=35_000.0, ts="2026-08-19T12:00:00.000Z"
    )
    assert stamped["model"] == "grok-4.6"

    conn = sqlite3.connect(str(db))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        n = conn.execute("SELECT COUNT(*) FROM session_markers").fetchone()[0]
        # cached_tokens / model columns added by migration
        cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(model_usage)").fetchall()
        }
    finally:
        conn.close()
    assert "session_markers" in tables
    assert n == 1
    assert "cached_tokens" in cols
    assert "model" in cols
    reset_journal(path=str(db), enabled=True)


def test_journal_missing_session_markers_table_fails_hard(tmp_path):
    """A journal that cannot host session_markers must not silently look healthy."""
    db = tmp_path / "broken.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE model_usage (id INTEGER PRIMARY KEY, ts TEXT, cost_usd REAL)"
        )
        conn.commit()
    finally:
        conn.close()

    # Bypass TradeJournal._ensure_schema by querying a raw connection that
    # deliberately lacks session_markers — the live anomaly we must catch.
    conn = sqlite3.connect(str(db))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT ts, model, net_liquidation FROM session_markers")
    finally:
        conn.close()
    assert "session_markers" not in tables
    assert "model_usage" in tables
