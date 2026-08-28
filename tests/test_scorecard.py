"""Book return vs model cost scorecard."""

from abcxauto.memory import get_journal
from abcxauto.scorecard import (
    compute_scorecard,
    estimate_cost_usd,
    estimate_tokens,
    format_scorecard_block,
    rth_session_start,
    spy_return_pct,
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
    assert abs(sc["model_cost_pct"] - 0.001) < 1e-9
    assert sc["beating_model"] is True
    assert abs(sc["edge_usd"] - 99.99) < 1e-9
    assert abs(sc["edge_pct"] - 9.999) < 1e-9
    assert abs(sc["since_start"]["model_cost_pct"] - 0.001) < 1e-9
    assert abs(sc["since_start"]["edge_pct"] - 9.999) < 1e-9
    assert "prefer" not in sc
    text = format_scorecard_block(equity=1100.0, journal=j, sc=sc)
    assert "book_return=+10.00% of starting NetLiq (+100.00$)" in text
    assert "SCORECARD (paper TWS):" in text
    assert "model_cost=0.0010% of starting NetLiq ($0.0100 real xAI" in text
    assert "edge=+9.9990% (+99.99$) → BEATING" in text


def test_scorecard_losing_to_model_bill():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 999.0, "DailyPnL": -1.0})
    j.record_model_usage(stage="act", input_tokens=10, output_tokens=10, cost_usd=5.0)
    sc = compute_scorecard(equity=999.0, journal=j)
    assert sc["beating_model"] is False
    assert sc["edge_usd"] < 0
    assert abs(sc["model_cost_pct"] - 0.5) < 1e-9
    assert abs(sc["edge_pct"] - (-0.6)) < 1e-9
    text = format_scorecard_block(equity=999.0, journal=j)
    assert "LOSING" in text
    assert "model_cost=0.5000% of starting NetLiq ($5.0000 real xAI" in text
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
    assert sc["model_cost_pct"] is None
    assert sc["edge_pct"] is None


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
    # 1h start_nl=1000, cost=1 → 0.1%; edge=29 → 2.9%
    assert abs(sc["windows"]["1h"]["model_cost_pct"] - 0.1) < 1e-9
    assert abs(sc["windows"]["1h"]["edge_pct"] - 2.9) < 1e-9
    assert sc["windows"]["1h"]["spy_return_pct"] is None
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


def test_rth_session_start_is_et_regular_bell():
    from datetime import datetime, timezone

    # Friday 12:00 ET (16:00 UTC in August) → that day's 09:30 ET.
    bell, day = rth_session_start(datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc))
    assert day == "2026-08-28"
    assert bell.hour == 13 and bell.minute == 30  # 09:30 EDT = 13:30 UTC
    # Friday 08:00 ET → previous weekday (Thursday).
    _bell, pre = rth_session_start(datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert pre == "2026-08-27"
    # Saturday → Friday.
    _bell, wknd = rth_session_start(datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc))
    assert wknd == "2026-08-28"


def test_scorecard_session_is_not_inception(monkeypatch):
    from datetime import datetime, timezone

    now = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)

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

        def model_usage_since(self, ts):
            if str(ts).startswith("2026-08-28"):
                return {"calls": 3, "cost_usd": 0.40, "input_tokens": 0, "output_tokens": 0}
            return {"calls": 10, "cost_usd": 2.0, "input_tokens": 0, "output_tokens": 0}

        def closed_fill_stats_since(self, ts):
            if str(ts).startswith("2026-08-28"):
                return {"n": 2, "wins": 1, "sum": 10.0}
            return {"n": 9, "wins": 4, "sum": 100.0}

        def nav_at_or_before(self, ts):
            return 36638.0, "2026-07-28T00:00:00Z"

        def nav_at_or_after(self, ts):
            if str(ts).startswith("2026-08-28"):
                return 35000.0, "2026-08-28T13:35:00.000Z"
            return 36638.0, "2026-07-28T00:00:00Z"

        def snapshot_count_since(self, _ts):
            return 4

        def commissions_since(self, ts):
            return 1.25 if str(ts).startswith("2026-08-28") else 9.0

        def nav_path_since(self, ts):
            if str(ts).startswith("2026-08-28"):
                return [("2026-08-28T13:35:00.000Z", 35000.0), ("2026-08-28T16:00:00.000Z", 35100.0)]
            return []

    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"model": "grok-4.6"})(),
    )
    sc = compute_scorecard(equity=35100.0, journal=J(), now=now, spy={})
    assert sc["since_start"]["startup_cash"] == 36638.0
    assert abs(sc["since_start"]["book_pnl"] - (35100.0 - 36638.0)) < 1e-9
    assert abs(sc["model_cost_pct"] - (2.0 / 36638.0 * 100.0)) < 1e-9
    assert abs(sc["edge_pct"] - ((35100.0 - 36638.0 - 2.0) / 36638.0 * 100.0)) < 1e-9
    assert sc["session"]["session_date"] == "2026-08-28"
    assert sc["session"]["kind"] == "rth"
    assert sc["session"]["book_pnl"] == 100.0
    assert abs(sc["session"]["edge_usd"] - 99.6) < 1e-9
    assert abs(sc["session"]["model_cost_pct"] - (0.40 / 35000.0 * 100.0)) < 1e-9
    assert abs(sc["session"]["edge_pct"] - (99.6 / 35000.0 * 100.0)) < 1e-9
    assert sc["session"]["fills"] == 2
    assert sc["session"]["wins"] == 1
    assert sc["session"]["commissions_usd"] == 1.25
    assert sc["session"]["end_nl"] == 35100.0
    assert "beating_model" not in sc["session"]
    assert sc["beating_model"] is False  # inception edge: -1538 - 2 < 0
    text = format_scorecard_block(equity=35100.0, journal=J(), sc=sc)
    lines = text.strip().splitlines()
    assert lines[0] == "SCORECARD (paper TWS):"
    assert lines[1].startswith("- session ")
    assert "2026-08-28 RTH" in lines[1]
    assert "book=+0.29%" in lines[1]
    assert "book=+0.29% paper" not in lines[1]
    assert "model_cost=0.0011% ($0.4000 real xAI)" in lines[1]
    assert "edge=+0.2846%" in lines[1]
    assert "fills=1/2" in lines[1]
    assert "model=grok-4.6" in lines[1]
    assert "since=2026-08-19" not in lines[1]
    assert "vsSPY=—" in lines[1]
    assert any(line.startswith("- first_NL=") for line in lines)
    assert "of starting NetLiq" in text
    assert "$2.0000 real xAI" in text  # inception real bill visible
    assert "LOSING to the model bill" in text
    assert "BEATING the model bill" not in text


def test_format_scorecard_omits_session_when_absent():
    sc = {
        "startup_cash": 1000.0,
        "net_liquidation": 1100.0,
        "book_pnl": 100.0,
        "book_return_pct": 10.0,
        "model_calls": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "model_cost_usd": 0.01,
        "model_cost_pct": 0.001,
        "edge_usd": 99.99,
        "edge_pct": 9.999,
        "beating_model": True,
        "fastest_beating": None,
        "best_pace": None,
        "windows": {},
        "session": None,
    }
    text = format_scorecard_block(sc=sc)
    assert "- session " not in text
    assert "first_NL=1000.00" in text
    assert "book_return=+10.00%" in text
    assert "model_cost=0.0010% of starting NetLiq ($0.0100 real xAI" in text
    assert "edge=+9.9990%" in text
    assert "BEATING" in text


def test_beating_model_still_dollar_edge_not_pct():
    """Tiny positive dollar edge stays BEATING even when pct of a huge NL is tiny."""
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1_000_000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 1_000_001.0, "DailyPnL": 1.0})
    j.record_model_usage(stage="act", input_tokens=1, output_tokens=1, cost_usd=0.5)
    sc = compute_scorecard(equity=1_000_001.0, journal=j)
    assert sc["edge_usd"] == 0.5
    assert sc["beating_model"] is True
    assert sc["edge_pct"] is not None
    assert abs(sc["edge_pct"] - 0.00005) < 1e-12
    text = format_scorecard_block(equity=1_000_001.0, journal=j, sc=sc)
    assert "$0.5000 real xAI" in text
    assert "BEATING" in text


def test_live_scorecard_does_not_split_paper_vs_real(monkeypatch):
    """Live: book and model cost are both real — no paper/xAI split labels."""
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type(
            "C",
            (),
            {"is_paper": False, "trading_mode": "live", "model": "grok-4.6"},
        )(),
    )
    sc = {
        "startup_cash": 1000.0,
        "net_liquidation": 1100.0,
        "book_pnl": 100.0,
        "book_return_pct": 10.0,
        "model_calls": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "model_cost_usd": 0.01,
        "model_cost_pct": 0.001,
        "edge_usd": 99.99,
        "edge_pct": 9.999,
        "beating_model": True,
        "fastest_beating": None,
        "best_pace": None,
        "windows": {},
        "session": {
            "model": "grok-4.6",
            "started_at": "2026-08-19T12:00:00Z",
            "startup_nl": 1000.0,
            "book_pnl": 100.0,
            "model_cost_usd": 0.01,
            "model_cost_pct": 0.001,
            "model_calls": 1,
            "edge_usd": 99.99,
            "edge_pct": 9.999,
            "fills": 0,
            "wins": 0,
        },
    }
    text = format_scorecard_block(sc=sc)
    assert "SCORECARD (live TWS):" in text
    assert " paper" not in text
    assert "real xAI" not in text
    assert "model_cost=0.0010% of starting NetLiq ($0.0100 cash," in text
    assert "book_return=+10.00% of starting NetLiq (+100.00$)" in text
    assert "model_cost=0.0010% ($0.0100)" in text  # session line, both real
    assert sc["beating_model"] is True


def test_scorecard_session_does_not_inherit_pre_session_nav(monkeypatch):
    """RTH start must not steal leftover NAV from a prior day."""
    from datetime import datetime, timezone

    class J:
        def startup_cash(self):
            return 36638.0

        def first_snapshot(self):
            return 36638.0, "2026-07-28T00:00:00Z"

        def model_usage_totals(self):
            return {"calls": 1, "cost_usd": 0.1, "input_tokens": 0, "output_tokens": 0}

        def last_session_marker(self):
            return {
                "ts": "2026-08-25T13:00:00.000Z",
                "model": "grok-4.6",
                "net_liquidation": None,
            }

        def model_usage_since(self, _ts):
            return {"calls": 1, "cost_usd": 0.1, "input_tokens": 0, "output_tokens": 0}

        def closed_fill_stats_since(self, _ts):
            return {"n": 0, "wins": 0, "sum": 0.0}

        def nav_at_or_before(self, _ts):
            return 36638.0, "2026-07-28T00:00:00Z"

        def nav_at_or_after(self, ts):
            if str(ts).startswith("2026-08-25"):
                return 35000.0, "2026-08-25T13:35:00.000Z"
            return 36638.0, "2026-07-28T00:00:00Z"

        def snapshot_count_since(self, _ts):
            return 1

    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"model": "grok-4.6"})(),
    )
    sc = compute_scorecard(
        equity=35100.0,
        journal=J(),
        now=datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc),
        spy={},
    )
    assert sc["session"]["session_date"] == "2026-08-25"
    assert sc["session"]["startup_nl"] == 35000.0
    assert sc["session"]["book_pnl"] == 100.0
    assert abs(sc["session"]["model_cost_pct"] - (0.1 / 35000.0 * 100.0)) < 1e-9
    assert abs(sc["session"]["edge_pct"] - (99.9 / 35000.0 * 100.0)) < 1e-9
    # Inception still uses the first snap. Session must not.
    assert sc["startup_cash"] == 36638.0
    assert sc["book_pnl"] == 35100.0 - 36638.0


def test_scorecard_empty_model_does_not_inherit_named_session(monkeypatch):
    from datetime import datetime, timezone

    class J:
        def startup_cash(self):
            return 1000.0

        def first_snapshot(self):
            return 1000.0, "2026-08-01T00:00:00Z"

        def model_usage_totals(self):
            return {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}

        def last_session_marker(self):
            return {
                "ts": "2026-08-19T12:00:00Z",
                "model": "grok-4.6",
                "net_liquidation": 900.0,
            }

        def model_usage_since(self, _ts):
            return {"calls": 4, "cost_usd": 1.0, "input_tokens": 0, "output_tokens": 0}

        def closed_fill_stats_since(self, _ts):
            return {"n": 3, "wins": 1, "sum": 50.0}

        def snapshot_count_since(self, _ts):
            return 2

    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"model": ""})(),
    )
    sc = compute_scorecard(
        equity=1100.0,
        journal=J(),
        now=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
        spy={},
    )
    assert sc["session"] is not None
    assert sc["session"]["session_date"] == "2026-08-28"
    assert sc["session"]["startup_nl"] != 900.0
    assert sc["session"]["book_pnl"] != 200.0
    assert sc["book_pnl"] == 100.0


def test_scorecard_stale_window_does_not_use_leftover_nl():
    """A 3-day-old snap is not the 15m NetLiq base."""
    from datetime import datetime, timedelta, timezone

    j = get_journal()
    now = datetime(2026, 8, 25, 16, 0, tzinfo=timezone.utc)

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    j.record_snapshot(
        account={"NetLiquidation": 1000.0, "DailyPnL": 0.0},
        ts=iso(now - timedelta(days=3)),
    )
    j.record_snapshot(
        account={"NetLiquidation": 1300.0, "DailyPnL": 50.0},
        ts=iso(now),
    )
    sc = compute_scorecard(equity=1300.0, journal=j, now=now)
    h15 = sc["windows"]["15m"]
    assert h15["coverage"] == "stale"
    assert h15["book_pnl"] is None
    assert h15["book_return_pct"] is None
    assert h15["model_cost_pct"] is None
    assert h15["edge_pct"] is None
    assert sc["book_pnl"] == 300.0
    text = format_scorecard_block(equity=1300.0, journal=j, sc=sc)
    assert "15m:n/a/n/a/stale/spy=—" in text


def test_scorecard_hero_sess_are_this_rth_not_leftover_8_26(monkeypatch):
    """A fixture 8/26 model session does not become sess. Hero is 8/28 RTH."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"model": "grok-4.6", "is_paper": True, "trading_mode": "paper"})(),
    )
    j = get_journal()
    now = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
    j.record_snapshot(
        account={"NetLiquidation": 35000.0, "DailyPnL": 0.0},
        ts="2026-08-26T14:00:00.000Z",
    )
    j.ensure_model_session(
        "grok-4.6", net_liquidation=35000.0, ts="2026-08-26T13:30:00.000Z"
    )
    j.record_model_usage(
        stage="act",
        input_tokens=10,
        output_tokens=10,
        cost_usd=49.07,
        ts="2026-08-26T15:00:00.000Z",
    )
    j.record_snapshot(
        account={"NetLiquidation": 35100.0, "DailyPnL": 10.0},
        ts="2026-08-28T13:35:00.000Z",
    )
    j.record_snapshot(
        account={"NetLiquidation": 35122.0, "DailyPnL": 22.0},
        ts="2026-08-28T16:00:00.000Z",
    )
    j.record_model_usage(
        stage="act",
        input_tokens=10,
        output_tokens=10,
        cost_usd=4.0,
        ts="2026-08-28T14:00:00.000Z",
    )
    sc = compute_scorecard(equity=35122.0, journal=j, now=now, spy={})
    assert sc["session"]["session_date"] == "2026-08-28"
    assert sc["session"]["kind"] == "rth"
    assert sc["session"]["startup_nl"] == 35100.0
    assert sc["session"]["end_nl"] == 35122.0
    assert sc["session"]["book_pnl"] == 22.0
    assert abs(sc["session"]["model_cost_usd"] - 4.0) < 1e-9
    assert sc["session"]["model_cost_usd"] != 49.07
    assert not str(sc["session"].get("started_at") or "").startswith("2026-08-26")
    marker = j.last_session_marker()
    assert marker and str(marker.get("ts") or "").startswith("2026-08-26")
    assert sc["startup_cash"] == 35000.0
    assert sc["book_pnl"] == 122.0
    assert "spy_return_pct" in sc["windows"]["15m"]
    assert sc["windows"]["15m"]["spy_return_pct"] is None
    assert sc["windows"]["inception"]["spy_return_pct"] is None
    assert sc["session"]["spy_return_pct"] is None
    text = format_scorecard_block(equity=35122.0, journal=j, sc=sc)
    assert "2026-08-28 RTH" in text
    assert "vsSPY=—" in text


def test_scorecard_spy_is_blank_or_real_never_invented():
    from datetime import datetime, timezone

    now = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
    j = get_journal()
    j.record_snapshot(
        account={"NetLiquidation": 1000.0, "DailyPnL": 0.0},
        ts="2026-08-28T13:35:00.000Z",
    )
    blank = compute_scorecard(equity=1010.0, journal=j, now=now, spy={})
    assert blank["spy_last"] is None
    assert blank["windows"]["1h"]["spy_return_pct"] is None
    assert blank["session"]["spy_return_pct"] is None
    last_only = compute_scorecard(
        equity=1010.0, journal=j, now=now, spy={"SPY": 500.0}
    )
    assert last_only["spy_last"] == 500.0
    assert last_only["windows"]["15m"]["spy_return_pct"] is None
    assert last_only["windows"]["1d"]["spy_return_pct"] is None
    assert last_only["windows"]["inception"]["spy_return_pct"] is None
    assert last_only["session"]["spy_return_pct"] is None
    real = compute_scorecard(
        equity=1010.0,
        journal=j,
        now=now,
        spy={"last": 510.0, "open": 500.0, "close": 505.0},
    )
    assert real["session"]["spy_return_pct"] == spy_return_pct(
        "rth", {"last": 510.0, "open": 500.0}, session=True
    )
    assert abs(real["session"]["spy_return_pct"] - 2.0) < 1e-9
    assert abs(real["windows"]["1d"]["spy_return_pct"] - ((510.0 / 505.0) - 1.0) * 100.0) < 1e-9
    assert real["windows"]["15m"]["spy_return_pct"] is None
    assert real["windows"]["1h"]["spy_return_pct"] is None
    assert real["windows"]["4h"]["spy_return_pct"] is None
    assert real["windows"]["1w"]["spy_return_pct"] is None
    assert real["windows"]["1m"]["spy_return_pct"] is None
    assert real["windows"]["inception"]["spy_return_pct"] is None
    # A last-only last_turn blob is not a window return.
    from_book = compute_scorecard(
        equity=1010.0,
        journal=j,
        now=now,
        spy={"ibkr_live_quotes": {"SPY": 500.0}},
    )
    assert from_book["spy_last"] == 500.0
    assert from_book["session"]["spy_return_pct"] is None
    text = format_scorecard_block(sc=real)
    assert "vsSPY=+2.00%" in text
    assert "1d:" in text and "spy=" in text
