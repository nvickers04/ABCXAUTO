"""Send journal: IBKR last/bid/ask vs sent/fill. Paper mid-inside-spread is labeled.

The live journal used to store oid/card= on the proposal and order_id on the
dispatch, with fill price later — not NBBO vs fill. Graduation / conservative_pnl
are not this PR. live_marks_match_paper() is not an exploit-band key; paper 7497
still never qualifies as live marks.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.memory import get_journal
from abcxauto.mode_size import live_marks_match_paper
from abcxauto.send_marks import (
    FILL_LABEL_AT_ASK,
    FILL_LABEL_MID_INSIDE,
    FILL_LABEL_MISSED,
    FILL_LABEL_WORKING,
    SEND_MARK_FIELDS,
    build_dispatch_marks,
    compute_marks,
    fill_label_of,
    public_marks,
)

SYSTEM_PROMPT_LOCK = (
    "You own an Interactive Brokers {mode} book. Strategy is yours.\n"
    "Live only follows a promoted playbook. Risk is code.\n"
    "send tickets that match ORDER EXAMPLES.\n"
    "Size vs max_risk_per_trade_pct of NetLiq.\n"
)


def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_live_marks_match_paper_still_refuses_7497():
    src = inspect.getsource(live_marks_match_paper)
    assert "Paper 7497 never qualifies" in src
    assert live_marks_match_paper() is False


def test_mid_inside_spread_paper_fill_is_labeled():
    """BUY filled at mid while bid < mid < ask is the paper gift, not a card."""
    marks = compute_marks(
        {"last": 10.10, "bid": 10.00, "ask": 10.20},
        sent_price=10.10,
        fill_price=10.10,
        side="BUY",
    )
    assert marks["fill_label"] == FILL_LABEL_MID_INSIDE
    assert marks["ibkr_last"] == 10.10
    assert marks["bid"] == 10.00
    assert marks["ask"] == 10.20
    assert marks["sent_price"] == 10.10
    assert marks["fill_price"] == 10.10
    assert marks["signed_slippage"] == 0.0
    assert abs(marks["spread_paid"] - 0.10) < 1e-9
    assert fill_label_of(bid=10.00, ask=10.20, fill_price=10.10) == FILL_LABEL_MID_INSIDE


def test_buy_at_ask_is_not_the_paper_gift():
    marks = compute_marks(
        {"last": 10.10, "bid": 10.00, "ask": 10.20},
        sent_price=10.10,
        fill_price=10.20,
        side="BUY",
    )
    assert marks["fill_label"] == FILL_LABEL_AT_ASK
    assert abs(marks["signed_slippage"] - 0.10) < 1e-9
    assert abs(marks["spread_paid"] - 0.20) < 1e-9


def test_sell_signed_slippage_is_adverse_when_worse_than_sent():
    marks = compute_marks(
        {"last": 10.10, "bid": 10.00, "ask": 10.20},
        sent_price=10.10,
        fill_price=10.00,
        side="SELL",
    )
    assert abs(marks["signed_slippage"] - 0.10) < 1e-9
    assert abs(marks["spread_paid"] - 0.20) < 1e-9
    assert marks["fill_label"] == "at_bid"


def _working_marks(**over):
    row = build_dispatch_marks(
        strategy="limit_order",
        params={"symbol": "NVDA", "action": "BUY", "limit_price": 10.10, "card": "flush bounce"},
        quote={"last": 10.10, "bid": 10.00, "ask": 10.20, "mid": 10.10},
        result={"success": True, "order_id": 88, "status": "Submitted"},
        ok=True,
    )
    row.update(over)
    return row


def test_dispatch_row_has_nbbo_and_sent_before_fill():
    journal = get_journal()
    pid = journal.record_proposal(
        strategy="limit_order", symbol="NVDA", direction="BUY", quantity=1,
        params={"card": "flush bounce", "limit_price": 10.10},
        validation_ok=True,
    )
    marks = _working_marks()
    assert marks["fill_label"] == FILL_LABEL_WORKING
    assert marks["fill_price"] is None
    did = journal.record_dispatch(pid, True, {"success": True, "order_id": 88})
    journal.record_send_marks(
        proposal_id=pid, dispatch_id=did, marks=marks,
        result={"success": True, "order_id": 88},
    )
    rows = journal.recent_send_marks()
    assert len(rows) == 1
    row = rows[0]
    for key in SEND_MARK_FIELDS:
        assert key in row
    assert row["ibkr_last"] == 10.10
    assert row["bid"] == 10.00
    assert row["ask"] == 10.20
    assert row["sent_price"] == 10.10
    assert row["fill_price"] is None
    assert row["fill_label"] == FILL_LABEL_WORKING
    recent = journal.recent_dispatches(limit=1)[0]
    for key in SEND_MARK_FIELDS:
        assert key in recent
    assert recent["result"]["order_id"] == 88
    assert recent["result"]["send_marks"]["bid"] == 10.00


def test_mid_inside_spread_fill_lands_on_send_and_fill_rows(tmp_path):
    journal = get_journal()
    pid = journal.record_proposal(
        strategy="market_bracket", symbol="NVDA", direction="LONG", quantity=10,
        params={"card": "flush bounce"},
        validation_ok=True,
    )
    marks = build_dispatch_marks(
        strategy="market_bracket",
        params={"symbol": "NVDA", "direction": "LONG", "card": "flush bounce"},
        quote={"last": 10.10, "bid": 10.00, "ask": 10.20},
        result={"success": True, "order_id": 501, "status": "Submitted"},
        ok=True,
    )
    did = journal.record_dispatch(pid, True, {"success": True, "order_id": 501})
    journal.record_send_marks(
        proposal_id=pid, dispatch_id=did, marks=marks,
        result={"success": True, "order_id": 501},
    )
    assert journal.record_fills(
        [
            {
                "ts": "2026-08-28T14:00:01.000Z",
                "exec_id": "paper-mid",
                "order_id": 501,
                "symbol": "NVDA",
                "sec_type": "STK",
                "side": "BOT",
                "quantity": 10,
                "price": 10.10,
                "commission": 1.0,
                "realized_pnl": 0.0,
            }
        ]
    ) == 1
    send = journal.recent_send_marks()[0]
    assert send["fill_price"] == 10.10
    assert send["fill_label"] == FILL_LABEL_MID_INSIDE
    assert send["status"] == "filled"
    assert abs(send["spread_paid"] - 0.10) < 1e-9
    assert send["signed_slippage"] == 0.0
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    try:
        fill = conn.execute(
            "SELECT ibkr_last, bid, ask, sent_price, price, signed_slippage, "
            "spread_paid, fill_label FROM fills WHERE exec_id = 'paper-mid'"
        ).fetchone()
    finally:
        conn.close()
    assert fill[0] == 10.10
    assert fill[1] == 10.00
    assert fill[2] == 10.20
    assert fill[3] == 10.10
    assert fill[4] == 10.10
    assert fill[5] == 0.0
    assert abs(fill[6] - 0.10) < 1e-9
    assert fill[7] == FILL_LABEL_MID_INSIDE
    dispatch = journal.recent_dispatches(limit=1)[0]
    assert dispatch["fill_label"] == FILL_LABEL_MID_INSIDE
    assert dispatch["result"]["send_marks"]["fill_label"] == FILL_LABEL_MID_INSIDE


def test_missed_working_order_keeps_nbbo_once_resolved():
    journal = get_journal()
    pid = journal.record_proposal(
        strategy="limit_order", symbol="NVDA", direction="BUY", quantity=1,
        params={"limit_price": 10.10, "card": "flush bounce"},
        validation_ok=True,
    )
    marks = _working_marks()
    did = journal.record_dispatch(pid, True, {"success": True, "order_id": 88})
    journal.record_send_marks(
        proposal_id=pid, dispatch_id=did, marks=marks,
        result={"success": True, "order_id": 88},
    )
    assert journal.resolve_unfilled_sends([{"order_id": 88}], grace_s=0) == 0
    assert journal.recent_send_marks()[0]["status"] == "working"
    assert journal.recent_send_marks()[0]["seen_working"] == 1
    assert journal.resolve_unfilled_sends([], grace_s=0) == 1
    row = journal.recent_send_marks()[0]
    for key in SEND_MARK_FIELDS:
        assert key in row
    assert row["ibkr_last"] == 10.10
    assert row["bid"] == 10.00
    assert row["ask"] == 10.20
    assert row["sent_price"] == 10.10
    assert row["fill_price"] is None
    assert row["signed_slippage"] is None
    assert row["spread_paid"] is None
    assert row["fill_label"] == FILL_LABEL_MISSED
    assert row["status"] == "missed"
    dispatch = journal.recent_dispatches(limit=1)[0]
    assert dispatch["fill_label"] == FILL_LABEL_MISSED
    assert dispatch["result"]["send_marks"]["fill_label"] == FILL_LABEL_MISSED


def test_public_marks_always_include_the_fields():
    blob = public_marks({"bid": 1.0})
    assert set(blob) == set(SEND_MARK_FIELDS)
    assert blob["bid"] == 1.0
    assert blob["fill_price"] is None


@pytest.mark.asyncio
async def test_execute_proposal_journals_nbbo_vs_paper_mid_fill(monkeypatch):
    from abcxauto.config import Config, get_config
    from abcxauto.executor import execute_proposal
    from abcxauto.proposals import validate_proposal

    base = get_config()
    monkeypatch.setattr(
        "abcxauto.executor.get_config",
        lambda: Config(
            **{
                **base.__dict__,
                "risk_gates_enabled": False,
                "max_arena_concentration_pct": 0,
                "defined_risk_only": False,
            }
        ),
    )
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(
            **{**base.__dict__, "defined_risk_only": False, "risk_posture": "balanced"}
        ),
    )

    class Gateway:
        connected = True

        async def get_positions(self):
            return [{"symbol": "NVDA", "quantity": 0, "sec_type": "STK"}]

        async def get_open_orders(self):
            return []

        async def get_live_quote(self, symbol, fresh=False):
            return {
                "symbol": symbol,
                "last": 10.10,
                "bid": 10.00,
                "ask": 10.20,
                "mid": 10.10,
                "source": "ibkr",
            }

        async def place_market_bracket(self, **kwargs):
            return {
                "success": True,
                "order_id": 501,
                "filled": True,
                "avg_fill_price": 10.10,
                "entry_price": 10.10,
            }

    proposal = validate_proposal(
        "market_bracket",
        {
            "symbol": "NVDA",
            "quantity": 10,
            "direction": "LONG",
            "stop_price": 9.70,
            "target_price": 10.60,
            "price_hint": 10.10,
            "card": "flush bounce",
        },
        "paper mid fill",
    )
    result = await execute_proposal(proposal, Gateway())
    assert result["success"] is True
    assert "send_marks" not in result
    row = get_journal().recent_send_marks()[0]
    assert row["ibkr_last"] == 10.10
    assert row["bid"] == 10.00
    assert row["ask"] == 10.20
    assert row["sent_price"] == 10.10
    assert row["fill_price"] == 10.10
    assert row["fill_label"] == FILL_LABEL_MID_INSIDE
    assert row["card"] == "flush bounce"
    dispatch = get_journal().recent_dispatches(limit=1)[0]
    assert dispatch["result"]["order_id"] == 501
    assert dispatch["result"]["send_marks"]["fill_label"] == FILL_LABEL_MID_INSIDE


@pytest.mark.asyncio
async def test_take_snapshot_resolves_a_missed_working_order():
    from abcxauto.monitor import PortfolioMonitor

    journal = get_journal()
    pid = journal.record_proposal(
        strategy="limit_order", symbol="NVDA", validation_ok=True
    )
    marks = _working_marks()
    did = journal.record_dispatch(
        pid, True, {"success": True, "order_id": 88}, ts="2026-08-28T12:00:00.000Z"
    )
    journal.record_send_marks(
        proposal_id=pid,
        dispatch_id=did,
        marks=marks,
        result={"success": True, "order_id": 88},
        ts="2026-08-28T12:00:00.000Z",
    )
    journal.resolve_unfilled_sends([{"order_id": 88}], grace_s=0)

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
            return []

    mon = PortfolioMonitor(Session(), Connector())
    await mon.take_snapshot()
    row = journal.recent_send_marks()[0]
    assert row["fill_label"] == FILL_LABEL_MISSED
    assert row["fill_price"] is None
    assert row["bid"] == 10.00
    assert row["ask"] == 10.20
    assert row["sent_price"] == 10.10
