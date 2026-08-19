"""Protection report: positions matched to stop/target orders; snapshot fill ingest."""

from __future__ import annotations

import sqlite3

import pytest

from abcxauto.memory import reset_journal
from abcxauto.monitor import PortfolioMonitor, build_protection_report


def _pos(symbol, qty, sec_type="STK", **extra):
    return {"symbol": symbol, "quantity": qty, "sec_type": sec_type, **extra}


def _order(symbol, action, order_type, order_id=1, **extra):
    return {
        "order_id": order_id, "symbol": symbol, "sec_type": "STK",
        "action": action, "quantity": 10, "order_type": order_type, **extra,
    }


def test_protected_long_position():
    report = build_protection_report(
        [_pos("AAPL", 10, unrealized_pnl=50.0)],
        [
            _order("AAPL", "SELL", "STP", order_id=1, aux_price=140.0),
            _order("AAPL", "SELL", "LMT", order_id=2, lmt_price=170.0),
        ],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert entry["stop_orders"][0]["order_id"] == 1
    assert entry["target_orders"][0]["order_id"] == 2
    assert report["unprotected_symbols"] == []


def test_unprotected_position_flagged():
    report = build_protection_report([_pos("NVDA", 5)], [])
    entry = report["positions"][0]
    assert entry["protected"] is False
    assert "stop_loss" in entry["missing"]
    assert "take_profit" in entry["missing"]
    assert report["unprotected_symbols"] == ["NVDA"]


def test_stop_without_target_still_protected_but_missing_target():
    report = build_protection_report(
        [_pos("NVDA", 5)],
        [_order("NVDA", "SELL", "TRAIL", order_id=3)],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert entry["missing"] == ["take_profit"]
    assert report["unprotected_symbols"] == []


def test_short_position_uses_buy_side_orders():
    report = build_protection_report(
        [_pos("TSLA", -10)],
        [
            _order("TSLA", "BUY", "STP", order_id=4, aux_price=260.0),
            _order("TSLA", "SELL", "STP", order_id=5, aux_price=200.0),  # wrong side
        ],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert [o["order_id"] for o in entry["stop_orders"]] == [4]


def test_option_lots_are_not_unprotected():
    report = build_protection_report(
        [_pos("SPY", 1, sec_type="OPT", market_value=250.0, expiration="20260731", strike=500.0, right="C", conId=9)],
        [],
    )
    entry = report["positions"][0]
    assert "protected" not in entry
    assert entry["covering_exits"] == 0
    assert entry["market_value"] == 250.0
    assert "days_to_expiry" in entry
    assert report["unprotected_symbols"] == []
    assert "short option" not in entry.get("note", "")


def test_short_option_note_is_not_unprotected():
    report = build_protection_report(
        [_pos("SPY", -2, sec_type="OPT", market_value=-400.0, expiration="20260731", strike=500.0, right="P", conId=8)],
        [],
    )
    entry = report["positions"][0]
    assert "short option" in entry["note"]
    assert entry.get("flag") == "short option — review risk"
    assert report["unprotected_symbols"] == []


def test_option_stop_and_target_protect():
    pos = _pos(
        "QQQ", 1, sec_type="OPT", conId=740683086,
        expiration="20260918", strike=745.0, right="C",
    )
    report = build_protection_report(
        [pos],
        [
            _order("QQQ", "SELL", "STP", order_id=1, sec_type="OPT", quantity=1, conId=740683086, aux_price=0.6),
            _order("QQQ", "SELL", "LMT", order_id=2, sec_type="OPT", quantity=1, conId=740683086, lmt_price=4.0),
        ],
    )
    entry = report["positions"][0]
    assert entry["covering_exits"] == 2
    assert "protected" not in entry
    assert report["unprotected_symbols"] == []


def test_one_option_lmt_is_a_fact_not_unprotected():
    pos = _pos(
        "QQQ", 1, sec_type="OPT", conId=740683086,
        expiration="20260918", strike=745.0, right="C",
    )
    report = build_protection_report(
        [pos],
        [_order("QQQ", "SELL", "LMT", order_id=2, sec_type="OPT", quantity=1, conId=740683086, lmt_price=0.64)],
    )
    assert report["positions"][0]["covering_exits"] == 1
    assert report["unprotected_symbols"] == []


def test_bag_combo_covers_both_vertical_legs():
    long_leg = _pos(
        "JPM", 1, sec_type="OPT", conId=787026479,
        expiration="20260918", strike=370.0, right="C",
    )
    short_leg = _pos(
        "JPM", -1, sec_type="OPT", conId=846417188,
        expiration="20260918", strike=375.0, right="C",
    )
    cut = {
        "order_id": 10, "symbol": "JPM", "sec_type": "BAG", "action": "SELL",
        "quantity": 1, "order_type": "LMT", "lmt_price": 0.71,
        "combo_legs": [
            {"conId": 787026479, "action": "SELL", "ratio": 1},
            {"conId": 846417188, "action": "BUY", "ratio": 1},
        ],
    }
    tgt = {
        "order_id": 11, "symbol": "JPM", "sec_type": "BAG", "action": "SELL",
        "quantity": 1, "order_type": "LMT", "lmt_price": 4.0,
        "combo_legs": [
            {"conId": 787026479, "action": "SELL", "ratio": 1},
            {"conId": 846417188, "action": "BUY", "ratio": 1},
        ],
    }
    report = build_protection_report([long_leg, short_leg], [cut, tgt])
    assert report["unprotected_symbols"] == []
    assert all(e["covering_exits"] == 2 for e in report["positions"])


def test_wrong_strike_option_stop_does_not_protect():
    pos = _pos(
        "QQQ", 1, sec_type="OPT", conId=1,
        expiration="20260918", strike=745.0, right="C",
    )
    report = build_protection_report(
        [pos],
        [
            _order("QQQ", "SELL", "STP", order_id=1, sec_type="OPT", quantity=1, conId=99, aux_price=0.6),
            _order("QQQ", "SELL", "LMT", order_id=2, sec_type="OPT", quantity=1, conId=99, lmt_price=4.0),
        ],
    )
    assert report["positions"][0]["covering_exits"] == 0
    assert report["unprotected_symbols"] == []


def test_wrong_conid_stop_does_not_count_as_protection():
    report = build_protection_report(
        [_pos("AAPL", 10, conId=111)],
        [_order("AAPL", "SELL", "STP", order_id=1, conId=222, aux_price=140.0)],
    )
    entry = report["positions"][0]
    assert entry["protected"] is False
    assert entry["stop_orders"] == []
    assert "stop_loss" in entry["missing"]
    assert report["unprotected_symbols"] == ["AAPL"]


def test_matching_conid_stop_protects():
    report = build_protection_report(
        [_pos("AAPL", 10, con_id=111)],
        [
            _order("AAPL", "SELL", "STP", order_id=1, conId=111, aux_price=140.0),
            _order("AAPL", "SELL", "LMT", order_id=2, conId=111, lmt_price=170.0),
        ],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert entry["stop_orders"][0]["order_id"] == 1
    assert report["unprotected_symbols"] == []


def test_symbol_fallback_when_conids_absent():
    report = build_protection_report(
        [_pos("MSFT", 5)],
        [_order("MSFT", "SELL", "STP", order_id=9, aux_price=300.0)],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert entry["stop_orders"][0]["order_id"] == 9
    assert report["unprotected_symbols"] == []


@pytest.mark.asyncio
async def test_take_snapshot_records_fills(tmp_path, monkeypatch):
    db = tmp_path / "monitor_fills.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    reset_journal(path=str(db), enabled=True)

    fills = [
        {
            "ts": "2026-07-09T12:00:00.000Z",
            "exec_id": "mon-exec-1",
            "order_id": 42,
            "symbol": "AAPL",
            "sec_type": "STK",
            "side": "BOT",
            "quantity": 10.0,
            "price": 150.0,
            "commission": 1.0,
            "realized_pnl": 0.0,
        }
    ]

    class Session:
        def emit(self, *_a, **_k):
            pass

    class Connector:
        connected = True

        async def get_positions(self):
            return []

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 100_000.0, "dailypnl": 0.0}

        async def get_fills(self):
            return fills

    mon = PortfolioMonitor(Session(), Connector())
    snap = await mon.take_snapshot()
    assert snap["connected"] is True

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT exec_id, order_id, symbol FROM fills"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("mon-exec-1", 42, "AAPL")]

    # Second poll is idempotent on exec_id.
    await mon.take_snapshot()
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


@pytest.mark.asyncio
async def test_take_snapshot_without_get_fills_still_works(tmp_path, monkeypatch):
    db = tmp_path / "monitor_nofills.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    reset_journal(path=str(db), enabled=True)

    class Session:
        def emit(self, *_a, **_k):
            pass

    class Connector:
        connected = True

        async def get_positions(self):
            return []

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 50_000.0}

    mon = PortfolioMonitor(Session(), Connector())
    snap = await mon.take_snapshot()
    assert snap["connected"] is True
    assert "protection" in snap


@pytest.mark.asyncio
async def test_stub_session_skips_grok_review_keeps_panic(monkeypatch):
    """supports_agent_review=False skips _ask_grok_review; auto-panic still runs."""
    from abcxauto.risk_gates import get_risk_gate, reset_risk_gate

    class Cfg:
        monitor_poll_s = 60
        monitor_review_s = 1
        monitor_extended_hours = False
        auto_panic_on_breach = True
        daily_loss_limit_pct = 2.0

    monkeypatch.setattr("abcxauto.monitor.get_config", lambda: Cfg())
    reset_risk_gate()

    injects: list[str] = []
    reviews: list[bool] = []

    class StubSession:
        supports_agent_review = False

        def emit(self, *_a, **_k):
            pass

        async def inject(self, text, *, source="monitor"):
            injects.append(text)

    class Conn:
        connected = True
        flattened = False

        async def get_positions(self):
            return [{"symbol": "SPY", "quantity": 1, "sec_type": "STK"}]

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 100_000.0, "dailypnl": -3000.0}

        async def flatten_all(self):
            self.flattened = True
            return {"success": True}

    mon = PortfolioMonitor(StubSession(), Conn())
    assert mon._supports_agent_review() is False

    async def boom(*_a, **_k):
        reviews.append(True)
        raise AssertionError("_ask_grok_review must not run for stub sessions")

    monkeypatch.setattr(mon, "_ask_grok_review", boom)

    snap = {
        "account": {"netliquidation": 100_000.0, "dailypnl": -3000.0},
        "protection": {
            "positions": [{"symbol": "SPY", "quantity": 1}],
            "unprotected_symbols": ["SPY"],
        },
    }
    # Force review branch timing
    mon._last_review_ts = 0.0
    mon._last_unprotected_nudge_ts = 0.0
    await mon._tick()
    assert reviews == []
    assert get_risk_gate().is_halted
    assert any("AUTO-PANIC" in t or "auto-panic" in t.lower() for t in injects) or injects
    reset_risk_gate()
