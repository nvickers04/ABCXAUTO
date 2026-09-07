"""PCS Arm v0 N=20 kill scorecard logging hooks (QA measurement only)."""

from __future__ import annotations

import sqlite3

import pytest

from abcxauto.memory import TradeJournal, reset_journal
from abcxauto.pcs_kill_scorecard import (
    DD30_PCT,
    QTY0_STREAK_LIMIT_DEFAULT,
    chicago_session_date,
    make_session_id,
    normalize_session_row,
    validate_fill_mark,
    window_aggregates,
)
from abcxauto.scorecard import (
    normalize_pcs_kill_session,
    pcs_kill_window_aggregates,
)


def _base_payload(**overrides):
    row = {
        "session_id": "pcs-v0-20260906-01",
        "session_date": "2026-09-06",
        "NL": 100_000.0,
        "conservative_pnl": 50.0,
        "model_cost": 2.5,
        "lambda": 0.35,
        "credit": 40.0,
        "max_loss": 200.0,
        "send": "SEND",
        "send_reason": "SEND:gate_clear",
        "qty": 1,
        "f10_ok": True,
        "dd_pct": 1.2,
        "fill_mark": "ibkr_fill",
        "session_start_NL": 100_000.0,
        "ba_commission_USD": 1.25,
        "notes": "unit",
    }
    row.update(overrides)
    return row


def test_make_session_id_and_chicago_date():
    assert make_session_id("2026-09-06", 1) == "pcs-v0-20260906-01"
    assert make_session_id("2026-09-06", 20) == "pcs-v0-20260906-20"
    with pytest.raises(ValueError):
        make_session_id("bad", 1)
    day = chicago_session_date()
    assert len(day) == 10 and day[4] == "-" and day[7] == "-"


def test_schema_validation_derived_and_diagnose():
    row = normalize_session_row(_base_payload())
    assert row["valid"] is True
    assert row["send_credited"] is True
    assert abs(row["session_edge_conservative"] - 47.5) < 1e-9
    assert abs(row["nl_incl_model"] - 99_997.5) < 1e-9
    assert abs(row["rr_credit"] - 0.2) < 1e-9
    assert abs(row["book_return_pct"] - 0.05) < 1e-9
    assert abs(row["model_cost_USD"] - 2.5) < 1e-9
    assert abs(row["model_cost_pct"] - 0.0025) < 1e-9
    assert abs(row["session_score"] - (0.05 - 0.0025)) < 1e-9
    assert row["fill_mark"] == "ibkr_fill"
    assert row["ba_commission_USD"] == 1.25
    # scorecard re-export is the same normalizer
    assert normalize_pcs_kill_session(_base_payload())["valid"] is True


def test_lambda_alias_and_send_reason_other():
    payload = _base_payload()
    del payload["lambda"]
    payload["λ"] = 0.41
    payload["send"] = "NO_SEND"
    payload["send_reason"] = "NO_SEND:other:skew_thin"
    row = normalize_session_row(payload)
    assert row["valid"] is True
    assert row["lambda"] == 0.41
    assert row["send_credited"] is False


def test_mid_fill_mark_rejected_no_send_credit():
    ok, reason = validate_fill_mark("mid")
    assert ok is False
    assert reason == "fill_mark=mid"
    row = normalize_session_row(_base_payload(fill_mark="mid", send="SEND"))
    assert row["valid"] is False
    assert row["send_credited"] is False
    assert any("mid" in r for r in row["invalid_reasons"])


def test_null_model_cost_fail_closed():
    payload = _base_payload()
    payload["model_cost"] = None
    row = normalize_session_row(payload)
    assert row["valid"] is False
    assert "model_cost_null" in row["invalid_reasons"]
    assert row["send_credited"] is False
    assert row["model_cost_USD"] is None


def test_conservative_must_not_exceed_optimistic():
    row = normalize_session_row(
        _base_payload(conservative_pnl=10.0, optimistic_pnl=5.0)
    )
    assert row["valid"] is False
    assert "conservative_pnl>optimistic" in row["invalid_reasons"]


def test_window_mean_session_score_vs_net_conservative():
    rows = [
        _base_payload(
            session_id="pcs-v0-20260906-01",
            conservative_pnl=100.0,
            model_cost=10.0,
            session_start_NL=100_000.0,
            NL=100_000.0,
        ),
        _base_payload(
            session_id="pcs-v0-20260906-02",
            conservative_pnl=50.0,
            model_cost=5.0,
            session_start_NL=100_000.0,
            NL=100_000.0,
        ),
    ]
    win = window_aggregates(rows)
    assert win["invalid_rows"] == 0
    assert abs(win["sum_conservative_pnl"] - 150.0) < 1e-9
    assert abs(win["sum_model_cost"] - 15.0) < 1e-9
    assert abs(win["net_conservative"] - 135.0) < 1e-9
    # session_score = book_return_pct - model_cost_pct
    # row1: 0.10 - 0.01 = 0.09; row2: 0.05 - 0.005 = 0.045; mean = 0.0675
    assert abs(win["mean_session_score"] - 0.0675) < 1e-9
    # mean(session_score) is % of start NL; net_conservative is USD — both present
    assert win["mean_session_score"] != win["net_conservative"]
    assert win["verdict"] == "PASS"
    assert win["abort_fuse"] == "none"
    assert pcs_kill_window_aggregates(rows)["verdict"] == "PASS"


def test_window_fail_when_edge_negative():
    rows = [
        _base_payload(
            session_id="pcs-v0-20260906-01",
            conservative_pnl=1.0,
            model_cost=10.0,
        )
    ]
    win = window_aggregates(rows)
    assert win["net_conservative"] < 0
    assert win["verdict"] == "FAIL"
    assert win["abort_fuse"] == "none"


def test_fuse_f10_abort():
    rows = [
        _base_payload(session_id="pcs-v0-20260906-01", f10_ok=True),
        _base_payload(session_id="pcs-v0-20260906-02", f10_ok=False, send="NO_SEND",
                      send_reason="NO_SEND:f10"),
    ]
    win = window_aggregates(rows)
    assert win["f10_breach_count"] == 1
    assert win["verdict"] == "ABORT"
    assert win["abort_fuse"] == "F10"


def test_fuse_dd30_abort():
    rows = [
        _base_payload(session_id="pcs-v0-20260906-01", dd_pct=12.0),
        _base_payload(session_id="pcs-v0-20260906-02", dd_pct=DD30_PCT, send="NO_SEND",
                      send_reason="NO_SEND:dd_fuse"),
    ]
    win = window_aggregates(rows)
    assert win["max_dd_pct"] == DD30_PCT
    assert win["verdict"] == "ABORT"
    assert win["abort_fuse"] == "DD30"


def test_fuse_qty0_streak_abort_default_limit():
    assert QTY0_STREAK_LIMIT_DEFAULT == 5
    rows = []
    for i in range(1, 6):
        rows.append(
            _base_payload(
                session_id=f"pcs-v0-20260906-{i:02d}",
                qty=0,
                send="NO_SEND",
                send_reason="NO_SEND:qty0_streak",
            )
        )
    win = window_aggregates(rows)
    assert win["qty0_streak_max"] == 5
    assert win["verdict"] == "ABORT"
    assert win["abort_fuse"] == "QTY0_STREAK"

    # Custom limit param
    short = window_aggregates(rows[:3], qty0_streak_limit=3)
    assert short["abort_fuse"] == "QTY0_STREAK"
    under = window_aggregates(rows[:3], qty0_streak_limit=5)
    assert under["abort_fuse"] != "QTY0_STREAK"


def test_invalid_mid_does_not_inflate_send_rate():
    rows = [
        _base_payload(session_id="pcs-v0-20260906-01", send="SEND"),
        _base_payload(
            session_id="pcs-v0-20260906-02",
            send="SEND",
            fill_mark="mid",
        ),
    ]
    win = window_aggregates(rows)
    assert win["invalid_rows"] == 1
    assert win["send_credited"] == 1
    assert abs(win["send_rate"] - 0.5) < 1e-9


def test_journal_persist_and_window(tmp_path):
    db = tmp_path / "pcs_kill.db"
    j = TradeJournal(path=str(db), enabled=True)
    reset_journal(path=str(db), enabled=True)
    try:
        rid = j.record_pcs_kill_session(_base_payload())
        assert rid is not None
        # upsert same session_id
        rid2 = j.record_pcs_kill_session(
            _base_payload(conservative_pnl=60.0, notes="updated")
        )
        assert rid2 == rid
        listed = j.list_pcs_kill_sessions(limit=20)
        assert len(listed) == 1
        assert listed[0]["session_id"] == "pcs-v0-20260906-01"
        assert listed[0]["conservative_pnl"] == 60.0
        assert listed[0]["session_score"] is not None

        j.record_pcs_kill_session(
            _base_payload(
                session_id="pcs-v0-20260906-02",
                conservative_pnl=40.0,
                model_cost=1.0,
            )
        )
        win = j.pcs_kill_window(limit=20)
        assert win["n"] == 2
        assert win["invalid_rows"] == 0
        assert win["verdict"] in ("PASS", "FAIL", "ABORT")
        assert win["abort_fuse"] in ("none", "F10", "DD30", "QTY0_STREAK")

        # schema table exists
        conn = sqlite3.connect(str(db))
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "pcs_kill_sessions" in names
    finally:
        reset_journal(path=str(tmp_path / "reset.db"), enabled=True)


def test_stress_pass_is_not_this_pass():
    """STRESS_PASS must not be emitted by the kill window verdict."""
    win = window_aggregates([_base_payload()])
    assert win["verdict"] in ("PASS", "FAIL", "ABORT")
    assert win["verdict"] != "STRESS_PASS"
    assert "STRESS_PASS" not in win.values()
