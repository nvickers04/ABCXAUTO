"""Journal score honesty: fill bid/ask + session-start NetLiq.

book_return_pct stays (end_NL - start_NL) / start_NL. Model cost is a
separate USD number. No second scorecard.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from abcxauto.memory import get_journal
from abcxauto.scorecard import compute_scorecard
from abcxauto.send_marks import (
    QUOTE_REASON_IBKR_LIVE,
    QUOTE_REASON_INCOMPLETE,
    QUOTE_REASON_NO_QUOTE,
    attach_fill_quotes,
    stamp_ibkr_quote,
)


def _fill(**kw):
    row = {
        "exec_id": "e1",
        "order_id": 50,
        "symbol": "PYPL",
        "sec_type": "STK",
        "side": "SLD",
        "quantity": 50,
        "price": 52.60,
        "commission": 1.0,
        "realized_pnl": -12.0,
        "ts": "2026-08-31T15:00:00.000Z",
    }
    row.update(kw)
    return row


def test_record_fills_stores_bid_ask_and_commission(tmp_path):
    j = get_journal()
    assert (
        j.record_fills(
            [
                _fill(
                    bid=52.55,
                    ask=52.65,
                    quote_reason=QUOTE_REASON_IBKR_LIVE,
                )
            ]
        )
        == 1
    )
    with sqlite3.connect(j.path) as conn:
        row = conn.execute(
            "SELECT bid, ask, commission, quote_reason FROM fills WHERE exec_id = 'e1'"
        ).fetchone()
    assert row[0] == 52.55
    assert row[1] == 52.65
    assert row[2] == 1.0
    assert row[3] == QUOTE_REASON_IBKR_LIVE


def test_record_fills_missing_quote_stays_null_with_reason(tmp_path):
    """PYPL stop SLD: realized PnL without inventing a mid."""
    j = get_journal()
    assert j.record_fills([_fill()]) == 1
    with sqlite3.connect(j.path) as conn:
        row = conn.execute(
            "SELECT bid, ask, commission, realized_pnl, quote_reason "
            "FROM fills WHERE exec_id = 'e1'"
        ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] == 1.0
    assert row[3] == -12.0
    assert row[4] == QUOTE_REASON_NO_QUOTE


def test_stamp_ibkr_quote_does_not_invent_mid_as_bid_ask():
    stamped = stamp_ibkr_quote(
        _fill(),
        {"last": 52.60, "mid": 52.60},
    )
    assert stamped.get("bid") is None
    assert stamped.get("ask") is None
    assert stamped["quote_reason"] in (QUOTE_REASON_NO_QUOTE, "no_ibkr_quote")


def test_stamp_ibkr_quote_incomplete_nbbo_keeps_null_side():
    stamped = stamp_ibkr_quote(_fill(), {"bid": 52.55, "last": 52.60})
    assert stamped["bid"] == 52.55
    assert stamped.get("ask") is None
    assert stamped["quote_reason"] == QUOTE_REASON_INCOMPLETE


@pytest.mark.asyncio
async def test_attach_fill_quotes_uses_ibkr_live_and_fails_closed():
    class Conn:
        async def get_live_quote(self, symbol, *, fresh=False):
            if symbol == "PYPL":
                return {"bid": 52.55, "ask": 52.65, "last": 52.60, "source": "ibkr"}
            return {"error": "no IBKR tick yet", "source": "ibkr"}

    live, missing = await attach_fill_quotes(
        [_fill(exec_id="live"), _fill(exec_id="miss", symbol="ZZZZ")],
        Conn(),
    )
    assert live["bid"] == 52.55
    assert live["ask"] == 52.65
    assert live["quote_reason"] == QUOTE_REASON_IBKR_LIVE
    assert missing.get("bid") is None
    assert missing.get("ask") is None
    assert missing["quote_reason"] == "no IBKR tick yet"


def test_ensure_session_start_nl_writes_once_per_et_day():
    j = get_journal()
    first = j.ensure_session_start_nl(35_000.0, ts="2026-08-31T13:05:00.000Z")
    assert first is not None
    assert first["net_liquidation"] == 35_000.0
    assert first["session_date"] == "2026-08-31"
    again = j.ensure_session_start_nl(36_000.0, ts="2026-08-31T18:00:00.000Z")
    assert again["net_liquidation"] == 35_000.0
    nl, ts = j.first_nl_on_et_day("2026-08-31")
    assert nl == 35_000.0
    assert str(ts).startswith("2026-08-31")
    with sqlite3.connect(j.path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE net_liquidation IS NOT NULL"
        ).fetchone()[0]
    assert n == 1


def test_book_return_pct_null_when_start_nl_missing():
    j = get_journal()
    now = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)
    sc = compute_scorecard(equity=35_100.0, journal=j, now=now)
    assert sc["book_return_pct"] is None
    assert sc["startup_cash"] is None
    sess = sc.get("session") or {}
    assert sess.get("startup_nl") is None
    assert sess.get("book_return_pct") is None
    assert sess.get("end_nl") == 35_100.0


def test_book_return_pct_computable_when_start_nl_and_fills_present():
    j = get_journal()
    j.ensure_session_start_nl(35_000.0, ts="2026-08-31T13:35:00.000Z")
    assert (
        j.record_fills(
            [
                _fill(bid=52.55, ask=52.65),
                _fill(
                    exec_id="e2",
                    order_id=51,
                    side="BOT",
                    price=52.40,
                    bid=52.35,
                    ask=52.45,
                    realized_pnl=0.0,
                    ts="2026-08-31T14:00:00.000Z",
                ),
            ]
        )
        == 2
    )
    j.record_snapshot(
        account={"NetLiquidation": 35_100.0, "DailyPnL": 100.0},
        ts="2026-08-31T20:00:00.000Z",
    )
    now = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)
    sc = compute_scorecard(equity=35_100.0, journal=j, now=now)
    assert sc["startup_cash"] == 35_000.0
    assert sc["book_pnl"] == 100.0
    assert abs(sc["book_return_pct"] - (100.0 / 35_000.0 * 100.0)) < 1e-9
    sess = sc["session"]
    assert sess["startup_nl"] == 35_000.0
    assert sess["end_nl"] == 35_100.0
    assert abs(sess["book_return_pct"] - (100.0 / 35_000.0 * 100.0)) < 1e-9
    listed = j.listed_fills()
    assert listed[0]["bid"] == 52.55
    assert listed[0]["ask"] == 52.65
    assert listed[0]["commission"] == 1.0


@pytest.mark.asyncio
async def test_get_fills_attaches_ibkr_quote_or_reason():
    from abcxauto.broker.connector import IBKRConnector

    class Conn:
        get_fills = IBKRConnector.get_fills

        def __init__(self):
            self.ib = SimpleNamespace(
                fills=lambda: [
                    SimpleNamespace(
                        execution=SimpleNamespace(
                            time=datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
                            execId="pypl-stop",
                            orderId=88,
                            side="SLD",
                            shares=50.0,
                            price=52.60,
                        ),
                        contract=SimpleNamespace(
                            symbol="PYPL", secType="STK", conId=1
                        ),
                        commissionReport=SimpleNamespace(
                            commission=1.0, realizedPNL=-12.0
                        ),
                    )
                ]
            )

        async def _ensure_connected(self):
            return True

        async def get_live_quote(self, symbol, *, fresh=False):
            return {"bid": 52.55, "ask": 52.65, "last": 52.60, "source": "ibkr"}

    rows = await Conn().get_fills()
    assert rows[0]["bid"] == 52.55
    assert rows[0]["ask"] == 52.65
    assert rows[0]["commission"] == 1.0
    assert rows[0]["quote_reason"] == QUOTE_REASON_IBKR_LIVE
