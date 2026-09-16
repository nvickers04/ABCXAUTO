"""cleanup_pro must not kill the live paper Pro on 7497 or flatten."""

from __future__ import annotations

import os
import re
from pathlib import Path

import scripts.cleanup_pro as cleanup


def _script_exclude_pids(script: str) -> set[int]:
    """PIDs in ``$exclude = @(…)``. Integers only — ``42`` is not ``4242``."""
    match = re.search(r"\$exclude = @\((.*?)\)", script)
    if not match:
        return set()
    out: set[int] = set()
    for part in match.group(1).split(","):
        token = part.strip()
        if not token:
            continue
        out.add(int(token))
    return out


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


def test_kill_policy_excludes_self_not_live_pro(monkeypatch):
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: ({4242, 77}, True))
    exclude, kill_python, kill_flet, kill_title = cleanup.kill_policy()
    assert os.getpid() in exclude
    assert 4242 not in exclude
    assert 77 not in exclude
    assert kill_python is True
    assert kill_flet is True
    assert kill_title is True


def test_kill_policy_fails_closed_when_scan_misses(monkeypatch):
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: (set(), False))
    _exclude, kill_python, kill_flet, kill_title = cleanup.kill_policy(python_targets=True)
    assert kill_python is False
    assert kill_flet is False
    assert kill_title is False


def test_kill_stale_does_not_spare_live_start_pro(monkeypatch):
    seen: dict[str, str] = {}
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: ({4242, 77}, True))
    monkeypatch.setattr(cleanup, "_ps", lambda script: seen.setdefault("script", script) or "killed none")
    cleanup.kill_stale()
    script = seen["script"]
    excluded = _script_exclude_pids(script)
    assert 4242 not in excluded
    assert 77 not in excluded
    assert os.getpid() in excluded
    assert "_start_pro.py" in script or "_start_pro\\.py" in script
    assert "(_pro_)" not in script
    assert "flatten" not in script.lower()


def test_kill_stale_exclude_is_exact_pid_not_substring(monkeypatch):
    """Self 42 must not match live desk 4242 (two books, two processes)."""
    monkeypatch.setattr(cleanup.os, "getpid", lambda: 42)
    monkeypatch.setattr(cleanup.os, "getppid", lambda: 7)
    monkeypatch.setattr(cleanup, "spare_live_paper_pids", lambda: ({4242, 77}, True))

    class _NoWalk:
        def __init__(self, *_a, **_k):
            raise RuntimeError("no parent walk")

    import psutil

    monkeypatch.setattr(psutil, "Process", _NoWalk)
    seen: dict[str, str] = {}
    monkeypatch.setattr(cleanup, "_ps", lambda script: seen.setdefault("script", script) or "killed none")
    cleanup.kill_stale()
    excluded = _script_exclude_pids(seen["script"])
    assert 42 in excluded
    assert 7 in excluded
    assert 4242 not in excluded
    assert 77 not in excluded


def test_cleanup_source_never_flattens_or_sends():
    src = Path(cleanup.__file__).read_text(encoding="utf-8")
    assert "flatten_all" not in src
    assert "panic" not in src
    assert "Start is not flatten" in src
