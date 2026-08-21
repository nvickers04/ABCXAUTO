import io
import logging
import threading
from datetime import datetime

import abcxauto.supervisor as sup
from abcxauto.supervisor import (
    clear_operator_stop,
    mark_operator_stop,
    operator_stopped,
    supervise,
    useful_hours,
)


class _Proc:
    def __init__(self, code: int) -> None:
        self._code = code

    def wait(self) -> int:
        return self._code


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
