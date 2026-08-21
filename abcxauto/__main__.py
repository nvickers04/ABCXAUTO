"""ABCXAUTO Pro Desktop — launch: python -m abcxauto

Default: Pro UI (``pro_desktop.run_app`` Flet) every run.
``--headless`` does not skip the cockpit. Console-only only when
``ABCXAUTO_FORCE_HEADLESS=1``. In Cursor the agent autostarts so the Grok
think stream is visible.
Use ``python -m abcxauto --cleanup`` to kill stale Flet/Python Pro processes
and clear project ``__pycache__``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_start_pro_script() -> None:
    """Give a fresh clone the launcher script it never gets from git (logs/ is ignored).

    Never overwrites: the operator may have edited theirs. Never fatal: a
    read-only logs/ is not a reason to refuse to open the desk.
    """
    try:
        from abcxauto.cursor_env import start_pro_path, write_start_pro_script

        target = start_pro_path()
        if target.exists():
            return
        write_start_pro_script(target)
    except Exception:
        logger.debug("start_pro script write failed", exc_info=True)


def _cleanup(
    *,
    aggressive: bool = False,
    flet_cache: bool = False,
    kill_only: bool = False,
    ui_only: bool = False,
    extra_exclude: list[int] | None = None,
) -> int:
    script = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_pro.py"
    cmd = [sys.executable, str(script), "--exclude-pid", str(os.getpid())]
    if extra_exclude:
        for pid in extra_exclude:
            cmd.extend(["--exclude-pid", str(pid)])
    if aggressive:
        cmd.append("--aggressive")
    if flet_cache:
        cmd.append("--flet-cache")
    if kill_only:
        cmd.append("--kill-only")
    if ui_only:
        cmd.append("--ui-only")
    return subprocess.call(cmd)


def main() -> None:
    if "--cleanup" in sys.argv:
        from abcxauto.supervisor import mark_operator_stop, release_desk_lock

        mark_operator_stop()
        code = _cleanup(
            aggressive="--aggressive" in sys.argv,
            flet_cache="--flet-cache" in sys.argv,
            kill_only="--kill-only" in sys.argv,
        )
        # The killed desk cannot drop its own lock. A stale pid heals on liveness
        # check, but pid reuse would refuse the next launch.
        release_desk_lock()
        raise SystemExit(code)
    if "--desktop" in sys.argv or "--web" in sys.argv:
        print(
            "web-pro / --desktop was removed. Use python -m abcxauto for Flet Pro.",
            flush=True,
        )
        raise SystemExit(2)
    if "--headless" in sys.argv:
        force = (os.environ.get("ABCXAUTO_FORCE_HEADLESS") or "").strip().lower()
        if force in ("1", "true", "yes", "on"):
            if not os.environ.get("ABCXAUTO_LAUNCH_PROBE"):
                from abcxauto.think_stream import begin_run

                begin_run()
            from abcxauto.headless import run_headless

            raise SystemExit(run_headless())
        print(
            "Pro desktop opens every run. "
            "ABCXAUTO_FORCE_HEADLESS=1 for console-only.",
            flush=True,
        )
    supervised = bool((os.environ.get("ABCXAUTO_SUPERVISED") or "").strip())
    probe = bool(os.environ.get("ABCXAUTO_LAUNCH_PROBE"))
    if not probe and not supervised:
        from abcxauto.supervisor import (
            claim_desk_lock,
            clear_operator_stop,
            desk_owner_pid,
            release_desk_lock,
            supervise,
        )

        # Flet re-enters __main__ in its own process and loses ABCXAUTO_SUPERVISED,
        # so without this the app child becomes a second supervisor: two desks,
        # two think loops, two IBKR sessions fighting for one client id.
        if not claim_desk_lock():
            print(
                f"desk already running (pid {desk_owner_pid()}). "
                "python -m abcxauto --cleanup to stop it first.",
                flush=True,
            )
            raise SystemExit(0)
        clear_operator_stop()
        # Only the process that won the lock is "the launch": the supervised child
        # skips this block, and the Flet re-entry loses ABCXAUTO_SUPERVISED but
        # bounces off the claim above. Once per launch, never on --cleanup.
        _ensure_start_pro_script()
        print("supervisor: launching Pro child", flush=True)
        try:
            raise SystemExit(supervise())
        finally:
            release_desk_lock()
    if not probe:
        from abcxauto.think_stream import begin_run

        begin_run()
    from abcxauto.cursor_env import should_autostart
    from abcxauto.pro_desktop import run_app

    if should_autostart():
        os.environ.setdefault("ABCXAUTO_AUTOSTART", "1")
    print(f"launching Pro from {Path(run_app.__code__.co_filename)}", flush=True)
    run_app()


if __name__ == "__main__":
    main()
