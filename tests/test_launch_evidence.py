"""Launch evidence — Tk widget probe + Pro module probe."""

import os
import subprocess
import sys
import time
from pathlib import Path

from abcxauto.desktop import TITLE as TK_TITLE
from abcxauto.pro_desktop import TITLE as PRO_TITLE

REPO = Path(__file__).resolve().parents[1]
SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")


def _widget_probe_launch(run_name: str) -> str:
    """Independent observation: launch_probe reads app.root.title() from live Tk widget."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    log_path = SCRATCH / f"{run_name}.log"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "launch_probe.py"), str(log_path)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=15,
    )
    text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert proc.returncode == 0, proc.stderr
    assert f"title={TK_TITLE}" in text, text
    assert "title_match=True" in text, text
    assert "mainloop_ready=True" in text, text
    return text


def _module_probe(run_name: str, *, tk: bool = False) -> str:
    """python -m abcxauto writes launch probe then exits (no GUI under probe env)."""
    probe = SCRATCH / f"{run_name}_probe.txt"
    stderr_path = SCRATCH / f"{run_name}_stderr.txt"
    for p in (probe, stderr_path):
        if p.exists():
            p.unlink()
    env = {**os.environ, "ABCXAUTO_LAUNCH_PROBE": str(probe)}
    cmd = [sys.executable, "-m", "abcxauto"]
    if tk:
        cmd.append("--tk")
    proc = subprocess.run(
        cmd,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    err = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else proc.stderr
    assert proc.returncode == 0, err or proc.stderr
    probe_text = probe.read_text(encoding="utf-8") if probe.exists() else ""
    expected = TK_TITLE if tk else PRO_TITLE
    assert f"title={expected}" in probe_text, probe_text
    assert "mainloop_ready=True" in probe_text
    return probe_text


def test_launch_run1_widget_probe_log():
    _widget_probe_launch("launch_run1")
    _module_probe("launch_run1")


def test_launch_run2_widget_probe_log():
    _widget_probe_launch("launch_run2")
    _module_probe("launch_run2")


def test_launch_pro_and_tk_module_probes():
    _module_probe("launch_pro")
    _module_probe("launch_tk", tk=True)


def test_launch_probe_script_writes_headless_probe(tmp_path):
    log = tmp_path / "launch_probe.log"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "launch_probe.py"), str(log)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    text = log.read_text(encoding="utf-8")
    assert f"title={TK_TITLE}" in text
    assert "title_match=True" in text
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "headless_probe.log").write_text(text, encoding="utf-8")
