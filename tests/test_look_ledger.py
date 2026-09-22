"""Look ledger: append-only usage rows + F10 session/week sums."""

from __future__ import annotations

from pathlib import Path

import pytest

from abcxauto import look_ledger
from abcxauto.look_ledger import (
    F10_HARD_USD,
    F10_PREFERRED_USD,
    append_call,
    latest_row,
    session_spend,
    week_spend,
)

LEDGER_SRC = Path(look_ledger.__file__).read_text(encoding="utf-8")


def test_f10_numbers():
    assert F10_PREFERRED_USD == 10.0
    assert F10_HARD_USD == 15.0


def test_no_brief_loop_halted_anywhere():
    # Hygiene: latch name must not live on the bill module or its payloads.
    banned = "brief" + "_loop_halted"
    assert banned not in LEDGER_SRC
    row = {
        "asof": "2026-09-22T12:00:00Z",
        "model": "grok-4.6",
        "input_tokens": 1,
        "output_tokens": 1,
        "usd": 0.01,
        "session": "regular",
    }
    assert banned not in row


def test_append_003_session_sum(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    row = append_call(
        asof="2026-09-22T12:00:00Z",
        model="grok-4.6",
        input_tokens=100,
        output_tokens=50,
        usd=0.03,
        session="regular",
        path=path,
    )
    assert row["usd"] == 0.03
    assert "turns" not in row
    assert "tool_count" not in row
    assert "verdict" not in row
    assert "brief_loop_halted" not in row
    assert isinstance(row["usd"], float)

    spend = session_spend("2026-09-22", path=path)
    assert spend == {
        "usd": 0.03,
        "unknown": False,
        "preferred": False,
        "hard": False,
    }
    assert spend["usd"] != 0.20
    assert "brief_loop_halted" not in spend


def test_unknown_usd_hard_no_estimate(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    row = append_call(
        asof="2026-09-22T13:00:00Z",
        model="grok-4.6",
        input_tokens=10,
        output_tokens=5,
        usd=None,
        session="regular",
        path=path,
    )
    assert row["usd"] == "unknown"

    spend = session_spend("2026-09-22", path=path)
    assert spend["usd"] is None
    assert spend["unknown"] is True
    assert spend["preferred"] is False
    assert spend["hard"] is True
    assert spend["usd"] != 0.20
    assert "brief_loop_halted" not in spend


def test_sum_1501_is_hard(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    append_call(
        asof="2026-09-22T10:00:00Z",
        model="grok-4.6",
        input_tokens=1,
        output_tokens=1,
        usd=10.0,
        session="regular",
        path=path,
    )
    append_call(
        asof="2026-09-22T11:00:00Z",
        model="grok-4.6",
        input_tokens=1,
        output_tokens=1,
        usd=5.01,
        session="regular",
        path=path,
    )
    spend = session_spend("2026-09-22", path=path)
    assert spend["usd"] == pytest.approx(15.01)
    assert spend["hard"] is True
    assert spend["preferred"] is True
    assert spend["unknown"] is False


def test_sum_1050_preferred_not_hard(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    append_call(
        asof="2026-09-22T10:00:00Z",
        model="grok-4.6",
        input_tokens=1,
        output_tokens=1,
        usd=10.50,
        session="regular",
        path=path,
    )
    spend = session_spend("2026-09-22", path=path)
    assert spend["usd"] == pytest.approx(10.50)
    assert spend["preferred"] is True
    assert spend["hard"] is False
    assert spend["unknown"] is False


def test_week_spend_iso_et(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    # Monday 2026-09-21 is ISO week 2026-W39 in ET.
    append_call(
        asof="2026-09-21T15:00:00Z",
        model="grok-4.6",
        input_tokens=1,
        output_tokens=1,
        usd=1.25,
        session="premarket",
        path=path,
    )
    append_call(
        asof="2026-09-14T15:00:00Z",
        model="grok-4.6",
        input_tokens=1,
        output_tokens=1,
        usd=9.0,
        session="regular",
        path=path,
    )
    spend = week_spend("2026-W39", path=path)
    assert spend["usd"] == pytest.approx(1.25)
    assert spend["hard"] is False


def test_latest_row(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    assert latest_row(path=path) is None
    append_call(
        asof="2026-09-22T10:00:00Z",
        model="a",
        input_tokens=1,
        output_tokens=1,
        usd=0.01,
        session="regular",
        path=path,
    )
    last = append_call(
        asof="2026-09-22T11:00:00Z",
        model="b",
        input_tokens=2,
        output_tokens=2,
        usd=0.02,
        session="regular",
        path=path,
    )
    assert latest_row(path=path) == last


def test_nan_and_negative_usd_unknown(tmp_path: Path):
    path = tmp_path / "look_ledger.jsonl"
    for bad in (float("nan"), float("inf"), -0.01, "nope"):
        row = append_call(
            asof="2026-09-22T12:00:00Z",
            model="grok-4.6",
            input_tokens=1,
            output_tokens=1,
            usd=bad,
            session="regular",
            path=path,
        )
        assert row["usd"] == "unknown"


def test_does_not_touch_default_ledger(tmp_path: Path):
    """Tests must never write the real data/state ledger."""
    real = look_ledger.DEFAULT_PATH
    existed = real.is_file()
    before = real.read_bytes() if existed else None
    path = tmp_path / "ok.jsonl"
    append_call(
        asof="2026-09-22T12:00:00Z",
        model="grok-4.6",
        input_tokens=1,
        output_tokens=1,
        usd=0.01,
        session="regular",
        path=path,
    )
    session_spend("2026-09-22", path=path)
    week_spend("2026-W39", path=path)
    latest_row(path=path)
    assert path.is_file()
    if existed:
        assert real.read_bytes() == before
    else:
        assert not real.exists()
