"""Headless paper runner is the autonomous path (no approval print-and-exit)."""

from pathlib import Path
from types import SimpleNamespace

from abcxauto.headless import apply_kill_switch, format_cycle_digest, format_record, run_headless


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
    from tests.conftest import assert_no_cycle_counter

    assert_no_cycle_counter(text)
    assert "idle -> hold" in text
    assert "CYCLE" not in text
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


def test_kill_switch_stops_engine_and_does_not_flatten():
    """Ctrl+C stops the agent + IBKR link. Positions stay at the broker."""
    calls: list[str] = []

    class _Engine:
        def stop_engine(self) -> None:
            calls.append("stop_engine")

        def panic(self) -> None:
            calls.append("panic")

        def flatten_all(self) -> None:
            calls.append("flatten_all")

    apply_kill_switch(_Engine())
    assert calls == ["stop_engine"]


def test_kill_switch_survives_stop_error():
    """A broken stop must not raise NameError (logger was missing) or flatten."""

    class _Boom:
        def stop_engine(self) -> None:
            raise RuntimeError("disconnect failed")

        def panic(self) -> None:
            raise AssertionError("panic must not run")

    apply_kill_switch(_Boom())


def test_headless_source_never_flattens_or_panics():
    src = Path(__file__).resolve().parents[1].joinpath("abcxauto", "headless.py").read_text(
        encoding="utf-8"
    )
    assert "flatten_all" not in src
    assert ".panic(" not in src
    assert "apply_kill_switch" in src
    assert "logger = logging.getLogger(__name__)" in src
