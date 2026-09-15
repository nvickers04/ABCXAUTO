"""Broker-initiated cancels must reach model facts with IBKR reason text."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import _book_payload
from abcxauto.broker.connector import IBKRConnector, OrderState, OrderStatus
from abcxauto.risk_gates import reset_risk_gate
from abcxauto.think_stream import write_last_turn
from abcxauto.world_state import WorldState, build_world_state, compact_broker_rejects


_AAPL_202 = (
    "Order Canceled - reason:We cannot accept an order at a limit price at or "
    "more aggressive than 2.87. Please submit your order using a limit price "
    "that is closer to the current market price of 2.35."
)


def _fresh_connector(monkeypatch) -> IBKRConnector:
    IBKRConnector._instance = None
    monkeypatch.setattr(IBKRConnector, "_start_heartbeat", lambda self: None)
    conn = IBKRConnector()
    return conn


def _trade(order_id: int, symbol: str = "AAPL", order_type: str = "LMT") -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        order=SimpleNamespace(orderId=order_id, orderType=order_type, action="BUY"),
        orderStatus=SimpleNamespace(
            status="Cancelled",
            filled=0,
            remaining=1,
            avgFillPrice=0.0,
        ),
    )


def test_broker_cancel_202_retained_with_order_id_and_reason(monkeypatch):
    conn = _fresh_connector(monkeypatch)
    conn._on_order_status(_trade(20759))
    conn.get_cancel_attribution = lambda *_a, **_k: {"kind": "broker_cancel"}
    conn._on_error(20759, 202, _AAPL_202, "")

    rows = conn.get_broker_rejects()
    assert len(rows) == 1
    assert rows[0]["order_id"] == 20759
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["kind"] == "broker_cancel"
    assert "limit price at or more aggressive than 2.87" in rows[0]["reason"]


def test_self_cancel_202_not_recorded_as_broker_reject(monkeypatch):
    conn = _fresh_connector(monkeypatch)
    conn._record_local_cancel_request(99, source="protect")
    conn._on_order_status(_trade(99, symbol="SPY"))
    conn._on_error(99, 202, "Order Canceled - reason:Requested by customer", "")

    assert conn.get_broker_rejects() == []


def test_broker_reject_buffer_stays_bounded(monkeypatch):
    conn = _fresh_connector(monkeypatch)
    conn.get_cancel_attribution = lambda *_a, **_k: {"kind": "broker_cancel"}
    cap = IBKRConnector._BROKER_REJECT_CAP
    for oid in range(1, cap + 6):
        conn._touch_order_meta(oid, symbol=f"S{oid}", order_type="LMT", action="BUY")
        conn._on_error(oid, 202, f"reason {oid}", "")

    rows = conn.get_broker_rejects()
    assert len(rows) == cap
    assert rows[0]["order_id"] == 6
    assert rows[-1]["order_id"] == cap + 5


def test_broker_rejects_surface_in_book_and_last_turn(monkeypatch, tmp_path):
    conn = _fresh_connector(monkeypatch)
    conn._on_order_status(_trade(20759))
    conn.get_cancel_attribution = lambda *_a, **_k: {"kind": "broker_cancel"}
    conn._on_error(20759, 202, _AAPL_202, "")

    snap = {
        "positions": [],
        "open_orders": [],
        "fills": [],
        "broker_rejects": conn.get_broker_rejects(),
        "account": {"netliquidation": 50_000.0},
        "reality_pulse": {"session": {"status": "regular"}},
        "protection": {"unprotected_symbols": []},
    }
    ws = build_world_state(cycle=1, snap=snap, opportunities=[], news_items=[])
    book = json.loads(json.dumps(_book_payload(ws)))
    reject = book["world"]["broker_rejects"][0]
    assert reject["order_id"] == 20759
    assert reject["symbol"] == "AAPL"
    assert reject["kind"] == "broker_cancel"
    assert "2.87" in reject["reason"]
    assert reject["working"] is False
    assert reject["gone"] is True

    monkeypatch.setattr("abcxauto.think_stream.LAST_TURN_PATH", tmp_path / "last_turn.json")
    write_last_turn(
        {
            "strat": "hunt",
            "rationale": "sent lmt",
            "tool_trace": ["send"],
            "sends": 1,
            "world_state": {
                "broker_rejects": ws.broker_rejects,
                "positions": [],
                "open_orders": [],
            },
        }
    )
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["broker_rejects"][0]["order_id"] == 20759
    assert "2.35" in last["broker_rejects"][0]["reason"]
    assert last["broker_rejects"][0]["working"] is False
    assert last["broker_rejects"][0]["gone"] is True


def test_order_state_meta_correlates_reject_to_sent_ticket(monkeypatch):
    conn = _fresh_connector(monkeypatch)
    with conn._order_state_lock:
        conn._order_states[501] = OrderState(
            order_id=501,
            symbol="NVDA",
            action="SELL",
            quantity=2,
            order_type="LMT",
            status=OrderStatus.SUBMITTED,
        )
    conn.get_cancel_attribution = lambda *_a, **_k: {"kind": "broker_cancel"}
    conn._on_error(501, 201, "Order rejected - reason:No trading permissions", "")

    row = conn.get_broker_rejects()[0]
    assert row["order_id"] == 501
    assert row["symbol"] == "NVDA"
    assert row["kind"] == "rejected"
    assert row["order_type"] == "LMT"
    assert row["action"] == "SELL"


def test_compact_broker_rejects_trims_and_preserves_fields():
    raw = [
        {
            "order_id": 1,
            "symbol": "AAPL",
            "kind": "broker_cancel",
            "reason": "x" * 300,
            "order_type": "LMT",
            "action": "BUY",
        }
    ]
    rows = compact_broker_rejects(raw)
    assert rows[0]["type"] == "LMT"
    assert rows[0]["action"] == "BUY"
    assert len(rows[0]["reason"]) == 240
