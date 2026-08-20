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


def test_supervised_start_does_not_cleanup(monkeypatch):
    """python -m abcxauto child must not kill itself before Pro opens."""
    import sys

    import abcxauto.__main__ as m

    monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    monkeypatch.setenv("ABCXAUTO_SUPERVISED", "1")
    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    cleanups: list[dict] = []
    monkeypatch.setattr(m, "_cleanup", lambda **k: cleanups.append(k) or 0)
    monkeypatch.setattr("abcxauto.think_stream.begin_run", lambda: None)
    called: dict[str, bool] = {}
    monkeypatch.setattr(
        "abcxauto.pro_desktop.run_app",
        lambda: called.__setitem__("pro", True),
    )
    m.main()
    assert called.get("pro") is True
    assert cleanups == []


def test_supervisor_wrapper_does_not_cleanup(monkeypatch):
    """Outer python -m abcxauto must not kill the process that just launched it."""
    import sys

    import pytest

    import abcxauto.__main__ as m

    monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    monkeypatch.delenv("ABCXAUTO_SUPERVISED", raising=False)
    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    cleanups: list[dict] = []
    monkeypatch.setattr(m, "_cleanup", lambda **k: cleanups.append(k) or 0)
    monkeypatch.setattr("abcxauto.supervisor.clear_operator_stop", lambda: None)
    monkeypatch.setattr("abcxauto.supervisor.supervise", lambda: 0)
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 0
    assert cleanups == []


def test_cleanup_flag_still_runs_cleanup(monkeypatch):
    import sys

    import pytest

    import abcxauto.__main__ as m

    monkeypatch.setattr(sys, "argv", ["abcxauto", "--cleanup"])
    called: dict[str, object] = {}

    def _fake_cleanup(**k):
        called["cleanup"] = k
        return 0

    monkeypatch.setattr(m, "_cleanup", _fake_cleanup)
    monkeypatch.setattr(
        "abcxauto.supervisor.mark_operator_stop",
        lambda: called.setdefault("stop", True),
    )
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 0
    assert called.get("stop") is True
    assert "cleanup" in called


def test_start_pro_script_autostarts_pro(monkeypatch):
    """Supported start: logs/_start_pro.py with ABCXAUTO_AUTOSTART=1."""
    import runpy
    from pathlib import Path

    called: dict[str, bool] = {}
    monkeypatch.setattr(
        "abcxauto.think_stream.begin_run",
        lambda: called.__setitem__("think", True),
    )
    monkeypatch.setattr(
        "abcxauto.pro_desktop.run_app",
        lambda: called.__setitem__("pro", True),
    )
    monkeypatch.delenv("ABCXAUTO_AUTOSTART", raising=False)
    path = Path(__file__).resolve().parents[1] / "logs" / "_start_pro.py"
    src = path.read_text(encoding="utf-8-sig")
    assert "_cleanup" not in src
    assert "cleanup_pro" not in src
    runpy.run_path(str(path), run_name="abcxauto_start_pro")
    assert os.environ.get("ABCXAUTO_AUTOSTART") == "1"
    assert called.get("think") is True
    assert called.get("pro") is True
