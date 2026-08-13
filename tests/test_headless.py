"""Headless paper runner is the autonomous path (no approval print-and-exit)."""

from types import SimpleNamespace

from abcxauto.headless import format_cycle_digest, format_record, run_headless


def test_headless_refuses_live(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: SimpleNamespace(is_paper=False, xai_api_key="k"),
    )
    assert run_headless() == 2


def test_headless_refuses_missing_key(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: SimpleNamespace(is_paper=True, xai_api_key=""),
    )
    assert run_headless() == 2


def test_format_cycle_digest_hold():
    text = format_cycle_digest(
        {
            "cycle": 1,
            "stance": "idle",
            "strat": "hold",
            "thesis": "Flat and protected. No A-grade setup.",
            "rationale": "Idle with full cash; skip weak tape.",
            "equity": 1_000_000.0,
            "pnl": 0.0,
            "result": {"status": "ok"},
            "pace": {"sleep_s": 300, "tier": "idle"},
        }
    )
    assert "CYCLE 1  idle -> hold" in text
    assert "NL=1000000" in text
    assert "thesis:" in text
    assert "why:" in text
    assert "sleep 300s" in text


def test_format_record_skips_snapshots():
    assert format_record({"type": "monitor_snapshot", "msg": "x"}) is None


def test_format_record_error():
    line = format_record({"type": "error", "ts": "12:00:00 UTC", "msg": "IBKR down"})
    assert line is not None
    assert "ERROR" in line
    assert "IBKR down" in line


def test_format_record_log_not_error():
    line = format_record(
        {"type": "log", "ts": "12:00:00 UTC", "msg": "Portfolio monitor started (pro path)"}
    )
    assert line is not None
    assert "ERROR" not in line
    assert "LOG" in line or "Portfolio monitor" in line


def test_format_record_benign_error_type_is_log():
    line = format_record(
        {
            "type": "error",
            "ts": "12:00:00 UTC",
            "msg": "Portfolio monitor started (pro path)",
        }
    )
    assert line is not None
    assert "ERROR" not in line
    assert "LOG" in line


def test_format_cycle_digest_grokfolio_wait():
    text = format_cycle_digest(
        {
            "cycle": 2,
            "stance": "idle",
            "strat": "hold",
            "rationale": "grokfolio_wait: next slot in 3600s",
            "equity": 1_000_000.0,
            "pnl": 0.0,
            "result": {"status": "held", "note": "grokfolio_wait: next slot in 3600s"},
            "pace": {"sleep_s": 3600, "tier": "grokfolio", "reason": "grokfolio_schedule"},
            "judgment": {"focus": "schedule"},
        }
    )
    assert "GROKFOLIO wait" in text
    assert "next slot in 3600s" in text
    assert all(ord(c) < 128 for c in text)


def test_format_cycle_digest_grokfolio_run():
    text = format_cycle_digest(
        {
            "cycle": 3,
            "stance": "hunt",
            "strat": "market_bracket",
            "rationale": "grokfolio buy MSFT",
            "result": {
                "status": "ok",
                "kind": "daily",
                "holdings": [{"symbol": "MSFT"}, {"symbol": "AAPL"}],
                "grokfolio": [{"op": "buy"}, {"op": "buy"}],
            },
            "pace": {"sleep_s": 1800, "tier": "grokfolio", "reason": "grokfolio_daily_done"},
            "judgment": {"focus": "grokfolio daily"},
        }
    )
    assert "GROKFOLIO daily" in text
    assert "2 holdings" in text
    assert all(ord(c) < 128 for c in text)
