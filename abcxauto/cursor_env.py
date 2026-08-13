"""Detect Cursor so Pro desktop + Grok stream open instead of a silent console."""

from __future__ import annotations

import os


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


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


def prefer_desktop() -> bool:
    """Cursor runs should get the Flet cockpit, not --headless."""
    if _truthy("ABCXAUTO_FORCE_HEADLESS") or _in_pytest():
        return False
    return running_in_cursor()


def should_autostart() -> bool:
    """Start the agent as soon as Pro is up so the think stream is live."""
    if _in_pytest() or os.environ.get("ABCXAUTO_UI_PROBE"):
        return False
    if _truthy("ABCXAUTO_NO_AUTOSTART"):
        return False
    if _truthy("ABCXAUTO_AUTOSTART"):
        return True
    return running_in_cursor()
