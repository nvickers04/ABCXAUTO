"""Grokfolio clock, clamps, and book diffs (no live broker)."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from abcxauto.grokfolio import (
    clamp_holdings,
    diff_book,
    due_kind,
    sleep_until_next_s,
)

_ET = ZoneInfo("America/New_York")
_THU_10 = datetime(2026, 8, 13, 10, 5, tzinfo=_ET)  # Thursday
_THU_14 = datetime(2026, 8, 13, 14, 0, tzinfo=_ET)
_SAT_10 = datetime(2026, 8, 15, 10, 0, tzinfo=_ET)  # Saturday
_CFG = SimpleNamespace(grokfolio_cadence="both")


def test_due_kind_daily_at_10_et_weekday():
    assert (
        due_kind(
            now=_THU_10,
            state={},
            cfg=_CFG,
            session_status="regular",
        )
        == "daily"
    )


def test_due_kind_hourly_hours():
    assert (
        due_kind(
            now=_THU_14,
            state={"last_daily": "2026-08-13"},
            cfg=_CFG,
            session_status="regular",
        )
        == "hourly"
    )
    noon = datetime(2026, 8, 13, 12, 1, tzinfo=_ET)
    assert (
        due_kind(
            now=noon,
            state={"last_daily": "2026-08-13"},
            cfg=SimpleNamespace(grokfolio_cadence="hourly"),
            session_status="regular",
        )
        == "hourly"
    )


def test_due_kind_weekend_none():
    assert (
        due_kind(
            now=_SAT_10,
            state={},
            cfg=_CFG,
            session_status="regular",
        )
        is None
    )


def test_due_kind_already_ran_none():
    assert (
        due_kind(
            now=_THU_10,
            state={"last_daily": "2026-08-13", "last_hourly": "2026-08-13T10"},
            cfg=_CFG,
            session_status="regular",
        )
        is None
    )
    closed = due_kind(
        now=_THU_10,
        state={},
        cfg=_CFG,
        session_status="closed",
    )
    assert closed is None


def test_clamp_holdings_drops_illegal_symbols():
    out = clamp_holdings(
        [
            {"symbol": "MSFT", "weight_pct": 8, "stop_pct": 8},
            {"symbol": "FAKEXYZ", "weight_pct": 8, "stop_pct": 8},
            {"symbol": "aapl", "weight_pct": 7, "stop_pct": 8},
        ],
        legal={"MSFT", "AAPL"},
        max_n=15,
        max_position_pct=20.0,
        max_risk_per_trade_pct=1.0,
    )
    syms = [h["symbol"] for h in out]
    assert "FAKEXYZ" not in syms
    assert syms == ["MSFT", "AAPL"]


def test_clamp_holdings_weight_cap_vs_position_and_risk():
    # stop 8%, risk/trade 1% => size cap 12.5%; also vs max_position_pct.
    wide = clamp_holdings(
        [{"symbol": "MSFT", "weight_pct": 40, "stop_pct": 8}],
        legal={"MSFT"},
        max_n=15,
        max_position_pct=20.0,
        max_risk_per_trade_pct=1.0,
    )
    assert len(wide) == 1
    assert wide[0]["weight_pct"] == 12.5
    tight_pos = clamp_holdings(
        [{"symbol": "MSFT", "weight_pct": 40, "stop_pct": 8}],
        legal={"MSFT"},
        max_n=15,
        max_position_pct=5.0,
        max_risk_per_trade_pct=1.0,
    )
    assert tight_pos[0]["weight_pct"] == 5.0


def test_clamp_holdings_unique_and_max_n():
    raw = [
        {"symbol": "MSFT", "weight_pct": 8, "stop_pct": 8},
        {"symbol": "MSFT", "weight_pct": 9, "stop_pct": 8},
        {"symbol": "AAPL", "weight_pct": 8, "stop_pct": 8},
        {"symbol": "NVDA", "weight_pct": 8, "stop_pct": 8},
    ]
    out = clamp_holdings(
        raw,
        legal={"MSFT", "AAPL", "NVDA"},
        max_n=2,
        max_position_pct=20.0,
        max_risk_per_trade_pct=1.0,
    )
    assert [h["symbol"] for h in out] == ["MSFT", "AAPL"]


def test_diff_book_close_missing_buy_new_skip_small_drift():
    target = [
        {"symbol": "MSFT", "weight_pct": 10.0, "stop_pct": 8, "thesis": "core"},
        {"symbol": "AAPL", "weight_pct": 10.0, "stop_pct": 8, "thesis": "core"},
    ]
    current = {"MSFT": 10.4, "XYZ": 5.0}  # MSFT within 1% NL drift; XYZ not in book
    actions = diff_book(target, current_w=current, net_liq=100_000.0, drift_pct_of_nl=1.0)
    ops = {(a["op"], a["symbol"]) for a in actions}
    assert ("close", "XYZ") in ops
    assert ("buy", "AAPL") in ops
    assert ("resize", "MSFT") not in ops
    assert ("buy", "MSFT") not in ops


def test_sleep_until_next_s_positive():
    after_rth = datetime(2026, 8, 13, 16, 30, tzinfo=_ET)
    wait = sleep_until_next_s(
        now=after_rth,
        cfg=_CFG,
        session_status="closed",
    )
    assert wait >= 60.0
    before_open = datetime(2026, 8, 13, 9, 0, tzinfo=_ET)
    wait_open = sleep_until_next_s(
        now=before_open,
        cfg=SimpleNamespace(grokfolio_cadence="daily"),
        session_status="pre",
    )
    assert wait_open >= 60.0
