"""Freeze-then-score window. Not a second scorecard. Not PCS HASH_DRIFT."""

from __future__ import annotations

import json
from pathlib import Path

from abcxauto.freeze import (
    assert_unchanged,
    close_freeze,
    constitution_payload,
    format_freeze_line,
    open_freeze,
    score,
)
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.memory import get_journal
from abcxauto.pcs_kill_scorecard import window_aggregates
from abcxauto.scorecard import compute_scorecard, format_scorecard_block
from abcxauto.self_tune import apply_self_tune


def _fill(**kw):
    row = {
        "exec_id": "e1",
        "order_id": 50,
        "symbol": "SPY",
        "sec_type": "STK",
        "side": "BOT",
        "quantity": 1,
        "price": 500.0,
        "commission": 1.0,
        "realized_pnl": 0.0,
        "ts": "2026-01-01T15:00:00.000Z",
    }
    row.update(kw)
    return row


def _pcs_row(**overrides):
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


def test_self_tune_blocked_while_freeze_armed():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 35_000.0})
    open_freeze(20, journal=j)
    out = apply_self_tune({"size_pct_nl": 1.0}, persist=False)
    assert out["status"] == "blocked"
    assert "freeze" in str(out.get("note") or "")
    assert out.get("applied") == {}
    assert out.get("rejected", {}).get("size_pct_nl") == "freeze"


def test_mutate_risk_settings_or_allowlist_drifts_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe_allowlist.json"))
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 35_000.0})
    open_freeze(20, journal=j)
    ok, drifted = assert_unchanged()
    assert ok is True
    assert drifted == []

    risk_path = Path(
        __import__("os").environ.get("ABCXAUTO_RISK_SETTINGS_PATH") or tmp_path / "risk_settings.json"
    )
    risk_path.write_text(json.dumps({"model": "not-the-locked-brain"}, indent=2) + "\n")
    ok_risk, drifted_risk = assert_unchanged()
    assert ok_risk is False
    assert "risk_settings" in drifted_risk

    risk_path.unlink(missing_ok=True)
    from abcxauto.universe import load_allowlist, save_allowlist

    save_allowlist({**load_allowlist(), "custom_symbols": ["ZZZZ"]})
    ok_uni, drifted_uni = assert_unchanged()
    assert ok_uni is False
    assert "universe" in drifted_uni


def test_idle_book_model_cost_fails_vs_cash():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 35_000.0})
    state = open_freeze(1, unseen_required=False, journal=j)
    j.ensure_session_start_nl(35_000.0, ts=state["opened_at"])
    j.record_model_usage(
        stage="test",
        model="grok-4.6",
        input_tokens=100,
        output_tokens=10,
        cost_usd=1.25,
        ts=state["opened_at"],
    )
    facts = score(journal=j, equity=35_000.0)
    assert facts is not None
    assert facts["cash_baseline_pnl"] == 0.0
    assert facts["vs_cash_usd"] < 0
    assert facts["beating_cash"] is False
    closed = close_freeze(journal=j, equity=35_000.0)
    assert closed["verdict"] == "FAIL"


def test_unseen_ok_when_new_symbol_fills_after_open():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 35_000.0})
    assert j.record_fills([_fill(symbol="SPY", exec_id="pre-spy")]) == 1
    state = open_freeze(20, journal=j)
    assert (
        j.record_fills(
            [
                _fill(
                    symbol="AMD",
                    exec_id="post-amd",
                    order_id=51,
                    ts=state["opened_at"],
                )
            ]
        )
        == 1
    )
    facts = score(journal=j, equity=35_000.0)
    assert facts is not None
    assert facts["unseen_ok"] is True
    assert "AMD" in j.symbols_filled_since(state["opened_at"])
    assert "SPY" in j.symbols_filled_before(state["opened_at"])


def test_format_scorecard_block_keeps_header_and_adds_freeze_line():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 35_000.0})
    open_freeze(20, journal=j)
    sc = compute_scorecard(equity=35_000.0, journal=j)
    text = format_scorecard_block(equity=35_000.0, journal=j, sc=sc)
    assert text.startswith("SCORECARD (paper TWS):")
    freeze_lines = [ln for ln in text.splitlines() if ln.startswith("- freeze")]
    assert len(freeze_lines) == 1
    assert format_freeze_line(sc.get("freeze")).startswith("- freeze")


def test_pcs_window_aggregates_keeps_abort_fuse_set():
    win = window_aggregates([_pcs_row()])
    assert win["abort_fuse"] in ("none", "F10", "DD30", "QTY0_STREAK")
    assert win["verdict"] in ("PASS", "FAIL", "ABORT")
    assert win["verdict"] != "HASH_DRIFT"
    assert win["abort_fuse"] != "HASH_DRIFT"
    assert "HASH_DRIFT" not in win.values()
    assert "STRESS_PASS" not in win.values()


def test_compute_scorecard_book_math_and_freeze_key():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 35_000.0})
    sc = compute_scorecard(equity=35_100.0, journal=j)
    assert abs(sc["book_return_pct"] - (100.0 / 35_000.0 * 100.0)) < 1e-9
    assert "freeze" in sc


def test_constitution_uses_llm_system_prompt_constant():
    payload = constitution_payload()
    assert payload["system_prompt"] == SYSTEM_PROMPT
    assert "{mode}" in SYSTEM_PROMPT
