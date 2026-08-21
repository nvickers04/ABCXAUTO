"""logs/app.log is what the operator reads to see what the desk did.

A test run used to land in it: ``run_headless()`` calls ``setup_file_logging()``,
which attached a handler on the real file for the rest of the pytest session, so
every later WARNING+ record — fake halts, AUTO-PANIC on a fake NetLiq — was
written into the operator's evidence.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from abcxauto.config import setup_file_logging

REPO_LOGS = (Path(__file__).resolve().parents[1] / "logs").resolve()


def _repo_log_handlers() -> list[str]:
    found = []
    for name in ("abcxauto", ""):
        for handler in logging.getLogger(name).handlers:
            if isinstance(handler, RotatingFileHandler):
                target = Path(getattr(handler, "baseFilename", ""))
                if target.resolve().parent == REPO_LOGS:
                    found.append(str(target))
    return found


def test_setup_file_logging_honours_the_env_override(tmp_path, monkeypatch):
    dest = tmp_path / "nested" / "app.log"
    monkeypatch.setenv("ABCXAUTO_LOG_PATH", str(dest))
    setup_file_logging()
    logging.getLogger("abcxauto.test").critical("PANIC that never happened")
    for handler in logging.getLogger("abcxauto").handlers:
        handler.flush()
    assert dest.is_file()
    assert "PANIC that never happened" in dest.read_text(encoding="utf-8")


def test_no_test_ever_holds_a_handler_on_the_real_app_log():
    assert _repo_log_handlers() == []


def test_the_run_headless_call_site_cannot_reattach_the_real_app_log():
    """``run_headless()`` is a real desk entry point — it opens an IBKR socket, so
    a test must never call it. Exercise the one line of it that caused the leak."""
    src = (Path(__file__).resolve().parents[1] / "abcxauto" / "headless.py").read_text(
        encoding="utf-8"
    )
    assert "setup_file_logging()" in src
    setup_file_logging()
    assert _repo_log_handlers() == []


def test_a_test_run_cannot_stop_the_live_desk():
    """A real operator_stop.json shuts the desk down — one stray test would kill it."""
    import os

    from abcxauto.supervisor import STOP_PATH, _stop_path

    assert _stop_path() != STOP_PATH
    assert str(_stop_path()) == os.environ["ABCXAUTO_OPERATOR_STOP_PATH"]


def test_the_child_console_file_is_isolated_too():
    from abcxauto.supervisor import DESK_OUT_PATH, _desk_out_path

    assert _desk_out_path() != DESK_OUT_PATH


def test_the_real_app_log_is_untouched_by_a_failing_desk_record(tmp_path, monkeypatch):
    live = REPO_LOGS / "app.log"
    before = live.stat().st_size if live.is_file() else None
    monkeypatch.setenv("ABCXAUTO_LOG_PATH", str(tmp_path / "app.log"))
    setup_file_logging()
    logging.getLogger("abcxauto.risk_gates").critical("RISK GATE HALTED (halt): test halt")
    for handler in logging.getLogger("abcxauto").handlers:
        handler.flush()
    after = live.stat().st_size if live.is_file() else None
    assert after == before
