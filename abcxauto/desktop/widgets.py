"""Shared chrome, fields, and blotter widgets for the Pro cockpit."""
from __future__ import annotations

import json
from typing import Any

import flet as ft

from abcxauto.desktop.tokens import (
    AMBER,
    ASIDE_W,
    BG,
    BLUE,
    BORDER,
    CARD_STATUS_COLOR,
    GREEN,
    HOVER,
    LOGO_SRC,
    MUTED,
    NAV,
    NAV_SUBTITLES,
    NAV_TITLES,
    RAIL_BTN_W,
    RAIL_W,
    RED,
    RISK_FIELDS,
    STREAM_FONT_SIZE,
    SURFACE,
    TEXT,
    WHITE,
)


class WidgetsMixin:

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
        # ---- Look cost: last look $ first, session total next, curve below.
        self.lbl_look_cost = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=MUTED)
        self.lbl_look_cost_sub = ft.Text("no look cost yet", size=11, color=MUTED)
        self.lbl_look_cost_session = ft.Text("session —", size=13, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_look_cost_detail = ft.Text("", size=12, color=MUTED, selectable=True, no_wrap=False)
        self.lbl_look_curve = ft.Text("", size=12, color=MUTED, selectable=True, no_wrap=False)
        self.col_look_recent = ft.Column(spacing=3, tight=True)
        self._look_meter_rows: list[dict] = []
        self._look_meter_last = 0.0
        # ---- Defined-risk concentration fact (dollars / % NL). Missing import is idle.
        self.lbl_conc_head = ft.Text("", size=12, color=MUTED, selectable=True, no_wrap=False)
        self.col_conc = ft.Column(spacing=3, tight=True)
        self._conc_key = ""
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
        self.lbl_sc_slip = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=MUTED)
        self.lbl_sc_slip_sub = ft.Text("no fills this session", size=11, color=MUTED)
        self.lbl_sc_spend = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=MUTED)
        self.lbl_sc_spend_sub = ft.Text("no model spend this session", size=11, color=MUTED)
        self._desk_stats: dict = {}
        self._desk_stats_last = 0.0
        self._desk_stats_inflight = False
        self._desk_stats_force = False
        # ---- Risk page
        self.lbl_risk_glance = ft.Text("", size=12, color=MUTED, selectable=True, no_wrap=False)
        self.col_risk_knobs = ft.Column(spacing=3, tight=True)
        self.lbl_risk_gates = ft.Text(
            "session 0 · day 0", size=12, color=MUTED, selectable=True
        )
        self.col_risk_gates = ft.Column(
            [
                ft.Text(
                    "No gate rejections this session or today.",
                    size=12,
                    color=MUTED,
                )
            ],
            spacing=3,
            tight=True,
        )
        self.lbl_risk_posture = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=TEXT)
        self.lbl_risk_floors = ft.Text("", size=12, color=MUTED, selectable=True)
        self.sw_size_floors = ft.Switch(
            value=False,
            active_color=GREEN,
            on_change=self._toggle_sizing_floors,
        )
        self.lbl_risk_halt_state = ft.Text("", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_risk_halt_math = ft.Text("", size=12, color=MUTED, selectable=True)
        self.btn_flatten = self._btn(
            "Flatten All", outlined=True, on_click=self._open_flatten_confirm_dialog
        )
        self._flatten_waiting = False
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
        self.lbl_dash_tools.max_lines = 1
        self.lbl_dash_tools.overflow = ft.TextOverflow.ELLIPSIS
        # Facts the Cockpit computes; the Dashboard stays a live look, not a report.
        # Mode repeats the rail's Paper/Live pill. tools / focus / pace live in
        # the stream and the next-look line. Open MTM is on the
        # Account card next to Today — not hidden.
        self._hidden_metrics = ft.Column(
            [
                self.lbl_session_score,
                self.lbl_path,
                self.lbl_mix,
                self.lbl_why,
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


    def _notebook_card(self, card: dict, attrib: dict | None = None) -> ft.Control:
        """Journal slug only — id / label / n. Persist catalog fields stay off."""
        _ = attrib
        label = str(
            card.get("label") or card.get("name") or card.get("id") or "?"
        ).strip() or "?"
        cid = str(card.get("id") or "").strip()
        status = str(card.get("status") or "").strip().lower()
        color = CARD_STATUS_COLOR.get(status, MUTED)
        head: list[ft.Control] = [
            ft.Text(
                label,
                size=13,
                weight=ft.FontWeight.BOLD,
                color=TEXT,
                expand=True,
            ),
        ]
        if cid and cid != label:
            head.append(self._chip(cid, MUTED))
        if status:
            head.append(self._chip(status, color))
        n = card.get("n")
        if isinstance(n, (int, float)):
            head.append(self._chip(f"n={int(n)}", MUTED))
        return ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, color if status == "live" else BORDER),
            border_radius=10,
            padding=12,
            content=ft.Column([ft.Row(head, spacing=6)], spacing=6, tight=True),
        )


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
