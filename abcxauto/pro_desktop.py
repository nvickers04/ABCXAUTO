"""ABCXAUTO Pro — three-column Flet cockpit over ProEngine.

One surface per thing the project actually has: the live look (Dashboard),
Grok's think stream (Stream), the screen Grok pulled (Scan), the broker book
(Positions), Grok's setup cards (Playbook), how that book is scoring against
the model bill (Scorecard), and the walk-away floor (Risk).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import flet as ft

from abcxauto.broker.connection import LIVE_CONFIRM_PHRASE
from abcxauto.config import get_config, setup_file_logging
from abcxauto.desktop.pages import PagesMixin
from abcxauto.desktop.actions import ActionsMixin
from abcxauto.desktop.widgets import WidgetsMixin
from abcxauto.desktop.sync import SyncMixin
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
from abcxauto.pro_engine import ProEngine
from abcxauto.reality_pulse import build_reality_pulse, format_desk_clock, pulse_clock_view
from abcxauto.think_stream import think_session_text

logger = logging.getLogger(__name__)


class ProTerminal(WidgetsMixin, PagesMixin, SyncMixin, ActionsMixin):
    def __init__(self, page: ft.Page):
        self.page = page
        self.engine = ProEngine()
        self.tab = "overview"
        self._think_sync_key: str | None = None  # None = never painted, "" = painted empty
        self._build_refs()
        self._sync_widgets()

    # ------------------------------------------------------------------ build

    def build(self) -> None:
        p, cfg = self.page, get_config()
        p.title = PRO_TITLE
        p.bgcolor = BG
        p.padding = 0
        p.theme_mode = ft.ThemeMode.DARK
        try:
            p.window.visible = True
            p.window.width = 1280
            p.window.height = 860
            p.window.min_width = RAIL_W + CENTER_MIN_W + ASIDE_W
            p.window.min_height = 720
            # Without prevent_close flet never delivers the "close" event, so the
            # stop-on-close below was dead. We latch, then destroy the window.
            p.window.prevent_close = True
            p.window.on_event = self._on_window_event
        except Exception:
            pass
        self.lbl_model.value = f"Grok {getattr(cfg, 'model', '—')}"
        try:
            from abcxauto.memory import get_journal

            get_journal().ensure_model_session(str(getattr(cfg, "model", "") or ""))
        except Exception:
            pass
        try:
            p.controls.clear()
        except Exception:
            pass
        p.add(self._shell())
        self._show_tab("overview")
        self._sync_widgets()
        p.update()
        p.run_task(self._poll_loop)
        p.run_task(self._clock_loop)
        p.run_task(self._reveal_window)
        from abcxauto.cursor_env import should_autostart

        if should_autostart():
            self._start()
        if probe := os.environ.get("ABCXAUTO_UI_PROBE"):
            Path(probe).write_text(
                json.dumps(
                    {
                        "title": p.title,
                        "tab": self.tab,
                        "ui_built": True,
                        "engine": "ProEngine",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _on_window_event(self, e: Any = None) -> None:
        """Closing the window is the operator saying stop — the supervisor must obey."""
        kind = str(getattr(e, "type", "") or getattr(e, "data", "") or "").lower()
        if "close" not in kind:
            return
        try:
            if os.environ.get("ABCXAUTO_UI_PROBE"):
                # A preview window is not the desk. Latching here silently blocks
                # the supervisor's next launch of the real Pro.
                logger.info("probe window closed — operator stop not latched")
            else:
                from abcxauto.supervisor import kill_descendant_flet, mark_operator_stop

                mark_operator_stop()
                kill_descendant_flet()
        except Exception:
            logger.debug("operator stop on window close failed", exc_info=True)
        finally:
            # prevent_close holds the window open, so the operator only gets out
            # if this runs no matter what happened above.
            self._destroy_window()

    def _destroy_window(self) -> None:
        """flet's window destroy/close are coroutines — they need the page loop."""
        win = getattr(self.page, "window", None)
        for step in ("destroy", "close"):
            fn = getattr(win, step, None)
            if fn is None:
                continue
            try:
                self.page.run_task(fn)
                return
            except Exception:
                logger.debug("window %s failed", step, exc_info=True)

    # ----------------------------------------------------------------- polls

    async def _reveal_window(self) -> None:
        try:
            await self.page.window.wait_until_ready_to_show()
        except Exception:
            pass
        try:
            self.page.window.visible = True
            await self.page.window.to_front()
        except Exception:
            try:
                self.page.window.visible = True
            except Exception:
                pass
        self._safe_update()

    async def _poll_loop(self) -> None:
        while True:
            try:
                self.engine.drain_apply()
                self._sync_widgets()
                self._safe_update()
            except Exception:
                logger.exception("poll loop tick failed")
            await asyncio.sleep(0.12)

    async def _clock_loop(self) -> None:
        while True:
            try:
                pulse = self.engine.state.reality_pulse or build_reality_pulse(
                    ibkr_connected=self.engine.state.connected,
                    positions=self.engine.state.positions,
                    account=None,
                )
                self._apply_clock(pulse)
                await self._refresh_returns()
                await self._refresh_news()
                self._safe_update()
            except Exception:
                pass
            await asyncio.sleep(1.0)


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
