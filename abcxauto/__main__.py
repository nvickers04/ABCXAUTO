"""ABCXAUTO Pro Desktop v0.1 — launch: python -m abcxauto

Use ``python -m abcxauto --tk`` for the legacy Tkinter cockpit.
"""

import sys


def main() -> None:
    if "--tk" in sys.argv:
        from abcxauto.desktop import run_app
    else:
        from abcxauto.pro_desktop import run_app
    run_app()


if __name__ == "__main__":
    main()
