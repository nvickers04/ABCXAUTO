"""cleanup_pro must not kill the live paper Pro on 7497 or flatten."""

from __future__ import annotations

import os
from pathlib import Path

import scripts.cleanup_pro as cleanup


def test_cmdline_is_pro_matches_launchers_not_cleanup():
    assert cleanup.cmdline_is_pro(["python", "-m", "abcxauto"]) is True
    assert cleanup.cmdline_is_pro(["python", "-mabcxauto"]) is True
    assert cleanup.cmdline_is_pro(r"C:\Users\nvick\ABCXAUTO\logs\_start_pro.py") is True
    assert cleanup.cmdline_is_pro(["python", "abcxauto/pro_desktop.py"]) is True
    assert cleanup.cmdline_is_pro(["python", "scripts/cleanup_pro.py"]) is False
    assert cleanup.cmdline_is_pro(["python", "-m", "abcxauto", "--cleanup"]) is False
    assert cleanup.cmdline_is_pro(["pytest", "tests/test_cleanup_pro.py"]) is False


def test_spare_live_paper_pids_includes_start_pro_and_children(monkeypatch):
    class _Child:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class _P:
        def __init__(self, pid: int, cmd: list[str]) -> None:
            self.info = {"pid": pid, "cmdline": cmd}

        def children(self, recursive=False):
            return [_Child(88)] if self.info["pid"] == 4242 else []

    import psutil

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _attrs: [
            _P(os.getpid(), ["python", "-m", "abcxauto"]),
            _P(4242, ["python", "logs/_start_pro.py"]),
            _P(8, ["python", "scripts/cleanup_pro.py"]),
        ],
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: _P(int(pid), []))
    spared, scan_ok = cleanup.spare_live_paper_pids()
    assert scan_ok is True
    assert 4242 in spared
    assert 88 in spared
    assert 8 not in spared
    assert os.getpid() not in spared


def test_spare_live_paper_pids_uses_desk_lock(tmp_path, monkeypatch):
    lock = tmp_path / "desk.lock"
    lock.write_text('{"pid": 77}', encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))

    class _P:
        def __init__(self, pid: int, cmd: list[str]) -> None:
            self.info = {"pid": pid, "cmdline": cmd}

        def children(self, recursive=False):
            return []

    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: _P(int(pid), []))
    spared, scan_ok = cleanup.spare_live_paper_pids()
    assert scan_ok is True
    assert 77 in spared


def test_kill_policy_excludes_live_paper_pro(monkeypatch):
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: ({4242, 77}, True))
    exclude, kill_python, kill_flet, kill_title = cleanup.kill_policy()
    assert 4242 in exclude
    assert 77 in exclude
    assert os.getpid() in exclude
    assert kill_python is True
    assert kill_flet is True
    assert kill_title is True


def test_kill_policy_fails_closed_when_scan_misses(monkeypatch):
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: (set(), False))
    _exclude, kill_python, kill_flet, kill_title = cleanup.kill_policy(python_targets=True)
    assert kill_python is False
    assert kill_flet is False
    assert kill_title is False


def test_kill_stale_puts_live_paper_pids_in_powershell_exclude(monkeypatch):
    seen: dict[str, str] = {}
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: ({4242, 77}, True))
    monkeypatch.setattr(cleanup, "_ps", lambda script: seen.setdefault("script", script) or "killed none")
    cleanup.kill_stale()
    script = seen["script"]
    assert "4242" in script
    assert "77" in script
    assert "_start_pro.py" in script or "_start_pro\\.py" in script
    assert "(_pro_)" not in script
    assert "flatten" not in script.lower()


def test_cleanup_source_never_flattens_or_sends():
    src = Path(cleanup.__file__).read_text(encoding="utf-8")
    assert "flatten_all" not in src
    assert "panic" not in src
    assert "Start is not flatten" in src
