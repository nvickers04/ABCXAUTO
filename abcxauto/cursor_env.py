"""Detect Cursor so Pro desktop + Grok stream open instead of a silent console."""

from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
START_PRO_PATH = _REPO / "logs" / "_start_pro.py"

# Supported one-shot start. No cleanup call: pre-launch kills match the launcher's
# own command line and can suicide the desk that is starting (commit 8eb97ce).
START_PRO_SOURCE = """import os
os.environ["ABCXAUTO_AUTOSTART"] = "1"
os.environ.pop("ABCXAUTO_LAUNCH_PROBE", None)
from abcxauto.think_stream import begin_run
begin_run()
from abcxauto.pro_desktop import run_app
run_app()
"""


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def start_pro_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_START_PRO_PATH") or "").strip()
    return Path(raw) if raw else START_PRO_PATH


def write_start_pro_script(path: str | Path | None = None) -> Path:
    """Write the autostart script the operator runs to open Pro, return its path."""
    target = Path(path) if path is not None else start_pro_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(START_PRO_SOURCE, encoding="utf-8")
    return target


def running_in_cursor() -> bool:
    """True when this process is a Cursor terminal, debug session, or agent shell."""
    if _truthy("ABCXAUTO_NOT_CURSOR"):
        return False
    if _truthy("ABCXAUTO_IN_CURSOR"):
        return True
    for key in ("CURSOR_TRACE_ID", "CURSOR_AGENT", "CURSOR_SESSION_ID"):
        if (os.environ.get(key) or "").strip():
            return True
    blob = " ".join(
        os.environ.get(k, "")
        for k in (
            "VSCODE_GIT_ASKPASS_NODE",
            "VSCODE_GIT_IPC_HANDLE",
            "VSCODE_NLS_CONFIG",
            "VSCODE_CWD",
        )
    ).lower()
    return "cursor" in blob


def _in_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or _truthy("ABCXAUTO_LAUNCH_PROBE")


def should_autostart() -> bool:
    """Start the agent as soon as Pro is up so the think stream is live."""
    if _in_pytest() or os.environ.get("ABCXAUTO_UI_PROBE"):
        return False
    if _truthy("ABCXAUTO_NO_AUTOSTART"):
        return False
    if _truthy("ABCXAUTO_AUTOSTART"):
        return True
    return running_in_cursor()
