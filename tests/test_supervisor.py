import io
import logging
import os
import threading
from datetime import datetime

import pytest

import abcxauto.supervisor as sup
from abcxauto.supervisor import (
    clear_operator_stop,
    live_pro_pids as _real_live_pro_pids,
    mark_operator_stop,
    operator_stopped,
    supervise,
    useful_hours,
)


@pytest.fixture(autouse=True)
def _no_foreign_pro(monkeypatch):
    """Unit tests must not see a leftover desk on the machine running pytest."""
    monkeypatch.setattr(sup, "live_pro_pids", lambda **_k: [])


class _Proc:
    def __init__(self, code: int) -> None:
        self._code = code

    def wait(self) -> int:
        return self._code


def test_orphan_flet_pids_keeps_a_live_parent():
    from abcxauto.supervisor import orphan_flet_pids

    rows = [
        {
            "Name": "flet.exe",
            "ProcessId": 11,
            "ParentProcessId": 99,
            "CommandLine": r"C:\flet\flet.exe C:\Users\nvick\ABCXAUTO\assets",
        },
        {
            "Name": "flet.exe",
            "ProcessId": 22,
            "ParentProcessId": 88,
            "CommandLine": r"C:\flet\flet.exe C:\Users\nvick\ABCXAUTO\assets",
        },
        {
            "Name": "flet.exe",
            "ProcessId": 33,
            "ParentProcessId": 77,
            "CommandLine": r"C:\flet\flet.exe C:\other\project\assets",
        },
    ]
    assert orphan_flet_pids(rows, repo=r"C:\Users\nvick\ABCXAUTO", pid_alive=lambda p: p == 99) == [22]


def test_operator_stop_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(tmp_path / "stop.json"))
    clear_operator_stop()
    assert operator_stopped() is False
    mark_operator_stop()
    assert operator_stopped() is True
    clear_operator_stop()
    assert operator_stopped() is False


def test_clean_exit_is_a_window_close_not_a_crash(monkeypatch):
    """Operator closed the window — no relaunch, even mid-RTH with TWS up."""
    launches = []

    def _popen(*_a, **_kw):
        launches.append(1)
        return _Proc(0)

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "useful_hours", lambda **_kw: True)
    monkeypatch.setattr(sup, "tws_listening", lambda *_a, **_kw: True)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    assert supervise() == 0
    assert len(launches) == 1


def test_crash_relaunches_during_useful_hours(monkeypatch):
    codes = [1, 0]
    launches = []

    def _popen(*_a, **_kw):
        launches.append(1)
        return _Proc(codes.pop(0))

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "useful_hours", lambda **_kw: True)
    monkeypatch.setattr(sup, "tws_listening", lambda *_a, **_kw: True)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    assert supervise() == 0
    assert len(launches) == 2


def test_child_console_is_persisted_and_still_echoed(tmp_path, monkeypatch, capsys):
    """A traceback that only reaches the child's console is evidence we must keep."""
    out = tmp_path / "desk.out"
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(out))
    crash = "RuntimeError: Control with ID 650 is not registered"
    sup.tee_child_output(io.StringIO(f"ABCXAUTO Pro entry=...\n\n{crash}\n"))

    for h in logging.getLogger("abcxauto.desk_out").handlers:
        h.flush()
    body = out.read_text(encoding="utf-8")
    assert crash in body
    assert "ABCXAUTO Pro entry=..." in body
    # Blank lines are dropped, and the operator still sees it live.
    assert "\n\n" not in body
    assert crash in capsys.readouterr().out


def test_supervise_pipes_the_child_console(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(tmp_path / "desk.out"))
    seen: dict = {}

    class _PipedProc(_Proc):
        def __init__(self) -> None:
            super().__init__(0)
            self.stdout = io.StringIO("child says hello\n")

    def _popen(*_a, **kw):
        seen.update(kw)
        return _PipedProc()

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    assert supervise() == 0
    assert seen.get("stdout") is sup.subprocess.PIPE
    assert seen.get("stderr") is sup.subprocess.STDOUT
    assert seen.get("text") is True


def test_why_the_desk_is_down_is_written_down(tmp_path, monkeypatch):
    """The supervisor's stay-down reason is the whole diagnosis; persist it."""
    out = tmp_path / "desk.out"
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(out))

    def _popen(*_a, **_kw):
        return _Proc(0)

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    assert supervise() == 0
    for h in logging.getLogger("abcxauto.desk_out").handlers:
        h.flush()
    body = out.read_text(encoding="utf-8")
    assert "clean exit" in body


def test_supervisor_attaches_the_file_log_itself(tmp_path, monkeypatch):
    """The child called setup_file_logging(); the supervisor never did, so its own
    warnings existed nowhere durable."""
    monkeypatch.setenv("ABCXAUTO_LOG_PATH", str(tmp_path / "app.log"))
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(tmp_path / "desk.out"))
    calls: list[int] = []
    import abcxauto.config as cfgmod

    monkeypatch.setattr(cfgmod, "setup_file_logging", lambda **_kw: calls.append(1))
    monkeypatch.setattr(sup.subprocess, "Popen", lambda *_a, **_kw: _Proc(0))
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    assert supervise() == 0
    assert calls == [1]


def test_child_console_is_not_written_twice(tmp_path, monkeypatch):
    """The tee thread and the lifecycle notes race to attach the handler."""
    out = tmp_path / "desk.out"
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(out))
    lg = logging.getLogger("abcxauto.desk_out")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()

    ready = threading.Barrier(4)

    def _attach() -> None:
        ready.wait()
        sup._desk_out_logger()

    threads = [threading.Thread(target=_attach) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(lg.handlers) == 1
    sup.note("supervisor: only once please")
    for h in lg.handlers:
        h.flush()
    body = out.read_text(encoding="utf-8")
    assert body.count("only once please") == 1


def test_desk_out_never_lands_in_app_log():
    """The child console is its own file; app.log stays the structured record."""
    lg = sup._desk_out_logger()
    assert lg.propagate is False


def test_useful_hours_rth_and_weekend():
    rth = datetime(2026, 8, 17, 10, 0)  # Monday
    assert useful_hours(now=rth) is True
    early = datetime(2026, 8, 17, 7, 0)
    assert useful_hours(now=early) is False
    sat = datetime(2026, 8, 15, 10, 0)
    assert useful_hours(now=sat) is False


def test_live_pro_pids_skips_self_and_does_not_signal(monkeypatch):
    """The scan is how we see a lockless _start_pro. It must not be a kill list."""
    import psutil

    class _P:
        def __init__(self, pid: int, cmd: list[str]) -> None:
            self.info = {"pid": pid, "cmdline": cmd}

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _attrs: [
            _P(os.getpid(), ["python", "-m", "abcxauto"]),
            _P(4242, ["python", "logs/_start_pro.py"]),
            _P(8, ["python", "scripts/cleanup_pro.py"]),
        ],
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(sup.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert _real_live_pro_pids() == [4242]
    assert killed == []


def test_cmdline_is_pro_matches_launchers_not_cleanup():
    assert sup._cmdline_is_pro(["python", "-m", "abcxauto"]) is True
    assert sup._cmdline_is_pro(r"C:\Users\nvick\ABCXAUTO\logs\_start_pro.py") is True
    assert sup._cmdline_is_pro(["python", "abcxauto/pro_desktop.py"]) is True
    assert sup._cmdline_is_pro(["python", "scripts/cleanup_pro.py"]) is False
    assert sup._cmdline_is_pro(["python", "-m", "abcxauto", "--cleanup"]) is False
    assert sup._cmdline_is_pro(["pytest", "tests/test_supervisor.py"]) is False


def test_supervise_does_not_spawn_over_a_live_pro(monkeypatch, tmp_path):
    """_start_pro (or a leftover child) already holds 7497. A second Start is 326."""
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(tmp_path / "desk.out"))
    launches: list[int] = []
    killed: list[tuple[int, int]] = []

    def _popen(*_a, **_kw):
        launches.append(1)
        return _Proc(0)

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "live_pro_pids", lambda **_k: [4242])
    monkeypatch.setattr(sup.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(sup, "useful_hours", lambda **_kw: True)
    monkeypatch.setattr(sup, "tws_listening", lambda *_a, **_kw: True)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    assert supervise() == 0
    assert launches == []
    assert killed == []
    for h in logging.getLogger("abcxauto.desk_out").handlers:
        h.flush()
    body = (tmp_path / "desk.out").read_text(encoding="utf-8")
    assert "already up" in body
    assert "4242" in body
    assert "flatten" not in body.lower()


def test_supervise_does_not_relaunch_into_a_live_pro(monkeypatch, tmp_path):
    """Child lost Error 326; the other Pro still owns the client id — stay down."""
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(tmp_path / "desk.out"))
    launches: list[int] = []
    live: list[int] = []

    def _popen(*_a, **_kw):
        launches.append(1)
        live[:] = [99]
        return _Proc(1)

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "live_pro_pids", lambda **_k: list(live))
    monkeypatch.setattr(sup, "useful_hours", lambda **_kw: True)
    monkeypatch.setattr(sup, "tws_listening", lambda *_a, **_kw: True)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    slept: list[float] = []
    monkeypatch.setattr(sup.time, "sleep", slept.append)
    assert supervise() == 1
    assert launches == [1]
    assert slept == []


def test_foreign_desk_pid_uses_lock_owner_without_killing(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(tmp_path / "desk.lock"))
    (tmp_path / "desk.lock").write_text('{"pid": 7}', encoding="utf-8")
    monkeypatch.setattr(sup, "_pid_alive", lambda pid: pid == 7)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(sup.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert sup.foreign_desk_pid() == 7
    assert killed == []


def test_start_over_a_live_pro_is_not_flatten(monkeypatch):
    """Second Start must not cleanup, panic, or flatten the book that is already live."""
    calls: list[str] = []

    def _popen(*_a, **_kw):
        calls.append("popen")
        return _Proc(0)

    monkeypatch.setattr(sup.subprocess, "Popen", _popen)
    monkeypatch.setattr(sup, "live_pro_pids", lambda **_k: [77])
    monkeypatch.setattr(sup, "release_desk_lock", lambda: calls.append("release"))
    monkeypatch.setattr(sup, "mark_operator_stop", lambda: calls.append("stop"))
    assert supervise() == 0
    assert calls == []
