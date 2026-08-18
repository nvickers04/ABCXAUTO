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


def test_useful_hours_rth_and_weekend():
    rth = datetime(2026, 8, 17, 10, 0)  # Monday
    assert useful_hours(now=rth) is True
    early = datetime(2026, 8, 17, 7, 0)
    assert useful_hours(now=early) is False
    sat = datetime(2026, 8, 15, 10, 0)
    assert useful_hours(now=sat) is False
