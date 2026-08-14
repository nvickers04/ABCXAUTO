"""Cursor launch prefers Pro desktop + autostart; pytest stays quiet."""

import os

from abcxauto.cursor_env import prefer_desktop, running_in_cursor, should_autostart


def test_running_in_cursor_explicit(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IN_CURSOR", "1")
    monkeypatch.delenv("ABCXAUTO_NOT_CURSOR", raising=False)
    assert running_in_cursor() is True


def test_running_in_cursor_trace(monkeypatch):
    monkeypatch.delenv("ABCXAUTO_IN_CURSOR", raising=False)
    monkeypatch.delenv("ABCXAUTO_NOT_CURSOR", raising=False)
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc")
    assert running_in_cursor() is True


def test_not_cursor_wins(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IN_CURSOR", "1")
    monkeypatch.setenv("ABCXAUTO_NOT_CURSOR", "1")
    assert running_in_cursor() is False
    assert prefer_desktop() is False


def test_prefer_desktop_skipped_in_pytest(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IN_CURSOR", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_cursor_env.py")
    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    assert prefer_desktop() is False
    assert should_autostart() is False


def test_force_headless(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IN_CURSOR", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ABCXAUTO_FORCE_HEADLESS", "1")
    assert prefer_desktop() is False


def test_autostart_when_flagged(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_AUTOSTART", "1")
    monkeypatch.setenv("ABCXAUTO_NOT_CURSOR", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ABCXAUTO_UI_PROBE", raising=False)
    monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    assert should_autostart() is True


def test_headless_flag_still_opens_pro(monkeypatch):
    import sys

    import abcxauto.__main__ as m

    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    monkeypatch.setenv("ABCXAUTO_LAUNCH_PROBE", "1")
    monkeypatch.setattr(sys, "argv", ["abcxauto", "--headless"])
    monkeypatch.setattr(m, "_cleanup", lambda **_k: 0)
    called: dict[str, bool] = {}
    monkeypatch.setattr(
        "abcxauto.pro_desktop.run_app",
        lambda: called.__setitem__("pro", True),
    )
    monkeypatch.setattr(
        "abcxauto.headless.run_headless",
        lambda: called.__setitem__("headless", True) or 0,
    )
    m.main()
    assert called.get("pro") is True
    assert "headless" not in called


def test_force_headless_skips_pro(monkeypatch):
    import sys

    import abcxauto.__main__ as m
    import pytest

    monkeypatch.setenv("ABCXAUTO_FORCE_HEADLESS", "1")
    monkeypatch.setenv("ABCXAUTO_LAUNCH_PROBE", "1")
    monkeypatch.setattr(sys, "argv", ["abcxauto", "--headless"])
    monkeypatch.setattr(m, "_cleanup", lambda **_k: 0)
    called: dict[str, bool] = {}
    monkeypatch.setattr(
        "abcxauto.pro_desktop.run_app",
        lambda: called.__setitem__("pro", True),
    )
    monkeypatch.setattr(
        "abcxauto.headless.run_headless",
        lambda: called.__setitem__("headless", True) or 0,
    )
    with pytest.raises(SystemExit):
        m.main()
    assert called.get("headless") is True
    assert "pro" not in called
