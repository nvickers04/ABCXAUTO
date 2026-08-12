"""Headless paper runner is the autonomous path (no approval print-and-exit)."""

from types import SimpleNamespace

from abcxauto.headless import run_headless


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
