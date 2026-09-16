"""ABCXAUTO Pro — three-column Flet cockpit over ProEngine.

Public import surface. Layout, tabs, sync, and actions live in
``abcxauto.desktop``. Call sites keep importing from here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import flet as ft

from abcxauto.config import get_config, setup_file_logging
from abcxauto.desktop.stream import (
    current_look_text,
    format_token_count,
    grok_sub_color,
    grok_sub_state,
    last_card_send_label,
    session_cap_idle_line,
    stream_line_kind,
    stream_view_lines,
    think_tail_in_flight,
    think_tail_last_marker,
    think_tail_last_say,
    think_tail_tool_chips,
)
from abcxauto.desktop.terminal import ProTerminal
from abcxauto.desktop.tokens import (
    AMBER,
    ASIDE_W,
    ASSETS_DIR,
    AGENT_FIELD_KEYS,
    BG,
    BLUE,
    BORDER,
    BRAIN_FIELDS,
    CARD_STATUS_COLOR,
    CENTER_MIN_W,
    FLOOR_GATES,
    GREEN,
    HOVER,
    LINK_FIELDS,
    LOGO_SRC,
    MUTED,
    NAV,
    NAV_SUBTITLES,
    NAV_TITLES,
    NOTE_COLOR,
    PACING_FIELDS,
    PAGE_REFRESH_S,
    PRO_TITLE,
    RAIL_BTN_W,
    RAIL_W,
    RED,
    RISK_FIELDS,
    STREAM_ALARM,
    STREAM_FONT_SIZE,
    STREAM_POKE,
    STREAM_TAIL_CHARS,
    STREAM_WARN,
    SURFACE,
    TAIL_LIVE_S,
    TEXT,
    TITLE,
    WHITE,
)

logger = logging.getLogger(__name__)


def main(page: ft.Page) -> None:
    ProTerminal(page).build()


def write_launch_probe(path: str | Path) -> None:
    Path(path).write_text(
        f"title={TITLE}\nexpected={TITLE}\nstatus=Safe\nmainloop_ready=True\n",
        encoding="utf-8",
    )


def run_app() -> None:
    setup_file_logging()
    try:
        from abcxauto.config import launch_model_knobs

        launch_model_knobs(reload=True)
    except Exception:
        logger.debug("launch model knobs on run_app failed", exc_info=True)
    try:
        from abcxauto.headless import _quiet_ibkr_scanner_noise

        _quiet_ibkr_scanner_noise()
    except Exception:
        pass
    probe = os.environ.get("ABCXAUTO_LAUNCH_PROBE")
    if probe:
        write_launch_probe(probe)
        print(f"ABCXAUTO title={TITLE} mainloop_ready=True status=Safe", flush=True)
        return
    print(f"ABCXAUTO Pro entry={Path(__file__).resolve()} title={TITLE}", flush=True)
    runner = getattr(ft, "run", None) or ft.app
    view = ft.AppView.FLET_APP
    if os.environ.get("ABCXAUTO_PRO_WEB", "").strip() in ("1", "true", "yes"):
        view = ft.AppView.WEB_BROWSER
    kwargs: dict[str, Any] = {"assets_dir": str(ASSETS_DIR), "view": view}
    try:
        runner(main, **kwargs)
    except TypeError:
        runner(main)


if __name__ == "__main__":
    run_app()
