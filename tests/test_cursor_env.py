"""Cursor launch prefers Pro desktop + autostart; pytest stays quiet."""

import os

from abcxauto.cursor_env import running_in_cursor, should_autostart


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


def test_autostart_skipped_in_pytest(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IN_CURSOR", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_cursor_env.py")
    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    assert should_autostart() is False


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


def test_start_pro_script_autostarts_pro(monkeypatch, tmp_path):
    """Supported start: the generated _start_pro.py with ABCXAUTO_AUTOSTART=1."""
    import runpy

    from abcxauto.cursor_env import write_start_pro_script

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
    path = write_start_pro_script(tmp_path / "logs" / "_start_pro.py")
    src = path.read_text(encoding="utf-8-sig")
    assert "_cleanup" not in src
    assert "cleanup_pro" not in src
    runpy.run_path(str(path), run_name="abcxauto_start_pro")
    assert os.environ.get("ABCXAUTO_AUTOSTART") == "1"
    assert called.get("think") is True
    assert called.get("pro") is True


def test_start_pro_path_default_and_override(monkeypatch, tmp_path):
    """Default stays logs/_start_pro.py; the env form redirects the writer."""
    from abcxauto.cursor_env import START_PRO_PATH, start_pro_path, write_start_pro_script

    monkeypatch.delenv("ABCXAUTO_START_PRO_PATH", raising=False)
    assert start_pro_path() == START_PRO_PATH
    assert START_PRO_PATH.parent.name == "logs"
    assert START_PRO_PATH.name == "_start_pro.py"

    target = tmp_path / "elsewhere" / "_start_pro.py"
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))
    assert start_pro_path() == target
    assert write_start_pro_script() == target
    assert "ABCXAUTO_AUTOSTART" in target.read_text(encoding="utf-8")


# ------------------------------------------------- launcher written on startup


def _arm_launch(monkeypatch, *, supervised=False, probe=False, claimed=True):
    """Point main() at a chosen leg of the supervisor -> child -> Flet chain.

    Nothing is launched: supervise() and run_app() are stubs.
    """
    import sys

    import abcxauto.__main__ as m
    from abcxauto import supervisor

    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    monkeypatch.setattr(m, "_cleanup", lambda **_k: 0)
    if supervised:
        monkeypatch.setenv("ABCXAUTO_SUPERVISED", "1")
    else:
        monkeypatch.delenv("ABCXAUTO_SUPERVISED", raising=False)
    if probe:
        monkeypatch.setenv("ABCXAUTO_LAUNCH_PROBE", "1")
    else:
        monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    if not claimed:
        monkeypatch.setattr(supervisor, "claim_desk_lock", lambda: False)
        monkeypatch.setattr(supervisor, "desk_owner_pid", lambda: 4242)
    calls: list[str] = []
    monkeypatch.setattr(supervisor, "supervise", lambda: calls.append("supervise") or 0)
    monkeypatch.setattr("abcxauto.think_stream.begin_run", lambda: None)
    monkeypatch.setattr("abcxauto.pro_desktop.run_app", lambda: calls.append("run_app"))
    monkeypatch.setattr("abcxauto.cursor_env.should_autostart", lambda: False)
    return m, calls


def test_launch_writes_start_pro_script_when_missing(monkeypatch, tmp_path):
    """logs/ is gitignored, so a fresh clone starts with no launcher at all."""
    import pytest

    from abcxauto.cursor_env import START_PRO_SOURCE

    target = tmp_path / "logs" / "_start_pro.py"
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))
    assert not target.exists()

    m, calls = _arm_launch(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        m.main()

    assert exc.value.code == 0
    assert calls == ["supervise"]
    assert target.read_text(encoding="utf-8") == START_PRO_SOURCE


def test_launch_does_not_clobber_an_edited_start_pro_script(monkeypatch, tmp_path):
    """The operator may have edited theirs; startup must not rewrite it."""
    import pytest

    target = tmp_path / "logs" / "_start_pro.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"# operator's own launcher\nprint('mine')\n"
    target.write_bytes(sentinel)
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))

    m, calls = _arm_launch(monkeypatch)
    with pytest.raises(SystemExit):
        m.main()

    assert calls == ["supervise"]  # the launch that would have written it ran
    assert target.read_bytes() == sentinel


def test_start_pro_script_written_once_per_launch(monkeypatch, tmp_path):
    """Supervisor writes it; the supervised child and the Flet re-entry do not."""
    import pytest

    target = tmp_path / "logs" / "_start_pro.py"
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))
    writes: list[str] = []

    def _count(path=None):
        writes.append(str(path))
        return target

    monkeypatch.setattr("abcxauto.cursor_env.write_start_pro_script", _count)

    m, _calls = _arm_launch(monkeypatch)
    with pytest.raises(SystemExit):
        m.main()
    assert len(writes) == 1

    # The child inherits ABCXAUTO_SUPERVISED and never enters the supervisor block.
    m, child_calls = _arm_launch(monkeypatch, supervised=True)
    m.main()
    assert child_calls == ["run_app"]
    assert len(writes) == 1

    # Flet re-enters __main__ without ABCXAUTO_SUPERVISED and bounces off the lock.
    m, _calls = _arm_launch(monkeypatch, claimed=False)
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 0
    assert len(writes) == 1


def test_cleanup_does_not_write_the_start_pro_script(monkeypatch, tmp_path):
    """--cleanup is a stop. Nothing about it is a launch."""
    import sys

    import pytest

    import abcxauto.__main__ as m
    from abcxauto import supervisor

    target = tmp_path / "logs" / "_start_pro.py"
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))
    monkeypatch.setattr(sys, "argv", ["abcxauto", "--cleanup"])
    monkeypatch.setattr(m, "_cleanup", lambda **_k: 0)
    monkeypatch.setattr(supervisor, "mark_operator_stop", lambda: None)

    with pytest.raises(SystemExit) as exc:
        m.main()

    assert exc.value.code == 0
    assert not target.exists()


def test_launch_start_pro_write_honours_the_env_override(monkeypatch, tmp_path):
    """ABCXAUTO_START_PRO_PATH redirects the startup write off the repo path."""
    import pytest

    from abcxauto.cursor_env import START_PRO_PATH, START_PRO_SOURCE

    target = tmp_path / "elsewhere" / "launch_me.py"
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))
    before = START_PRO_PATH.read_bytes() if START_PRO_PATH.is_file() else None

    m, _calls = _arm_launch(monkeypatch)
    with pytest.raises(SystemExit):
        m.main()

    assert target.read_text(encoding="utf-8") == START_PRO_SOURCE
    after = START_PRO_PATH.read_bytes() if START_PRO_PATH.is_file() else None
    assert after == before


def test_start_pro_write_failure_does_not_block_the_launch(monkeypatch, tmp_path):
    """A read-only logs/ costs a debug line, not the desk."""
    import pytest

    target = tmp_path / "logs" / "_start_pro.py"
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(target))
    lock = tmp_path / "desk.lock"
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))

    def _boom(_path=None):
        raise PermissionError("logs/ is read-only")

    monkeypatch.setattr("abcxauto.cursor_env.write_start_pro_script", _boom)

    m, calls = _arm_launch(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        m.main()

    assert exc.value.code == 0
    assert calls == ["supervise"]
    assert not target.exists()
    assert not lock.is_file()  # the desk lock still round-tripped
