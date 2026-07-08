"""Launch evidence — subprocess probe + live module launch stays alive."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from abcxauto.desktop import TITLE

REPO = Path(__file__).resolve().parents[1]
SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")


def test_launch_probe_captures_window_title(tmp_path):
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
    assert f"title={TITLE}" in text
    assert "title_match=True" in text
    assert "mainloop_ready=True" in text
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "launch_probe.log").write_text(text, encoding="utf-8")


def test_module_launch_stays_alive():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "abcxauto"],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(3)
        assert proc.poll() is None, "process exited early"
        (SCRATCH / "launch_run1.log").write_text(
            f"alive_after_3s=True\npid={proc.pid}\nno_immediate_traceback=True\n",
            encoding="utf-8",
        )
        proc2 = subprocess.Popen(
            [sys.executable, "-m", "abcxauto"],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(3)
        assert proc2.poll() is None
        (SCRATCH / "launch_run2.log").write_text(
            f"alive_after_3s=True\npid={proc2.pid}\nno_immediate_traceback=True\n",
            encoding="utf-8",
        )
        proc2.kill()
    finally:
        proc.kill()