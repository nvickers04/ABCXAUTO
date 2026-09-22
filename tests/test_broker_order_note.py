"""Broker cancel / IBKR error notes — durable without a live broker."""

from __future__ import annotations

from abcxauto.memory import get_journal
from abcxauto.memory.notes import record_broker_order_note


def test_record_broker_order_note_records_symbol_and_code():
    out = record_broker_order_note(
        symbol="AVGO",
        order_id=23210,
        order_type="STP",
        status="Cancelled",
        error_code=10326,
    )
    assert out.get("ok") is True
    note = (out.get("note") or {})
    body = str(note.get("body") or "")
    assert "AVGO" in body
    assert "10326" in body
    assert "23210" in body
    assert note.get("symbol") == "AVGO"

    listed = get_journal().get_notes(tags=["broker", "cancel"])
    bodies = [str(n.get("body") or "") for n in listed.get("notes") or []]
    assert any("AVGO" in b and "10326" in b for b in bodies)


def test_quiet_ibkr_farm_codes_do_not_write_notes():
    before = get_journal().list_notes().get("n") or 0
    for code in (2104, 2106, 2108, 2158):
        out = record_broker_order_note(
            symbol="AVGO",
            order_id=99,
            error_code=code,
            detail="Market data farm connection is OK",
        )
        assert out.get("ok") is False
        assert out.get("error") == "quiet_code"
    assert (get_journal().list_notes().get("n") or 0) == before
