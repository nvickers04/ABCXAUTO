"""Detect Cursor so Pro desktop + Grok stream open instead of a silent console."""

from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
START_PRO_PATH = _REPO / "logs" / "_start_pro.py"

# Supported one-shot start. No cleanup_pro call: that script used to match the
# launcher's own command line and suicide the desk (commit 8eb97ce). Leftover
# _start_pro / Pro python and orphan ABCXAUTO flet are reaped in supervisor.
START_PRO_SOURCE = """import os
os.environ["ABCXAUTO_AUTOSTART"] = "1"
os.environ.pop("ABCXAUTO_LAUNCH_PROBE", None)
from abcxauto.supervisor import prepare_desk_start, claim_desk_lock, release_desk_lock
prepare_desk_start()
if not claim_desk_lock():
    raise SystemExit(0)
from abcxauto.think_stream import begin_run
begin_run()
from abcxauto.pro_desktop import run_app
try:
    run_app()
finally:
    release_desk_lock()
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
    """Start the agent as soon as Pro is up so the think stream is live.

    Persist ``ABCXAUTO_AUTOSTART=1`` when we decide yes. Flet re-enters
    ``__main__`` and can drop Cursor-only env; the flag is what keeps Grok
    starting so the think stream is not a silent console.
    """
    if _in_pytest() or os.environ.get("ABCXAUTO_UI_PROBE"):
        return False
    if _truthy("ABCXAUTO_NO_AUTOSTART"):
        return False
    if _truthy("ABCXAUTO_AUTOSTART") or running_in_cursor():
        os.environ["ABCXAUTO_AUTOSTART"] = "1"
        return True
    return False
