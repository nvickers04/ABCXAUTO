"""ABCXAUTO Pro Desktop — launch: python -m abcxauto

Use ``python -m abcxauto --cleanup`` to kill stale Flet/Python Pro processes
and clear project ``__pycache__``.
Use ``python -m abcxauto --cleanup --aggressive --flet-cache`` for a deep clean.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if "--cleanup" in sys.argv:
        from abcxauto.cleanup import main as cleanup_main

        sys.argv = [sys.argv[0], *[a for a in sys.argv[1:] if a != "--cleanup"]]
        raise SystemExit(cleanup_main())

    from abcxauto.cleanup import clear_pycache, kill_stale

    # Clear stale Pro/Flet windows so an old client isn't reused.
    kill_stale(aggressive=False)
    clear_pycache()

    from abcxauto.ui import run_app

    print(f"launching Pro from {Path(run_app.__code__.co_filename)}", flush=True)
    run_app()


if __name__ == "__main__":
    main()
