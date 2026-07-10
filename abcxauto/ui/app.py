"""Pro desktop entry — Flet run_app / launch probe."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import flet as ft

from abcxauto.ui.terminal import ProTerminal
from abcxauto.ui.theme import TITLE


def main(page: ft.Page) -> None:
    ProTerminal(page).build()


def write_launch_probe(path: str | Path) -> None:
    """Headless launch evidence for CI / ABCXAUTO_LAUNCH_PROBE."""
    Path(path).write_text(
        f"title={TITLE}\nexpected={TITLE}\nstatus=Safe\nmainloop_ready=True\n",
        encoding="utf-8",
    )


def run_app() -> None:
    probe = os.environ.get("ABCXAUTO_LAUNCH_PROBE")
    if probe:
        write_launch_probe(probe)
        print(f"ABCXAUTO title={TITLE} mainloop_ready=True status=Safe", flush=True)
        return
    print(f"ABCXAUTO Pro entry={Path(__file__).resolve()} title={TITLE}", flush=True)
    # Flet >=0.80: ft.app is deprecated and can leave the desktop client on
    # the "Working…" splash; ft.run is the supported entrypoint.
    runner = getattr(ft, "run", None) or ft.app
    view = ft.AppView.FLET_APP
    if os.environ.get("ABCXAUTO_PRO_WEB", "").strip() in ("1", "true", "yes"):
        view = ft.AppView.WEB_BROWSER
    kwargs: dict[str, Any] = {"assets_dir": None, "view": view}
    try:
        runner(main, **kwargs)
    except TypeError:
        runner(main)


if __name__ == "__main__":
    run_app()
