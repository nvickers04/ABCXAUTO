"""ABCXAUTO Pro Desktop v0.1 — launch: python -m abcxauto

Use ``python -m abcxauto --tk`` for the legacy Tkinter cockpit.
Use ``python -m abcxauto --cleanup`` to kill stale Flet/Python Pro processes
and clear project ``__pycache__``.
Use ``python -m abcxauto --cleanup --aggressive --flet-cache`` for a deep clean.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _cleanup(*, aggressive: bool = False, flet_cache: bool = False) -> int:
    script = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_pro.py"
    cmd = [sys.executable, str(script)]
    if aggressive:
        cmd.append("--aggressive")
    if flet_cache:
        cmd.append("--flet-cache")
    return subprocess.call(cmd)


def main() -> None:
    if "--cleanup" in sys.argv:
        raise SystemExit(
            _cleanup(
                aggressive="--aggressive" in sys.argv,
                flet_cache="--flet-cache" in sys.argv,
            )
        )
    if "--tk" in sys.argv:
        from abcxauto.desktop import run_app
    else:
        # Clear stale Pro/Flet windows so an old client isn't reused.
        _cleanup(aggressive=False, flet_cache=False)
        from abcxauto.pro_desktop import run_app

        print(f"launching Pro from {Path(run_app.__code__.co_filename)}", flush=True)
    run_app()


if __name__ == "__main__":
    main()
