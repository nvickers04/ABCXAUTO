"""Book return vs model cost scorecard."""

from abcxauto.memory import get_journal
from abcxauto.scorecard import (
    compute_scorecard,
    estimate_cost_usd,
    estimate_tokens,
    format_scorecard_block,
    usage_from_response,
)


def test_estimate_tokens_and_cost():
    n = estimate_tokens("abcd" * 25)
    assert n >= 1
    cost = estimate_cost_usd(1_000_000, 1_000_000, in_rate=3.0, out_rate=15.0)
    assert abs(cost - 18.0) < 1e-9
    short = estimate_cost_usd(1_000, 1_000)
    assert abs(short - 0.008) < 1e-9
    long = estimate_cost_usd(1_000_000, 1_000_000)
    assert abs(long - 16.0) < 1e-9


def test_usage_from_response_reads_sdk_and_falls_back():
    class Usage:
        prompt_tokens = 1200
        completion_tokens = 80
        reasoning_tokens = 400
        cached_tokens = 100

    class Resp:
        usage = Usage()

    used = usage_from_response(Resp())
    assert used["input_tokens"] == 1200
    assert used["cached_tokens"] == 100
    assert used["output_tokens"] == 80
    assert used["reasoning_tokens"] == 400
    fallback = usage_from_response(None, think_text="abcd" * 20, say_text="efgh" * 10)
    assert fallback["input_tokens"] == 0
    assert fallback["output_tokens"] > 0
    j = get_journal()
    j.record_model_usage(
        stage="grok",
        input_tokens=1200,
        output_tokens=80,
        cached_tokens=100,
        cost_usd=0.01,
    )
    tot = j.model_usage_totals()
    assert tot["cached_tokens"] == 100
    assert tot["input_tokens"] == 1200


def test_scorecard_beating_when_book_ahead():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 1100.0, "DailyPnL": 10.0})
    j.record_model_usage(stage="judge", input_tokens=100, output_tokens=50, cost_usd=0.01)
    sc = compute_scorecard(equity=1100.0, journal=j)
    assert sc["startup_cash"] == 1000.0
    assert sc["book_pnl"] == 100.0
    assert sc["model_cost_usd"] == 0.01
    assert sc["beating_model"] is True
    assert abs(sc["edge_usd"] - 99.99) < 1e-9
    assert "prefer" not in sc


def test_scorecard_losing_to_model_bill():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 999.0, "DailyPnL": -1.0})
    j.record_model_usage(stage="act", input_tokens=10, output_tokens=10, cost_usd=5.0)
    sc = compute_scorecard(equity=999.0, journal=j)
    assert sc["beating_model"] is False
    assert sc["edge_usd"] < 0
    text = format_scorecard_block(equity=999.0, journal=j)
    assert "LOSING" in text
    assert "do not sit" not in text.lower()
    assert "not cash" not in text.lower()
    assert "Do not skip protect" not in text


def test_scorecard_return_is_pct_of_starting_net_liq():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1_000_000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 1_000_100.0, "DailyPnL": 100.0})
    sc = compute_scorecard(equity=1_000_100.0, journal=j)
    assert sc["book_pnl"] == 100.0
    assert abs(sc["book_return_pct"] - 0.01) < 1e-9


def test_scorecard_no_history_does_not_invent_pnl():
    j = get_journal()
    sc = compute_scorecard(equity=1_000_000.0, journal=j)
    assert sc["book_pnl"] is None
    assert sc["startup_cash"] is None


def test_scorecard_windows_fastest_beating():
    from datetime import datetime, timedelta, timezone

    j = get_journal()
    now = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    j.record_snapshot(
        account={"NetLiquidation": 1000.0, "DailyPnL": 0.0},
        ts=iso(now - timedelta(hours=3)),
    )
    j.record_snapshot(
        account={"NetLiquidation": 1000.0, "DailyPnL": 0.0},
        ts=iso(now - timedelta(minutes=90)),
    )
    j.record_snapshot(
        account={"NetLiquidation": 1030.0, "DailyPnL": 30.0},
        ts=iso(now),
    )
    j.record_model_usage(
        stage="act", input_tokens=10, output_tokens=10, cost_usd=1.0, ts=iso(now - timedelta(minutes=20))
    )
    sc = compute_scorecard(equity=1030.0, journal=j, now=now)
    assert sc["beating_model"] is True
    assert "1h" in sc["windows"]
    assert sc["windows"]["1h"]["coverage"] == "ok"
    assert sc["windows"]["1h"]["beating_model"] is True
    assert sc["fastest_beating"] in ("15m", "1h", "4h")
    text = format_scorecard_block(equity=1030.0, journal=j, sc=sc)
    assert "fastest_beating" in text
    assert "windows" in text
    assert "prefer" not in sc
    assert "prefer" not in text.lower()


def test_scorecard_thin_window_not_fastest():
    from datetime import datetime, timedelta, timezone

    j = get_journal()
    now = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    j.record_snapshot(
        account={"NetLiquidation": 1000.0, "DailyPnL": 0.0},
        ts=iso(now - timedelta(minutes=5)),
    )
    j.record_snapshot(
        account={"NetLiquidation": 1100.0, "DailyPnL": 100.0},
        ts=iso(now),
    )
    sc = compute_scorecard(equity=1100.0, journal=j, now=now)
    h1 = sc["windows"]["1h"]
    assert h1["coverage"] in ("thin", "none")
    assert sc["fastest_beating"] != "1h" or h1["coverage"] == "ok"


def test_scorecard_session_is_not_inception(monkeypatch):
    class J:
        def startup_cash(self):
            return 36638.0

        def first_snapshot(self):
            return 36638.0, "2026-07-28T00:00:00Z"

        def model_usage_totals(self):
            return {"calls": 10, "cost_usd": 2.0, "input_tokens": 0, "output_tokens": 0}

        def last_session_marker(self):
            return {
                "ts": "2026-08-19T12:00:00Z",
                "model": "grok-4.6",
                "net_liquidation": 35000.0,
            }

        def model_usage_since(self, _ts):
            return {"calls": 3, "cost_usd": 0.40, "input_tokens": 0, "output_tokens": 0}

        def closed_fill_stats_since(self, _ts):
            return {"n": 2, "wins": 1, "sum": 10.0}

        def nav_at_or_before(self, _ts):
            return 35000.0, "2026-08-19T12:00:00Z"

        def snapshot_count_since(self, _ts):
            return 4

    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"model": "grok-4.6"})(),
    )
    sc = compute_scorecard(equity=35100.0, journal=J())
    assert sc["since_start"]["startup_cash"] == 36638.0
    assert abs(sc["since_start"]["book_pnl"] - (35100.0 - 36638.0)) < 1e-9
    assert sc["session"]["book_pnl"] == 100.0
    assert abs(sc["session"]["edge_usd"] - 99.6) < 1e-9
    assert sc["session"]["fills"] == 2
    assert sc["session"]["wins"] == 1
