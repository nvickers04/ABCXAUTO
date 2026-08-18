"""ABCXAUTO Pro Desktop — launch: python -m abcxauto

Default: Pro UI (``pro_desktop.run_app`` Flet) every run.
``--headless`` does not skip the cockpit. Console-only only when
``ABCXAUTO_FORCE_HEADLESS=1``. In Cursor the agent autostarts so the Grok
think stream is visible.
Use ``python -m abcxauto --cleanup`` to kill stale Flet/Python Pro processes
and clear project ``__pycache__``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
        from abcxauto.supervisor import mark_operator_stop

        mark_operator_stop()
        raise SystemExit(
            _cleanup(
                aggressive="--aggressive" in sys.argv,
                flet_cache="--flet-cache" in sys.argv,
                kill_only="--kill-only" in sys.argv,
            )
        )
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
                _cleanup(aggressive=False, flet_cache=False, kill_only=True)
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
        from abcxauto.supervisor import clear_operator_stop, supervise

        clear_operator_stop()
        _cleanup(aggressive=False, flet_cache=False, kill_only=True)
        print("supervisor: launching Pro child", flush=True)
        raise SystemExit(supervise())
    # Kill leftover ABCXAUTO Python/Flet — never this brand-new process.
    if not probe:
        _cleanup(aggressive=False, flet_cache=False, kill_only=True)
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
