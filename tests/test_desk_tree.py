"""Start and stop are one Pro/flet tree. Never a second window. Never flatten."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import abcxauto.supervisor as sup
from abcxauto.supervisor import supervise


class _Proc:
    def __init__(self, code: int) -> None:
        self._code = code
        self.pid = 501
        self.stdout = None

    def wait(self) -> int:
        return self._code


def test_reap_kills_leftover_start_pro_and_its_flet(monkeypatch):
    """Bounce leftover: python 30744 + child flet 27184, then zero leftovers."""
    killed: list[int] = []
    monkeypatch.setattr(sup, "leftover_pro_pids", lambda **_k: [30744])
    monkeypatch.setattr(
        sup,
        "process_tree_pids",
        lambda root, exclude=None: [27184, 30744] if root == 30744 else [root],
    )
    monkeypatch.setattr(sup, "kill_pid", lambda pid, **_k: killed.append(pid) or True)
    monkeypatch.setattr(sup, "list_flet_rows", lambda: [])
    monkeypatch.setattr(sup, "protected_pids", lambda extra=None: {os.getpid()})
    assert sup.reap_leftover_desk() == [27184, 30744]
    assert killed == [27184, 30744]


def test_reap_kills_orphan_flet_when_python_is_already_dead(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(sup, "leftover_pro_pids", lambda **_k: [])
    monkeypatch.setattr(sup, "kill_pid", lambda pid, **_k: killed.append(pid) or True)
    monkeypatch.setattr(
        sup,
        "list_flet_rows",
        lambda: [
            {
                "Name": "flet.exe",
                "ProcessId": 27184,
                "ParentProcessId": 30744,
                "CommandLine": r"C:\flet\flet.exe C:\Users\nvick\ABCXAUTO\assets",
            }
        ],
    )
    monkeypatch.setattr(sup, "_REPO", Path(r"C:\Users\nvick\ABCXAUTO"))
    monkeypatch.setattr(sup, "_pid_alive", lambda _pid: False)
    assert 27184 in sup.reap_leftover_desk()
    assert killed == [27184]


def test_reap_does_not_leave_a_second_flet(monkeypatch):
    """Start reaps the leftover window before the new pair is allowed to spawn."""
    leftover_flet = {27184}
    monkeypatch.setattr(sup, "leftover_pro_pids", lambda **_k: [])
    monkeypatch.setattr(
        sup,
        "kill_pid",
        lambda pid, **_k: leftover_flet.discard(pid) or True,
    )
    monkeypatch.setattr(
        sup,
        "list_flet_rows",
        lambda: [
            {
                "Name": "flet.exe",
                "ProcessId": 27184,
                "ParentProcessId": 30744,
                "CommandLine": r"C:\flet\flet.exe C:\Users\nvick\ABCXAUTO\assets",
            }
        ],
    )
    monkeypatch.setattr(sup, "_REPO", Path(r"C:\Users\nvick\ABCXAUTO"))
    monkeypatch.setattr(sup, "_pid_alive", lambda _pid: False)
    sup.prepare_desk_start()
    assert leftover_flet == set()


def test_prepare_clears_stale_lock_and_operator_stop(tmp_path, monkeypatch):
    """Dead pid 36604 (Aug 25) and stop:true at 9:48 CT cannot pin a new run."""
    lock = tmp_path / "desk.lock"
    stop = tmp_path / "operator_stop.json"
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))
    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(stop))
    lock.write_text(
        json.dumps({"pid": 36604, "ts": "2026-08-25T12:00:00"}), encoding="utf-8"
    )
    stop.write_text(
        json.dumps({"stop": True, "ts": "2026-08-26T09:48:00"}), encoding="utf-8"
    )
    monkeypatch.setattr(sup, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(sup, "reap_leftover_desk", lambda **_k: [])
    assert sup.operator_stopped() is True
    assert lock.is_file()
    sup.prepare_desk_start()
    assert sup.operator_stopped() is False
    assert not lock.is_file()
    assert sup.claim_desk_lock() is True
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_stale_lock_for_a_live_owner_still_blocks(tmp_path, monkeypatch):
    lock = tmp_path / "desk.lock"
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))
    lock.write_text(json.dumps({"pid": 7}), encoding="utf-8")
    monkeypatch.setattr(sup, "_pid_alive", lambda pid: pid == 7)
    monkeypatch.setattr(sup, "reap_leftover_desk", lambda **_k: [])
    assert sup.clear_stale_desk_lock() is False
    assert lock.is_file()
    assert sup.claim_desk_lock() is False


def test_stop_desk_kills_pro_and_flet_and_drops_lock(tmp_path, monkeypatch):
    """Stop leaves zero Pro/flet. Night leftover 25336 + 22688 cannot survive."""
    lock = tmp_path / "desk.lock"
    stop = tmp_path / "operator_stop.json"
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))
    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(stop))
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": "now"}), encoding="utf-8")
    monkeypatch.setattr(
        sup, "reap_leftover_desk", lambda **_k: [25336, 22688]
    )
    monkeypatch.setattr(sup, "kill_descendant_flet", lambda **_k: [])
    out = sup.stop_desk()
    assert out == [25336, 22688]
    assert sup.operator_stopped() is True
    assert not lock.is_file()


def test_leftover_pro_pids_skip_tws_self_and_our_tree(monkeypatch):
    me = os.getpid()
    monkeypatch.setattr(sup, "live_pro_pids", lambda **_k: [me, 88, 99, 42])
    monkeypatch.setattr(sup, "_pid_is_tws", lambda pid: pid == 88)
    monkeypatch.setattr(sup, "_is_in_tree", lambda root, target: root == 99)
    monkeypatch.setattr(sup, "protected_pids", lambda extra=None: {me})
    assert sup.leftover_pro_pids() == [42]


def test_process_tree_pids_skips_tws(monkeypatch):
    class _Child:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class _P:
        def children(self, recursive=True):
            return [_Child(9), _Child(11)]

    import psutil

    monkeypatch.setattr(sup, "protected_pids", lambda extra=None: set())
    monkeypatch.setattr(sup, "_pid_is_tws", lambda pid: pid == 9)
    monkeypatch.setattr(psutil, "Process", lambda _pid: _P())
    tree = sup.process_tree_pids(5)
    assert 9 not in tree
    assert 11 in tree
    assert 5 in tree


def test_start_pro_source_reaps_before_run_app():
    from abcxauto.cursor_env import START_PRO_SOURCE

    assert "prepare_desk_start" in START_PRO_SOURCE
    assert START_PRO_SOURCE.index("prepare_desk_start") < START_PRO_SOURCE.index("run_app")
    assert "cleanup_pro" not in START_PRO_SOURCE
    assert "_cleanup" not in START_PRO_SOURCE
    assert "flatten" not in START_PRO_SOURCE.lower()


def test_main_start_reaps_before_supervise(monkeypatch):
    import abcxauto.__main__ as m

    order: list[str] = []
    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    monkeypatch.delenv("ABCXAUTO_SUPERVISED", raising=False)
    monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    monkeypatch.delenv("ABCXAUTO_FORCE_HEADLESS", raising=False)
    monkeypatch.setattr("abcxauto.supervisor.ancestor_holds_desk", lambda: False)
    monkeypatch.setattr(
        "abcxauto.supervisor.prepare_desk_start",
        lambda **_k: order.append("prepare") or [],
    )
    monkeypatch.setattr(
        "abcxauto.supervisor.claim_desk_lock",
        lambda: order.append("claim") or True,
    )
    monkeypatch.setattr(
        "abcxauto.supervisor.supervise",
        lambda: order.append("supervise") or 0,
    )
    monkeypatch.setattr(
        "abcxauto.supervisor.release_desk_lock",
        lambda **_k: order.append("release"),
    )
    monkeypatch.setattr(m, "_ensure_start_pro_script", lambda: None)
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 0
    assert order[:3] == ["prepare", "claim", "supervise"]


def test_flet_reentry_does_not_reap_the_live_desk(monkeypatch):
    import abcxauto.__main__ as m

    calls: list[str] = []
    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    monkeypatch.delenv("ABCXAUTO_SUPERVISED", raising=False)
    monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    monkeypatch.setattr("abcxauto.supervisor.ancestor_holds_desk", lambda: True)
    monkeypatch.setattr("abcxauto.supervisor.desk_owner_pid", lambda: 99)
    monkeypatch.setattr(
        "abcxauto.supervisor.prepare_desk_start",
        lambda **_k: calls.append("prepare"),
    )
    monkeypatch.setattr(
        "abcxauto.supervisor.supervise",
        lambda: calls.append("supervise") or 0,
    )
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 0
    assert calls == []


def test_main_cleanup_stops_the_tree(monkeypatch):
    import abcxauto.__main__ as m

    order: list[str] = []
    monkeypatch.setattr(sys, "argv", ["abcxauto", "--cleanup"])
    monkeypatch.setattr(
        "abcxauto.supervisor.stop_desk", lambda **_k: order.append("stop") or []
    )
    monkeypatch.setattr(m, "_cleanup", lambda **_k: order.append("cleanup") or 0)
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 0
    assert order == ["stop", "cleanup"]


def test_supervise_reaps_before_and_after_so_stop_leaves_zero_flet(monkeypatch):
    reaps: list[str] = []

    monkeypatch.setattr(
        sup, "reap_leftover_desk", lambda **_k: reaps.append("reap") or []
    )
    monkeypatch.setattr(
        sup, "kill_descendant_flet", lambda **_k: reaps.append("flet") or []
    )
    monkeypatch.setattr(sup.subprocess, "Popen", lambda *_a, **_kw: _Proc(0))
    monkeypatch.setattr(sup, "foreign_desk_pid", lambda **_k: 0)
    monkeypatch.setattr(sup, "operator_stopped", lambda: False)
    assert supervise() == 0
    assert reaps[0] == "reap"
    assert "flet" in reaps
    assert reaps.count("reap") >= 2


def test_tws_probe_stays_paper_7497():
    import inspect

    assert inspect.signature(sup.tws_listening).parameters["port"].default == 7497


def test_tree_helpers_never_flatten_or_open_live():
    src = Path(sup.__file__).read_text(encoding="utf-8")
    assert "flatten_all" not in src
    assert "7496" not in src
    main_src = Path(__file__).resolve().parents[1] / "abcxauto" / "__main__.py"
    assert "flatten_all" not in main_src.read_text(encoding="utf-8")
