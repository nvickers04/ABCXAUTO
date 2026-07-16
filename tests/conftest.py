"""Shared test helpers."""

from pathlib import Path

import pytest

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")

# Static safety defaults from cycle.TWEAKS — restore after tests that clear TWEAKS.
STATIC_TWEAKS = {
    "max_risk_pct": 0.5,
}


class _Cfg:
    xai_api_key = "test-key"
    cycle_sleep_s = 0.05
    grok_min_interval_s = 0.0
    signal_only = False
    monitor_enabled = False


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path):
    """Keep the trade journal out of the real journal.db during tests."""
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"))
    yield
    reset_journal(path=str(tmp_path / "journal.db"))


@pytest.fixture(autouse=True)
def _clear_risk_overrides(tmp_path, monkeypatch):
    """Risk overrides must not leak; use a temp settings file per test."""
    from abcxauto.config import (
        clear_risk_settings,
        clear_runtime_overrides,
        get_config,
        load_risk_settings,
    )

    path = tmp_path / "risk_settings.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    clear_runtime_overrides()
    get_config.cache_clear()
    yield
    clear_risk_settings(path=path)
    clear_runtime_overrides()
    get_config.cache_clear()


@pytest.fixture(autouse=True)
def _restore_tweaks():
    """Ensure static safety TWEAKS survive tests that clear the dict."""
    from abcxauto.cycle import TWEAKS

    before = dict(TWEAKS)
    yield
    TWEAKS.clear()
    TWEAKS.update(STATIC_TWEAKS)
    TWEAKS.update({k: v for k, v in before.items() if k in STATIC_TWEAKS or k in TWEAKS})
    # Prefer known static defaults after every test
    for k, v in STATIC_TWEAKS.items():
        TWEAKS[k] = v


@pytest.fixture(autouse=True)
def _stub_opportunity_scan(monkeypatch):
    """Avoid live MDA candle fan-out during unit tests."""

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr(
        "abcxauto.opportunity_scan.scan_opportunities",
        _empty,
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.scan_opportunities",
        _empty,
    )


@pytest.fixture(autouse=True)
def _isolate_open_risk_and_structure_files(tmp_path, monkeypatch):
    """Keep trade plan / structure lessons out of the live workspace files."""
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "active_trade_plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat_book_streak.json"))
    monkeypatch.setenv(
        "ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "structure_events.jsonl")
    )
    monkeypatch.setenv(
        "ABCXAUTO_STRUCTURE_VOCAB_PATH", str(tmp_path / "structure_vocab.json")
    )
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle_streak.json"))
