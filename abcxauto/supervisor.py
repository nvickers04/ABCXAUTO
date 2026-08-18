"""Relaunch Pro during useful hours unless the operator killed it."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[1]
STOP_PATH = _REPO / "data" / "state" / "operator_stop.json"


def _stop_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_OPERATOR_STOP_PATH") or "").strip()
    return Path(raw) if raw else STOP_PATH


def mark_operator_stop() -> None:
    p = _stop_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"ts": datetime.now().isoformat(), "stop": True}),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("operator_stop write failed", exc_info=True)


def clear_operator_stop() -> None:
    p = _stop_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        logger.debug("operator_stop clear failed", exc_info=True)


def operator_stopped() -> bool:
    p = _stop_path()
    if not p.is_file():
        return False
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return bool(raw.get("stop", True))


def useful_hours(*, now: datetime | None = None) -> bool:
    """RTH plus last hour of premarket (ET). Closed / weekend stay down."""
    try:
        from zoneinfo import ZoneInfo

        clock = now or datetime.now(ZoneInfo("America/New_York"))
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            clock = clock.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        clock = now or datetime.now()
    if clock.weekday() >= 5:
        return False
    minutes = clock.hour * 60 + clock.minute
    return (8 * 60 + 30) <= minutes < (16 * 60)


def tws_listening(host: str = "127.0.0.1", port: int = 7497, timeout: float = 2.0) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def supervise(child_env: dict[str, str] | None = None) -> int:
    """Run Pro as a child. Relaunch on crash during useful hours if TWS is up."""
    env = dict(os.environ)
    if child_env:
        env.update(child_env)
    env["ABCXAUTO_SUPERVISED"] = "1"
    backoff = 15.0
    while True:
        proc = subprocess.Popen(
            [sys.executable, "-m", "abcxauto"],
            env=env,
            cwd=str(_REPO),
        )
        code = proc.wait()
        if int(code or 0) == 0:
            logger.info("supervisor: clean exit — operator closed the window, stay down")
            return 0
        if operator_stopped():
            logger.info("supervisor: operator stop — stay down")
            return int(code or 0)
        if not useful_hours():
            logger.info("supervisor: outside useful hours — stay down")
            return int(code or 0)
        if not tws_listening():
            logger.info("supervisor: TWS 7497 down — stay down")
            return int(code or 0)
        logger.warning("supervisor: child exited %s — relaunch in %.0fs", code, backoff)
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)
