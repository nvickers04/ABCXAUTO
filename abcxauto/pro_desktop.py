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


class ProTerminal(PagesMixin, SyncMixin):
    def __init__(self, page: ft.Page):
        self.page = page
        self.engine = ProEngine()
        self.tab = "overview"
        self._think_sync_key: str | None = None  # None = never painted, "" = painted empty
        self._build_refs()
        self._sync_widgets()

    def _build_refs(self) -> None:
        self.lbl_title = ft.Text("ABCXAUTO", size=18, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_model = ft.Text("Grok —", size=11, color=MUTED)
        self.lbl_status = ft.Text("Safe", size=18, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_clock = ft.Text("—", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_session_badge = ft.Text("—", size=11, weight=ft.FontWeight.W_600, color=AMBER)
        self.lbl_countdown_title = ft.Text("Close time", size=11, color=MUTED)
        self.lbl_countdown = ft.Text("—", size=11, color=TEXT)
        self.lbl_data_age = ft.Text("n/a", size=11, color=TEXT)
        self.lbl_mandate_health = ft.Text(
            "green — protected", size=11, weight=ft.FontWeight.W_600, color=GREEN
        )
        self.lbl_pulse_narrative = ft.Text(
            "Reality Pulse idle — Start for live awareness.",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.dot_conn = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.dot_xai = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.dot_mda = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.lbl_ibkr_status = ft.Text("Disconnected", size=12, color=MUTED)
        self.lbl_xai_status = ft.Text("Missing key", size=12, color=MUTED)
        self.lbl_mda_status = ft.Text("Not configured", size=12, color=MUTED)
        self.lbl_link = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_banner = ft.Text("", size=12, color=AMBER, selectable=True, visible=False)
        self.lbl_tools = ft.Text("Tools: —", size=12, color=MUTED, selectable=True)
        self.lbl_playbook = ft.Text("Playbook: —", size=12, color=MUTED, selectable=True)
        self.lbl_score = ft.Text("Score: —", size=13, color=MUTED, selectable=True)
        self.lbl_session_score = ft.Text("sess —", size=12, color=MUTED, selectable=True)
        self._score_last = 0.0
        self.lbl_account_name = ft.Text("IBKR", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_account_id = ft.Text("Not connected", size=12, color=MUTED)
        self.lbl_account_mode = ft.Text("Paper", size=12, weight=ft.FontWeight.W_600, color=GREEN)
        self.btn_account_mode = ft.Container(
            content=self.lbl_account_mode,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, GREEN),
            border_radius=999,
            ink=True,
            tooltip="Switch Paper / Live",
            on_click=self._toggle_trading_mode,
        )
        self.lbl_floors = ft.Text("Floors off", size=12, weight=ft.FontWeight.W_600, color=AMBER)
        self.btn_floors = ft.Container(
            content=self.lbl_floors,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, AMBER),
            border_radius=999,
            ink=True,
            tooltip="Paper: toggle % size floors. Live: always on.",
            on_click=self._toggle_sizing_floors,
        )
        self.tf_live_confirm = ft.TextField(
            label="Type live confirm phrase",
            password=True,
            can_reveal_password=True,
            dense=True,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            width=320,
        )
        self.lbl_equity = ft.Text("$0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_equity_sub = ft.Text("", size=11, color=MUTED)
        self.lbl_pnl = ft.Text("$+0.00", size=14, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_pnl_pct = ft.Text("", size=11, color=MUTED)
        self.lbl_ret_1w = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_3m = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_1y = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_source = ft.Text("IBKR NAV — building history…", size=10, color=MUTED)
        self._ret_cache: dict | None = None
        self._ret_last_fetch = 0.0
        self.news_list = ft.Column(spacing=0, tight=True)
        self._news_last_fetch = 0.0
        self._news_cache: list[dict] = []
        # Status-strip values: small type. This band is a status bar, not a panel.
        self.lbl_unprotected = ft.Text("0", size=13, weight=ft.FontWeight.BOLD, color=GREEN)
        self.lbl_halt = ft.Text("clear", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_edge = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=MUTED)
        self.lbl_edge_sub = ft.Text("vs model", size=11, color=MUTED)
        self.lbl_open_upnl = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_open_upnl_sub = ft.Text("open marks", size=11, color=MUTED)
        self.lbl_desk = ft.Text("Grok off", size=13, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_desk_sub = ft.Text("", size=11, color=MUTED)
        self.lbl_lot_count = ft.Text("0", size=13, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_mix = ft.Text("Mix: —", size=12, color=MUTED, selectable=True)
        self.lbl_path = ft.Text("Path: —", size=12, color=MUTED, selectable=True)
        self._path_last = 0.0
        self._page_last = 0.0
        self._brief_last = 0.0
        self._brief_row: dict = {}
        self._prev_text: str | None = None
        self.lbl_risk = ft.Text("—", size=12, color=MUTED, selectable=True)
        self.lbl_pace = ft.Text("Pace: —", size=12, color=MUTED, selectable=True)
        self.lbl_last_send = ft.Text("—", size=12, color=MUTED, selectable=True)
        self.lbl_result = ft.Text("Result: —", size=12, color=MUTED, selectable=True)
        self.lbl_why = ft.Text("Why: —", size=12, color=MUTED, selectable=True)
        self.lbl_focus = ft.Text("Focus: —", size=12, color=MUTED, selectable=True)
        self.lbl_lessons = ft.Text("", size=11, color=AMBER, selectable=True, visible=False)
        # ---- Scan tape: the screen Grok pulled, in IBKR's own order.
        self.lbl_scan_head = ft.Text(
            "No screen this session — Grok runs the scanner.",
            size=11,
            color=MUTED,
            selectable=True,
        )
        self.col_scan = ft.Column(spacing=3, tight=True)
        self._scan_key = ""
        # ---- Health strip: the three things that make the operator step in —
        # silence, burn, and a link that explains a quiet desk. Not risk numbers.
        self.lbl_hs_state = ft.Text("off", size=12, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_hs_age = ft.Text("no look yet", size=11, color=MUTED, selectable=True)
        self.lbl_hs_next = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_hs_burn = ft.Text("no looks yet", size=12, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_hs_look = ft.Text("this look: —", size=11, color=MUTED, selectable=True)
        self.lbl_hs_link = ft.Text("link —", size=11, color=MUTED, selectable=True)
        self.health_box = ft.Container(padding=0)
        self._sc_last: dict = {}
        self._tail_len: int | None = None
        self._tail_fp: str | None = None
        self._tail_moved_mono = 0.0
        self.lbl_stream_status = ft.Text("Grok stream", size=12, color=MUTED)
        self.think_live = ft.Text(
            "Grok stream: waiting for tools...",
            size=STREAM_FONT_SIZE,
            color=MUTED,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        self.btn_copy_stream = self._btn("Copy stream", outlined=True, on_click=self._copy_stream)
        # The spine: one control per stream line so markers can be styled.
        # think_live is the empty-state label only — never a sliced day.
        self.col_stream = ft.Column(spacing=0, tight=True)
        self._stream_lines_key = ""
        self._stream_follow = True
        self.lbl_stream_follow = ft.Text("live", size=11, weight=ft.FontWeight.W_600, color=GREEN)
        self.btn_stream_follow = ft.Container(
            content=self.lbl_stream_follow,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, GREEN),
            border_radius=999,
            ink=True,
            tooltip="Following the tail. Click to hold position / jump back to live.",
            on_click=self._toggle_stream_follow,
        )
        # auto_scroll pins the tail on the client. A server-side scroll_to here is
        # an invoke_method, and its result can land after a tab swap has already
        # unregistered this control — which kills flet's receive loop.
        self.think_scroll = ft.Column(
            [self.col_stream, self.think_live],
            scroll=ft.ScrollMode.AUTO,
            auto_scroll=True,
            expand=True,
            spacing=0,
        )
        # ---- Positions blotter: one row control per record, text form kept as fallback.
        self.lbl_positions = ft.Text("No open positions", size=12, color=MUTED, selectable=True)
        self.lbl_working_orders = ft.Text(
            "No working orders", size=12, color=MUTED, selectable=True
        )
        self.lbl_recent_fills = ft.Text(
            "No fills this session", size=12, color=MUTED, selectable=True
        )
        self.lbl_activity = ft.Text("Connect IBKR.", size=12, color=MUTED, selectable=True)
        self.col_lots = ft.Column(
            [self.lbl_positions], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self.col_orders = ft.Column(
            [self.lbl_working_orders], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self.col_fills = ft.Column(
            [self.lbl_recent_fills], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self.col_activity = ft.Column(
            [self.lbl_activity], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self._lots_key = ""
        self._orders_key = ""
        self._fills_key = ""
        self._activity_key = ""
        self._tab = "lots"
        self.tabs: dict[str, dict[str, Any]] = {}
        for key, label in (
            ("lots", "Lots"),
            ("orders", "Working orders"),
            ("fills", "Session fills"),
            ("log", "Activity"),
        ):
            self.tabs[key] = self._tab_chip(key, label)
        self.tab_bodies: dict[str, ft.Control] = {
            "lots": self.col_lots,
            "orders": self.col_orders,
            "fills": self.col_fills,
            "log": self.col_activity,
        }
        self.lbl_risk_status = ft.Text("", size=12, color=MUTED, selectable=True)
        # One line each — a long rationale must not reserve blank rows.
        for lbl in (
            self.lbl_banner,
            self.lbl_mix,
            self.lbl_score,
            self.lbl_path,
            self.lbl_pace,
            self.lbl_playbook,
            self.lbl_risk,
            self.lbl_tools,
            self.lbl_last_send,
            self.lbl_result,
            self.lbl_why,
            self.lbl_focus,
            self.lbl_lessons,
            self.lbl_scan_head,
        ):
            lbl.max_lines = 1
            lbl.overflow = ft.TextOverflow.ELLIPSIS
        self.btn_connect = self._btn(
            "Connect IBKR", outlined=True, on_click=self._toggle_connect, width=RAIL_BTN_W
        )
        self.btn_run = self._btn("Start", filled=True, on_click=self._toggle_run, width=RAIL_BTN_W)
        self.btn_halt = self._btn(
            "Halt", outlined=True, on_click=self._toggle_halt, width=RAIL_BTN_W
        )
        self.btn_refresh = self._btn(
            "Refresh book", outlined=True, on_click=self._refresh_book, width=RAIL_BTN_W
        )
        self.lbl_run_state = ft.Text("Grok off", size=12, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_alert = ft.Text("", size=12, color=RED, selectable=True, visible=False)
        # ---- Notebook page
        self.lbl_notebook_head = ft.Text("", size=13, color=TEXT, selectable=True)
        self.lbl_notebook_meta = ft.Text("", size=12, color=MUTED, selectable=True)
        self.lbl_notebook_lots = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_notebook_body = ft.Text(
            "",
            size=12,
            color=TEXT,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        self.col_notebook_cards = ft.Column(spacing=8, tight=True)
        self.col_notebook_types = ft.Column(spacing=2, tight=True)
        self.lbl_notebook_types = ft.Text("", size=12, color=MUTED, selectable=True)
        self.notebook_raw_panel = ft.Container(visible=False)
        self._notebook_key = ""
        # ---- Scorecard page
        self.lbl_sc_netliq = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_sc_verdict = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_sc_score = ft.Text("Score: —", size=12, color=MUTED, selectable=True)
        self.lbl_sc_session = ft.Text("sess —", size=12, color=MUTED, selectable=True)
        self.lbl_sc_strats = ft.Text("", size=12, color=MUTED, selectable=True)
        self.col_sc_windows = ft.Column(spacing=3, tight=True)
        self.col_sc_cards = ft.Column(spacing=3, tight=True)
        self.col_sc_ledger = ft.Column(spacing=3, tight=True)
        # ---- Risk page
        self.lbl_risk_glance = ft.Text("", size=12, color=MUTED, selectable=True, no_wrap=False)
        self.col_risk_knobs = ft.Column(spacing=3, tight=True)
        self.lbl_risk_posture = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=TEXT)
        self.lbl_risk_floors = ft.Text("", size=12, color=MUTED, selectable=True)
        self.sw_size_floors = ft.Switch(
            value=False,
            active_color=GREEN,
            on_change=self._toggle_sizing_floors,
        )
        self.lbl_risk_halt_state = ft.Text("", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_risk_halt_math = ft.Text("", size=12, color=MUTED, selectable=True)
        # An open edit must survive the 3s page repaint, so a touched field is
        # dirty until it is applied or the page is refreshed.
        self.fields: dict[str, ft.TextField] = {}
        self.gates: dict[str, ft.Switch] = {}
        self._dirty: set[str] = set()
        # Derived, not restated: a knob added to RISK_FIELDS must get a widget or
        # the Risk page raises on the row it cannot build.
        for key, _label, _hint in RISK_FIELDS:
            self._num_field(key)
        for key in (
            "defined_risk_only",
            "cash_only",
            "risk_gates_enabled",
            "auto_panic_on_breach",
        ):
            self.gates[key] = ft.Switch(
                value=True,
                active_color=GREEN,
                on_change=lambda e, k=key: self._toggle_floor_gate(k),
            )
        self._risk_key = ""
        # ---- Settings page
        for key in (
            "model",
            "model_rth",
            "model_research",
            "model_params",
            "model_params_rth",
            "model_params_research",
            "temperature",
            "max_tokens",
            "monitor_poll_s",
            "monitor_review_s",
            "disconnect_halt_s",
            "session_look_cap",
            "session_token_cap",
            "ibkr_host",
            "ibkr_client_id",
        ):
            self._num_field(
                key,
                width=150
                if key in (
                    "model",
                    "model_rth",
                    "model_research",
                    "model_params",
                    "model_params_rth",
                    "model_params_research",
                    "ibkr_host",
                    "session_token_cap",
                )
                else 110,
            )
        for key in ("monitor_enabled", "monitor_extended_hours"):
            self.gates[key] = ft.Switch(
                value=True,
                active_color=BLUE,
                on_change=lambda e, k=key: self._apply_agent_switch(k),
            )
        self.lbl_settings_status = ft.Text("", size=12, color=MUTED, selectable=True)
        self.lbl_settings_link = ft.Text("", size=12, color=TEXT, selectable=True)
        self.lbl_settings_mode = ft.Text("Paper", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_settings_brain = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_settings_path = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_dash_tools = ft.Text("Tools: —", size=12, color=MUTED, selectable=True)
        self.lbl_nb_playbook = ft.Text("Playbook: —", size=12, color=MUTED, selectable=True)
        for lbl in (self.lbl_dash_tools, self.lbl_nb_playbook):
            lbl.max_lines = 1
            lbl.overflow = ft.TextOverflow.ELLIPSIS
        # Facts the Cockpit computes; the Dashboard stays a live look, not a report.
        # Mode repeats the rail's Paper/Live pill. tools / focus / pace live in
        # the stream, the playbook and the next-look line. Open MTM is on the
        # Account card next to Today — not hidden.
        self._hidden_metrics = ft.Column(
            [
                self.lbl_session_score,
                self.lbl_path,
                self.lbl_mix,
                self.lbl_why,
                self.lbl_playbook,
                self.lbl_tools,
                self.lbl_status,
                self.lbl_focus,
                self.lbl_dash_tools,
                self.lbl_pulse_narrative,
                self.lbl_pace,
                self.lbl_risk,
            ],
            visible=False,
            spacing=0,
        )
        self.content = ft.Container(expand=True, padding=0)
        self.surface_tabs: dict[str, dict[str, Any]] = {}
        self.lbl_center_title = ft.Text("Dashboard", size=20, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_center_subtitle = ft.Text(NAV_SUBTITLES["overview"], size=12, color=MUTED)

    # ---------------------------------------------------------------- widgets

    def _num_field(self, key: str, *, width: int = 120) -> ft.TextField:
        """One editable config field. Enter applies it; typing marks it dirty."""
        tf = ft.TextField(
            dense=True,
            width=width,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            text_size=12,
            content_padding=8,
            on_change=lambda _e, k=key: self._dirty.add(k),
            on_submit=lambda _e, k=key: self._apply_field(k),
        )
        self.fields[key] = tf
        return tf

    def _set_field(self, key: str, value: Any) -> None:
        tf = self.fields.get(key)
        if tf is None or key in self._dirty:
            return
        if isinstance(value, bool) or value is None:
            tf.value = ""
        elif isinstance(value, dict):
            tf.value = json.dumps(value, sort_keys=True) if value else ""
        elif isinstance(value, float):
            tf.value = f"{value:g}"
        else:
            tf.value = str(value)

    def _field_row(
        self, key: str, label: str, hint: str, *, control: ft.Control | None = None
    ) -> ft.Control:
        body: list[ft.Control] = [
            ft.Container(expand=True, content=ft.Text(label, size=12, color=TEXT)),
            ft.Container(
                width=232,
                tooltip=hint,
                content=ft.Text(
                    hint,
                    size=11,
                    color=MUTED,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            ),
            control if control is not None else self.fields[key],
        ]
        if control is None:
            body.append(
                ft.Container(
                    width=30,
                    height=30,
                    border_radius=15,
                    border=ft.Border.all(1, BORDER),
                    alignment=ft.Alignment.CENTER,
                    ink=True,
                    tooltip="Apply",
                    on_click=lambda _e, k=key: self._apply_field(k),
                    content=ft.Icon(ft.Icons.CHECK, size=15, color=TEXT),
                )
            )
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            border=ft.Border.all(1, BORDER),
            content=ft.Row(body, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def _tab_chip(self, key: str, label: str) -> dict[str, Any]:
        text = ft.Text(label, size=12, weight=ft.FontWeight.W_600, color=MUTED)
        count = ft.Text("", size=11, color=MUTED)
        chip = ft.Container(
            content=ft.Row([text, count], spacing=5, tight=True),
            padding=ft.Padding.symmetric(horizontal=10, vertical=5),
            border_radius=999,
            border=ft.Border.all(1, BORDER),
            ink=True,
            on_click=lambda _e, k=key: self._select_tab(k),
        )
        return {"chip": chip, "text": text, "count": count, "label": label}

    def _select_tab(self, key: str) -> None:
        self._tab = key
        self._sync_tabs()
        self._safe_update()

    def _btn(
        self,
        text: str,
        *,
        on_click,
        filled: bool = False,
        outlined: bool = False,
        width: int | None = None,
    ) -> ft.Button:
        style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=999),
            padding=ft.Padding.symmetric(horizontal=18, vertical=11),
            side=ft.BorderSide(1, BORDER) if outlined or not filled else None,
        )
        btn = ft.Button(
            content=text,
            bgcolor=WHITE if filled else BG,
            color="#0f1419" if filled else TEXT,
            style=style,
            on_click=on_click,
        )
        btn.text = text
        if width:
            btn.width = width
        return btn

    def _avatar(self, letter: str = "A", size: int = 40) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=size // 2,
            bgcolor=SURFACE,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(letter, size=size // 2, weight=ft.FontWeight.BOLD, color=TEXT),
        )

    @staticmethod
    def _chip(text: str, color: str) -> ft.Container:
        return ft.Container(
            content=ft.Text(text, size=11, weight=ft.FontWeight.W_600, color=color),
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
            border=ft.Border.all(1, color),
            border_radius=999,
        )

    @staticmethod
    def _cell(
        text: str,
        *,
        width: int | None = None,
        expand: bool = False,
        color: str = TEXT,
        size: int = 12,
        mono: bool = False,
        right: bool = False,
        weight: Any = None,
    ) -> ft.Control:
        label = ft.Text(
            text,
            size=size,
            color=color,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            font_family="Consolas" if mono else None,
            text_align=ft.TextAlign.RIGHT if right else None,
            weight=weight,
        )
        if expand:
            return ft.Container(expand=True, content=label)
        return ft.Container(width=width, content=label)

    def _blotter_row(self, cells: list[ft.Control], *, alert: bool = False) -> ft.Container:
        return ft.Container(
            content=ft.Row(cells, spacing=6),
            padding=ft.Padding.symmetric(horizontal=8, vertical=5),
            border_radius=6,
            bgcolor=SURFACE if alert else BG,
            border=ft.Border.all(1, RED if alert else BORDER),
        )

    def _head_row(self, cols: list[tuple[str, int | None]]) -> ft.Container:
        cells = [
            self._cell(
                name,
                width=w,
                expand=w is None,
                color=MUTED,
                size=11,
                weight=ft.FontWeight.W_600,
            )
            for name, w in cols
        ]
        return ft.Container(
            content=ft.Row(cells, spacing=6),
            padding=ft.Padding.only(left=8, right=8, top=2, bottom=2),
        )

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

    def _shell(self) -> ft.Control:
        """Left rail · center column · right rail. Rails are fixed; the feed absorbs width."""
        return ft.Row(
            [self._left_rail(), self._center_column(), self._right_rail()],
            expand=True,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _left_rail(self) -> ft.Container:
        logo = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            content=ft.Row(
                [
                    ft.Image(
                        src=LOGO_SRC,
                        width=36,
                        height=36,
                        fit=ft.BoxFit.CONTAIN,
                        error_content=ft.Text(
                            "A", size=22, weight=ft.FontWeight.BOLD, color=BLUE
                        ),
                    ),
                    ft.Column([self.lbl_title, self.lbl_model], spacing=0, tight=True),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._sync_ibkr_account_label()
        self.account_bar = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border=ft.Border(top=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._avatar("I", 36),
                            ft.Column(
                                [self.lbl_account_name, self.lbl_account_id],
                                spacing=1,
                                tight=True,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row([self.btn_account_mode, self.btn_floors], spacing=6, wrap=True),
                    self.lbl_link,
                ],
                spacing=6,
                tight=True,
            ),
        )
        rail_body = ft.Column(
            [
                logo,
                ft.Container(height=6),
                self.btn_connect,
                ft.Container(height=6),
                self.btn_run,
                ft.Container(height=6),
                self.btn_halt,
                ft.Container(height=6),
                self.btn_refresh,
                ft.Container(height=6),
                ft.Row([self.lbl_run_state], spacing=6),
                ft.Container(expand=True),
                self.account_bar,
            ],
            spacing=2,
            expand=True,
        )
        return ft.Container(
            width=RAIL_W,
            bgcolor=BG,
            padding=ft.Padding.only(left=8, right=12, top=4, bottom=8),
            content=rail_body,
        )

    def _surface_tab(self, key: str, label: str) -> ft.Container:
        """Primary navigation. Same pill language as the blotter tabs."""
        text = ft.Text(label, size=12, weight=ft.FontWeight.W_600, color=MUTED)
        chip = ft.Container(
            content=text,
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            border_radius=999,
            border=ft.Border.all(1, BORDER),
            ink=True,
            tooltip=NAV_SUBTITLES.get(key, ""),
            on_click=lambda _e, k=key: self._show_tab(k),
        )
        self.surface_tabs[key] = {"chip": chip, "text": text, "label": label}
        return chip

    def _center_column(self) -> ft.Container:
        self.surface_tabs = {}
        header = ft.Container(
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=16, top=10, bottom=8),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [self.lbl_center_title, self.lbl_center_subtitle],
                                spacing=1,
                                tight=True,
                                expand=True,
                            ),
                            self._refresh_icon(self._refresh_agent_tab),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [self._surface_tab(key, label) for key, label, _o, _f in NAV],
                        spacing=6,
                        wrap=True,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )
        self._sync_surface_tabs()
        return ft.Container(
            expand=True,
            width=None,
            bgcolor=BG,
            border=ft.Border(
                left=ft.BorderSide(1, BORDER),
                right=ft.BorderSide(1, BORDER),
            ),
            content=ft.Column(
                [header, self._status_strip(), self.content],
                spacing=0,
                expand=True,
            ),
        )

    def _status_strip(self) -> ft.Control:
        """One thin pinned band on every surface, small type.

        Only facts that change what the operator does now: is Grok looking, is the
        desk halted, what is exposed, the last say or card, and whether looks are
        burning with no ticket. Model/edge lives on Scorecard, not here.
        """
        self.health_box.content = ft.Row(
            [
                self.lbl_hs_state,
                self.lbl_hs_age,
                self.lbl_hs_next,
                self.lbl_hs_burn,
                self.lbl_hs_look,
                self.lbl_hs_link,
            ],
            spacing=12,
            wrap=True,
        )
        self.health_box.padding = ft.Padding.only(top=1, bottom=1)
        self.health_box.border_radius = 6
        return ft.Container(
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=16, top=5, bottom=5),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._stat("Grok", self.lbl_desk, self.lbl_desk_sub),
                            self._stat("Halt", self.lbl_halt),
                            self._stat("Lots", self.lbl_lot_count),
                            self._stat("Naked", self.lbl_unprotected),
                            self.lbl_last_send,
                            self.lbl_result,
                        ],
                        spacing=14,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.health_box,
                    self.lbl_alert,
                    self.lbl_banner,
                    self.lbl_lessons,
                ],
                spacing=2,
                tight=True,
            ),
        )

    def _stat(self, label: str, value: ft.Control, sub: ft.Control | None = None) -> ft.Control:
        body: list[ft.Control] = [
            ft.Text(label, size=10, color=MUTED, weight=ft.FontWeight.W_600),
            value,
        ]
        if sub is not None:
            body.append(sub)
        return ft.Row(
            body, spacing=5, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER
        )

    def _right_rail(self) -> ft.Container:
        def _ret_col(label: str, value: ft.Text, *, visible: bool = True) -> ft.Column:
            return ft.Column(
                [ft.Text(label, size=11, color=MUTED), value],
                spacing=2,
                tight=True,
                visible=visible,
            )

        self.col_ret_1w = _ret_col("1W", self.lbl_ret_1w, visible=False)
        self.col_ret_3m = _ret_col("3M", self.lbl_ret_3m, visible=False)
        self.col_ret_1y = _ret_col("1Y", self.lbl_ret_1y, visible=False)
        account_card = self._aside_card(
            "Account",
            ft.Column(
                [
                    ft.Text("Total value", size=12, color=MUTED),
                    self.lbl_equity,
                    self.lbl_equity_sub,
                    ft.Row(
                        [
                            _ret_col("Today", self.lbl_pnl),
                            _ret_col("Open", self.lbl_open_upnl),
                            self.col_ret_1w,
                            self.col_ret_3m,
                            self.col_ret_1y,
                        ],
                        spacing=16,
                        wrap=True,
                    ),
                    self.lbl_pnl_pct,
                    self.lbl_open_upnl_sub,
                    self.lbl_ret_source,
                    ft.Container(height=4),
                    ft.Row(
                        [
                            self.lbl_clock,
                            ft.Container(
                                bgcolor=BG,
                                border_radius=999,
                                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                                content=self.lbl_session_badge,
                            ),
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            self.dot_conn,
                            ft.Text("IBKR", size=12, color=TEXT, weight=ft.FontWeight.W_600),
                            self.lbl_ibkr_status,
                        ],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            self.dot_xai,
                            ft.Text("xAI", size=12, color=TEXT, weight=ft.FontWeight.W_600),
                            self.lbl_xai_status,
                        ],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            self.dot_mda,
                            ft.Text("MDA", size=12, color=TEXT, weight=ft.FontWeight.W_600),
                            self.lbl_mda_status,
                        ],
                        spacing=8,
                    ),
                    ft.Row([self.lbl_countdown_title, self.lbl_countdown], spacing=8),
                    ft.Row(
                        [ft.Text("IBKR refresh", size=11, color=MUTED), self.lbl_data_age],
                        spacing=8,
                    ),
                    ft.Row(
                        [ft.Text("Risk posture", size=11, color=MUTED), self.lbl_mandate_health],
                        spacing=8,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
        )
        news_card = self._aside_card(
            "What's happening",
            ft.Column(
                [
                    ft.Text(
                        "Headlines for your book and the broader market.",
                        size=11,
                        color=MUTED,
                    ),
                    self.news_list,
                ],
                spacing=8,
                tight=True,
            ),
        )
        return ft.Container(
            width=ASIDE_W,
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=8, top=8, bottom=12),
            content=ft.Column(
                [account_card, ft.Container(height=12), news_card],
                spacing=0,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
        )

    def _aside_card(self, title: str, body: ft.Control) -> ft.Container:
        return ft.Container(
            bgcolor=SURFACE,
            border_radius=16,
            padding=16,
            content=ft.Column(
                [ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT), body],
                spacing=10,
            ),
        )

    # -------------------------------------------------------------- nav / pages

    def _show_tab(self, key: str) -> None:
        self.tab = key if key in NAV_TITLES else "overview"
        key = self.tab
        self._sync_surface_tabs()
        self.lbl_center_title.value = NAV_TITLES.get(key, "Dashboard")
        self.lbl_center_subtitle.value = NAV_SUBTITLES.get(key, "")
        builders = {
            "overview": self._page_overview,
            "positions": self._page_positions,
            "notebook": self._page_notebook,
            "scorecard": self._page_scorecard,
            "risk": self._page_risk,
            "settings": self._page_settings,
        }
        # Leaving a page abandons whatever was half-typed on it.
        self._dirty.clear()
        self._sync_active_page(force=True)
        self.content.content = builders.get(key, self._page_overview)()
        self._safe_update()

    def _section(self, title: str, *body: ft.Control) -> ft.Container:
        """Clean center panel — hairline separator, no social-post chrome."""
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT), *body],
                spacing=8,
                tight=True,
            ),
        )

    @staticmethod
    def _refresh_icon(on_refresh, size: int = 32) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=size // 2,
            border=ft.Border.all(1, BORDER),
            bgcolor=SURFACE,
            alignment=ft.Alignment.CENTER,
            ink=True,
            tooltip="Refresh",
            on_click=on_refresh,
            content=ft.Icon(ft.Icons.REFRESH, size=16, color=TEXT),
        )

    def _section_header(self, title: str, on_refresh) -> ft.Row:
        return ft.Row(
            [
                ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT, expand=True),
                self._refresh_icon(on_refresh),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _section_refresh(self, title: str, on_refresh, *body: ft.Control) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [self._section_header(title, on_refresh), *body],
                spacing=8,
                tight=True,
            ),
        )

    # ----------------------------------------------------------------- helpers

    def _safe_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass

    def _toast(self, msg: str, *, color: str = BLUE) -> None:
        keep = [c for c in (self.page.overlay or []) if not isinstance(c, ft.SnackBar)]
        bar = ft.SnackBar(ft.Text(msg), bgcolor=color, open=True)
        try:
            self.page.overlay = keep + [bar]
        except Exception:
            self.page.overlay.append(bar)

    def _set_btn_text(
        self,
        btn: ft.Button,
        text: str,
        *,
        filled: bool = False,
        danger: bool = False,
        outlined: bool = False,
    ) -> None:
        btn.text = text
        btn.content = text
        if danger:
            btn.bgcolor = BG
            btn.color = RED
            btn.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                padding=ft.Padding.symmetric(horizontal=18, vertical=11),
                side=ft.BorderSide(1, RED),
            )
            return
        btn.bgcolor = WHITE if filled else BG
        btn.color = "#0f1419" if filled else TEXT
        btn.style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=999),
            padding=ft.Padding.symmetric(horizontal=18, vertical=11),
            side=None if filled else ft.BorderSide(1, BORDER),
        )

    def _toggle_run(self, _=None) -> None:
        s = self.engine.state
        running = bool(s.running) and getattr(s, "autonomous", False) and not getattr(s, "paused", False)
        if running:
            self.engine.pause_engine()
            self._toast("Agent stopped — IBKR stays connected", color=AMBER)
        else:
            err = self.engine.start()
            self._toast(err or "Starting Grok", color=RED if err else BLUE)
        self._sync_widgets()
        self._safe_update()

    def _toggle_connect(self, _=None) -> None:
        logger.info("Connect IBKR clicked")
        s = self.engine.state
        linked = bool(s.connected) or (
            self.engine.worker is not None and self.engine.worker.is_alive()
        )
        if linked:
            self._open_disconnect_confirm_dialog()
            return
        err = self.engine.connect_broker()
        self._toast(err or "Connecting to IBKR…", color=RED if err else BLUE)
        self._sync_widgets()
        self._safe_update()

    def _lab_notebook(self) -> tuple[str, str]:
        """Empty notebook surface. Persist is gone."""
        return "Lab notebook", "(empty)"

    def _open_disconnect_confirm_dialog(self) -> None:
        s = self.engine.state

        def _cancel(_=None) -> None:
            dlg.open = False
            self._safe_update()

        def _confirm(_=None) -> None:
            dlg.open = False
            if getattr(s, "autonomous", False) and s.running:
                self.engine.pause_engine()
            self.engine.stop_engine()
            self._toast("Disconnected from IBKR", color=AMBER)
            self._sync_widgets()
            self._safe_update()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            title=ft.Text("Disconnect IBKR?", color=TEXT),
            content=ft.Text(
                "Stops the agent and the IBKR link. Positions and orders stay at the broker.",
                size=13,
                color=MUTED,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_cancel),
                ft.TextButton("Disconnect", on_click=_confirm, style=ft.ButtonStyle(color=RED)),
            ],
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _toggle_halt(self, _=None) -> None:
        try:
            from abcxauto.risk_gates import get_risk_gate

            gate = get_risk_gate()
            if gate.is_halted:
                gate.resume()
                self._toast("Halt cleared — new risk allowed", color=GREEN)
            else:
                gate.halt("manual halt from console", kind="halt")
                self._toast("New risk halted", color=AMBER)
        except Exception as exc:
            self._toast(f"Halt failed: {exc}", color=RED)
        self._sync_widgets()
        self._sync_risk_page(force=True)
        self._safe_update()

    def _lot_control(self, row: dict) -> ft.Control:
        mtm = self._lot_mark_pct(row)
        if isinstance(mtm, (int, float)):
            mtm_txt = f"{mtm:+.2f}%"
            if mtm < 0:
                mtm_color = RED
            elif mtm > 0:
                mtm_color = GREEN
            else:
                mtm_color = MUTED
        else:
            mtm_txt, mtm_color = "—", MUTED
        avg, mkt = row.get("avg"), row.get("mkt")
        basis = (
            f"{avg:g} → {mkt:g}"
            if isinstance(avg, (int, float)) and isinstance(mkt, (int, float))
            else ""
        )
        cells: list[ft.Control] = [
            ft.Container(
                expand=True,
                content=ft.Text(
                    row.get("ident") or "?",
                    size=12,
                    color=TEXT,
                    font_family="Consolas",
                    no_wrap=True,
                ),
            ),
            ft.Container(
                width=110,
                content=ft.Text(basis, size=11, color=MUTED, text_align=ft.TextAlign.RIGHT),
            ),
            ft.Container(
                width=58,
                content=ft.Text(
                    mtm_txt,
                    size=12,
                    weight=ft.FontWeight.W_600,
                    color=mtm_color,
                    text_align=ft.TextAlign.RIGHT,
                ),
            ),
        ]
        if row.get("unprotected"):
            cells.append(ft.Text("naked", size=11, weight=ft.FontWeight.W_600, color=RED))
        return ft.Container(
            content=ft.Row(cells, spacing=6),
            padding=ft.Padding.symmetric(horizontal=8, vertical=5),
            border_radius=6,
            bgcolor=SURFACE if row.get("unprotected") else BG,
            border=ft.Border.all(1, RED if row.get("unprotected") else BORDER),
        )

    def _copy_stream(self, _=None) -> None:
        text = self._copy_stream_text()
        try:
            self.page.set_clipboard(text)
            self._toast("Stream copied", color=BLUE)
        except Exception as exc:
            self._toast(f"Copy failed: {exc}", color=AMBER)

    def _refresh_book(self, _=None) -> None:
        err = self.engine.request_snapshot()
        self.engine.drain_apply()
        self._toast(err or "Refreshing book…", color=AMBER if err else BLUE)
        self._sync_widgets()
        self._safe_update()

    def _refresh_book_tab(self, _=None) -> None:
        self._refresh_book()

    def _refresh_agent_tab(self, _=None) -> None:
        try:
            pulse = self.engine.state.reality_pulse or build_reality_pulse(
                ibkr_connected=self.engine.state.connected,
                positions=self.engine.state.positions,
                account=None,
            )
            self._apply_clock(pulse)
        except Exception:
            pass
        self.engine.drain_apply()
        err = self.engine.request_snapshot()
        self._toast(
            "Dashboard refreshed (broker snapshot unavailable)" if err else "Refreshing…",
            color=AMBER if err else BLUE,
        )
        self._sync_widgets()
        self._safe_update()

    def _refresh_scorecard_tab(self, _=None) -> None:
        self._score_last = 0.0
        self._path_last = 0.0
        self._refresh_score_line()
        self._sync_path_line()
        self._sync_scorecard_page(force=True)
        self._safe_update()

    def _refresh_notebook_tab(self, _=None) -> None:
        self._sync_notebook_page(force=True)
        self._safe_update()

    def _refresh_risk_tab(self, _=None) -> None:
        self._sync_risk_page(force=True)
        self._safe_update()

    def _start(self, _=None) -> None:
        err = self.engine.start()
        if err:
            self._toast(err, color=RED)
        self._sync_widgets()
        self._safe_update()

    def _notebook_card(self, card: dict, attrib: dict | None = None) -> ft.Control:
        status = str(card.get("status") or "testing").strip().lower()
        color = CARD_STATUS_COLOR.get(status, MUTED)
        head: list[ft.Control] = [
            ft.Text(
                str(card.get("name") or "?"),
                size=13,
                weight=ft.FontWeight.BOLD,
                color=TEXT,
                expand=True,
            ),
            self._chip(status, color),
        ]
        ticket = str(card.get("ticket") or "").strip()
        if ticket:
            head.append(self._chip(ticket, BLUE))
        score = (attrib or {}).get(str(card.get("name") or "").lower()) or {}
        sends = int(score.get("sends") or 0)
        fills = int(score.get("attributed_fills") or 0)
        pnl = score.get("realized_pnl")
        if not sends:
            head.append(self._chip("no sends yet", MUTED))
        elif not fills or not isinstance(pnl, (int, float)):
            head.append(self._chip(f"{sends} send(s) · no fills yet", MUTED))
        else:
            head.append(
                self._chip(
                    f"{sends} send(s) · ${pnl:+,.2f}", GREEN if pnl > 0 else RED if pnl else MUTED
                )
            )
        rows: list[ft.Control] = [ft.Row(head, spacing=6)]
        for field, label in (
            ("when_on", "when"),
            ("scan", "scan"),
            ("shape", "shape"),
            ("invalidation", "invalid"),
            ("fill_assumption", "fill"),
            ("note", "note"),
        ):
            val = str(card.get(field) or "").strip()
            if not val:
                continue
            rows.append(
                ft.Row(
                    [
                        ft.Container(
                            width=56,
                            content=ft.Text(label, size=11, color=MUTED),
                        ),
                        ft.Text(val, size=12, color=TEXT, expand=True, selectable=True),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )
            )
        return ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, color if status == "working" else BORDER),
            border_radius=10,
            padding=12,
            content=ft.Column(rows, spacing=6, tight=True),
        )

    def _refresh_settings_tab(self, _=None) -> None:
        """Drop pending edits and re-read the file so the page shows what stuck."""
        from abcxauto.config import load_risk_settings

        self._dirty.clear()
        try:
            load_risk_settings()
        except Exception:
            logger.debug("settings reload failed", exc_info=True)
        self._sync_settings_page(force=True)
        self._safe_update()

    def _set_risk_posture(self, posture: str) -> None:
        from abcxauto.config import update_risk_config

        try:
            update_risk_config(risk_posture=str(posture or "").strip().lower(), persist=True)
            self._sync_risk_settings_view()
            self._toast(f"Posture → {posture}", color=BLUE)
            self._safe_update()
        except Exception as exc:
            self._toast(f"Posture failed: {exc}", color=RED)
            self._safe_update()

    def _note_setting(self, msg: str, *, color: str = BLUE) -> None:
        """One line the operator can read after an Apply, plus a toast."""
        for lbl in (self.lbl_risk_status, self.lbl_settings_status):
            lbl.value = msg
            lbl.color = color
        self._toast(msg, color=color)

    def _apply_field(self, key: str) -> None:
        if key in AGENT_FIELD_KEYS:
            self._apply_agent_field(key)
        else:
            self._apply_risk_field(key)

    def _apply_risk_field(self, key: str) -> None:
        """Tighten-only: the writers clamp to the floor and we report what stuck."""
        from abcxauto.config import update_capacity_config, update_risk_config

        raw = str((self.fields.get(key) or ft.TextField()).value or "").strip()
        try:
            typed = float(raw)
        except (TypeError, ValueError):
            self._note_setting(f"{key} needs a number", color=AMBER)
            self._safe_update()
            return
        try:
            if key == "max_open_positions":
                update_capacity_config(max_open_positions=int(typed), persist=True)
            else:
                update_risk_config(**{key: typed}, persist=True)
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
            self._safe_update()
            return
        self._dirty.discard(key)
        now = getattr(get_config(), key, None)
        if isinstance(now, (int, float)) and abs(float(now) - typed) > 1e-9:
            self._note_setting(
                f"{key} clamped to {now:g} — walk-away floor", color=AMBER
            )
        else:
            self._note_setting(f"{key} → {typed:g}")
        self._sync_risk_page(force=True)
        self._safe_update()

    def _apply_agent_field(self, key: str) -> None:
        from abcxauto.config import set_agent_knobs

        raw = str((self.fields.get(key) or ft.TextField()).value or "").strip()
        try:
            res = set_agent_knobs({key: raw}, persist=True)
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
            self._safe_update()
            return
        self._dirty.discard(key)
        self._report_agent_result(key, res)
        self._sync_settings_page(force=True)
        self._safe_update()

    def _apply_agent_switch(self, key: str) -> None:
        from abcxauto.config import set_agent_knobs

        sw = self.gates.get(key)
        want = bool(getattr(sw, "value", True))
        try:
            res = set_agent_knobs({key: want}, persist=True)
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
            self._safe_update()
            return
        self._report_agent_result(key, res)
        self._sync_settings_page(force=True)
        self._safe_update()

    def _report_agent_result(self, key: str, res: dict) -> None:
        res = res if isinstance(res, dict) else {}
        why = (res.get("rejected") or {}).get(key)
        if why:
            self._note_setting(f"{key} refused: {why}", color=RED)
            return
        note = (res.get("clamped") or {}).get(key)
        if isinstance(note, dict):
            self._note_setting(
                f"{key} clamped to {note.get('clamped')} (asked {note.get('raw')})",
                color=AMBER,
            )
            return
        value = (res.get("applied") or {}).get(key)
        tail = " — next look" if key in (
            "model",
            "model_rth",
            "model_research",
            "model_params",
            "model_params_rth",
            "model_params_research",
            "temperature",
            "max_tokens",
        ) else ""
        self._note_setting(f"{key} → {value}{tail}")

    def _toggle_floor_gate(self, key: str) -> None:
        """Live floor gates stay armed. Paper may turn risk_gates_enabled off."""
        from abcxauto.config import update_risk_config

        sw = self.gates.get(key)
        if sw is None:
            return
        cfg = get_config()
        paper = bool(getattr(cfg, "is_paper", True)) and str(
            getattr(cfg, "trading_mode", "paper") or ""
        ).lower() != "live"
        if key == "risk_gates_enabled" and paper:
            want = bool(sw.value)
            try:
                update_risk_config(risk_gates_enabled=want, persist=True)
                self._note_setting(f"risk_gates_enabled → {want}")
            except Exception as exc:
                self._note_setting(f"{key} failed: {exc}", color=RED)
            self._sync_risk_page(force=True)
            self._safe_update()
            return
        if not bool(sw.value):
            sw.value = True
            self._note_setting(
                f"{key} is the walk-away floor — Pro cannot turn it off", color=AMBER
            )
            self._safe_update()
            return
        try:
            update_risk_config(**{key: True}, persist=True)
            self._note_setting(f"{key} armed")
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
        self._sync_risk_page(force=True)
        self._safe_update()

    def _toggle_trading_mode(self, _=None) -> None:
        if get_config().is_paper:
            self._open_live_confirm_dialog()
            return
        try:
            self.engine.switch_trading_mode("paper")
            self._after_mode_change()
        except Exception as exc:
            self._toast(str(exc), color=RED)
            self._safe_update()

    def _toggle_sizing_floors(self, _=None) -> None:
        """Paper two-way toggle of clerk ``sizing_floors``. Live paints ON and ignores clicks."""
        cfg = get_config()
        if not cfg.is_paper or str(cfg.trading_mode or "").lower() == "live":
            self._sync_floors_chip()
            self._toast("Live: Floors on (forced)", color=AMBER)
            self._safe_update()
            return
        try:
            from abcxauto.config import update_risk_config
            from abcxauto.risk_gates import sizing_floors_active

            next_on = not sizing_floors_active(cfg)
            update_risk_config(sizing_floors=next_on, persist=True)
            self._sync_floors_chip()
            self._toast(
                f"Floors → {'on' if next_on else 'off'}",
                color=GREEN if next_on else AMBER,
            )
            self._safe_update()
        except Exception as exc:
            self._sync_floors_chip()
            self._toast(f"Floors toggle failed: {exc}", color=RED)
            self._safe_update()

    def _open_live_confirm_dialog(self) -> None:
        self.tf_live_confirm.value = ""

        def _cancel(_=None) -> None:
            dlg.open = False
            self._safe_update()

        def _confirm(_=None) -> None:
            phrase = str(self.tf_live_confirm.value or "").strip()
            try:
                self.engine.switch_trading_mode("live", live_confirm=phrase)
                dlg.open = False
                self._after_mode_change()
            except Exception as exc:
                self._toast(str(exc), color=RED)
                self._safe_update()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            title=ft.Text("Switch to Live?", color=TEXT),
            content=ft.Column(
                [
                    ft.Text("Real-money mode. Type the exact confirm phrase:", size=13, color=MUTED),
                    ft.Text(LIVE_CONFIRM_PHRASE, size=12, color=TEXT, selectable=True),
                    self.tf_live_confirm,
                ],
                tight=True,
                spacing=12,
                width=360,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_cancel),
                ft.TextButton("Switch to Live", on_click=_confirm),
            ],
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _after_mode_change(self) -> None:
        self._sync_ibkr_account_label()
        self._toast(f"Mode → {self.lbl_account_mode.value}", color=BLUE)
        self._safe_update()

    def _stream_line(self, raw: str, kind: str, mode: str) -> ft.Control:
        color: str = TEXT
        weight: Any = None
        if kind == "banner":
            if "CLERK" in raw:
                color, weight = MUTED, ft.FontWeight.BOLD
            else:
                color, weight = BLUE, ft.FontWeight.BOLD
        elif kind == "clerk":
            color, weight = MUTED, ft.FontWeight.W_600
        elif kind == "send":
            color, weight = GREEN, ft.FontWeight.BOLD
        elif kind == "tool":
            color, weight = BLUE, ft.FontWeight.W_600
        elif kind == "alarm":
            color, weight = RED, ft.FontWeight.BOLD
        elif kind in ("warn", "poke"):
            color, weight = AMBER, ft.FontWeight.W_600
        elif kind == "think":
            color, weight = MUTED, ft.FontWeight.W_600
        elif kind == "say":
            color, weight = BLUE, ft.FontWeight.W_600
        elif kind in ("cached", "scan", "json"):
            color = MUTED
        elif mode == "think":
            color = MUTED
        line = ft.Text(
            raw,
            size=STREAM_FONT_SIZE,
            color=color,
            weight=weight,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        if kind != "banner":
            return line
        # A look boundary is the thing the operator scans for — rule it off.
        return ft.Container(
            content=line,
            padding=ft.Padding.only(top=10, bottom=4),
            border=ft.Border(top=ft.BorderSide(1, BORDER)),
        )

    def _scan_inline(self) -> ft.Control:
        """The screen that scan pulled, at the look where it was pulled."""
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            content=ft.Column(
                [
                    ft.Container(
                        content=self.lbl_scan_head,
                        ink=True,
                        tooltip="Show / hide the screen this scan pulled",
                        on_click=self._toggle_scan_inline,
                    ),
                    self.col_scan,
                ],
                spacing=6,
                tight=True,
            ),
        )

    def _toggle_scan_inline(self, _=None) -> None:
        self._scan_inline_open = not getattr(self, "_scan_inline_open", True)
        self.col_scan.visible = self._scan_inline_open
        self._safe_update()

    def _toggle_stream_follow(self, _=None) -> None:
        """Reading back must not be yanked to the tail. The chip is the way home."""
        follow = not getattr(self, "_stream_follow", True)
        self._stream_follow = follow
        self.think_scroll.auto_scroll = follow
        self.lbl_stream_follow.value = "live" if follow else "jump to live"
        self.lbl_stream_follow.color = GREEN if follow else AMBER
        self.btn_stream_follow.border = ft.Border.all(1, GREEN if follow else AMBER)
        self._safe_update()

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
