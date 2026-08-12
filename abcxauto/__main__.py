"""ABCXAUTO Pro Desktop — launch: python -m abcxauto

Default: Pro UI (``pro_desktop.run_app`` Flet).
Use ``python -m abcxauto --desktop`` for the web Pro native window.
Use ``python -m abcxauto --cleanup`` to kill stale Flet/Python Pro processes
and clear project ``__pycache__``.
Use ``python -m abcxauto --cleanup --aggressive --flet-cache`` for a deep clean.
Use ``python -m abcxauto --headless`` to run the autonomous paper loop
(no UI; Ctrl+C is the kill switch).
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
        raise SystemExit(
            _cleanup(
                aggressive="--aggressive" in sys.argv,
                flet_cache="--flet-cache" in sys.argv,
                kill_only="--kill-only" in sys.argv,
            )
        )
    if "--desktop" in sys.argv or "--web" in sys.argv:
        from abcxauto.desktop_app import run

        raise SystemExit(run())
    if "--headless" in sys.argv:
        from abcxauto.headless import run_headless

        raise SystemExit(run_headless())
    # Clear stale Flet / titled Pro windows only — never match this
    # brand-new ``python -m abcxauto`` process.
    if not os.environ.get("ABCXAUTO_LAUNCH_PROBE"):
        _cleanup(aggressive=False, flet_cache=False, ui_only=True)
    from abcxauto.pro_desktop import run_app

    print(f"launching Pro from {Path(run_app.__code__.co_filename)}", flush=True)
    run_app()


if __name__ == "__main__":
    main()
