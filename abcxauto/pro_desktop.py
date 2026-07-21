"""ABCXAUTO Pro Desktop — slim Flet cockpit over ProEngine."""

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

from abcxauto.config import (
    RISK_POSTURES,
    apply_risk_posture,
    get_config,
    load_risk_settings,
    risk_config_snapshot,
    risk_envelope_snapshot,
    risk_settings_path,
    setup_file_logging,
    update_controls_config,
    update_risk_config,
)
from abcxauto.broker.connection import LIVE_CONFIRM_PHRASE
from abcxauto.memory import get_journal
from abcxauto.pro_engine import ProEngine
from abcxauto.reality_pulse import build_reality_pulse, pulse_clock_view

logger = logging.getLogger(__name__)

# X Lights Out palette — look only; nav/controls are product labels
BG = "#000000"
SURFACE = "#16181c"
HOVER = "#181818"
BORDER = "#2f3336"
TEXT = "#e7e9ea"
MUTED = "#71767b"
GREEN = "#00ba7c"
RED = "#f4212e"
LIKE = "#f91880"
BLUE = "#1d9bf0"
AMBER = "#ffd400"
WHITE = "#ffffff"
CARD, CARD2 = BG, SURFACE

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_SRC = "abcxauto_logo.png"

# key, label, outlined icon, filled icon
NAV = [
    ("overview", "Dashboard", ft.Icons.DASHBOARD_OUTLINED, ft.Icons.DASHBOARD),
    ("positions", "Positions", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, ft.Icons.ACCOUNT_BALANCE_WALLET),
    ("controls", "Controls", ft.Icons.TUNE_OUTLINED, ft.Icons.TUNE),
    ("universe", "Universe", ft.Icons.PUBLIC_OUTLINED, ft.Icons.PUBLIC),
    ("risk", "Risk", ft.Icons.SHIELD_OUTLINED, ft.Icons.SHIELD),
    ("scorecard", "Scorecard", ft.Icons.BAR_CHART_OUTLINED, ft.Icons.BAR_CHART),
    ("suite", "Test Suite", ft.Icons.SCIENCE_OUTLINED, ft.Icons.SCIENCE),
]
_SCORECARD_REFRESH_S = 3.0
TITLE = "ABCXAUTO Pro v0.4"
PRO_TITLE = TITLE

class ProTerminal:
    def __init__(self, page: ft.Page):
        self.page = page
        self.engine = ProEngine()
        self.tab = "overview"
        self.lbl_mode = ft.Text("Safe", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self._build_refs()
        self._sync_widgets()

    def _build_refs(self) -> None:
        self.lbl_equity = ft.Text("$0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_pnl = ft.Text("$+0.00", size=14, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_ret_1w = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_3m = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_1y = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_source = ft.Text("IBKR NAV — building history…", size=10, color=MUTED)
        self._ret_cache: dict | None = None
        self._ret_last_fetch = 0.0
        self.news_list = ft.Column(spacing=0, tight=True)
        self._news_last_fetch = 0.0
        self._news_cache: list[dict] = []
        self.dot_conn = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.dot_xai = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.dot_mda = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.lbl_ibkr_status = ft.Text("Disconnected", size=12, color=MUTED)
        self.lbl_xai_status = ft.Text("Missing key", size=12, color=MUTED)
        self.lbl_mda_status = ft.Text("Not configured", size=12, color=MUTED)
        self.lbl_status = ft.Text("Safe", color=TEXT, size=13, weight=ft.FontWeight.W_600)
        self.lbl_cycles = ft.Text("0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_risk = ft.Text("—", size=16, weight=ft.FontWeight.W_600, color=TEXT)
        self.lbl_book_netliq = ft.Text("$0", size=18, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_book_pnl = ft.Text("$+0.00", size=18, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_book_unprotected = ft.Text("0", size=18, weight=ft.FontWeight.BOLD, color=GREEN)
        self.lbl_book_decision = ft.Text("—", size=18, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_book_halt = ft.Text("clear", size=18, weight=ft.FontWeight.BOLD, color=GREEN)
        cols = ("conId", "Symbol", "Type", "Qty", "uPnL", "Details")
        self.pos_table = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(c, color=MUTED, size=13)) for c in cols],
            rows=[],
            heading_row_color=BG,
            border=ft.Border.all(1, BORDER),
            data_row_min_height=44,
        )
        self.lbl_pos_summary = ft.Text("No open positions", color=TEXT, size=14, selectable=True)
        self.lbl_working_orders = ft.Text(
            "No working orders", color=MUTED, size=12, selectable=True
        )
        self.lbl_recent_fills = ft.Text(
            "No fills this session", color=MUTED, size=12, selectable=True
        )
        self.lbl_dash_pace = ft.Text("Pace: —", size=13, color=MUTED, selectable=True)
        self.lbl_dash_attention = ft.Text(
            "Attention: —", size=12, color=MUTED, selectable=True
        )
        self.lbl_proposal = ft.Text(
            "No proposal yet — Start agent or wait for a cycle.",
            color=TEXT, size=13, selectable=True,
        )
        self.lbl_agent_now = ft.Text(
            "Waiting for first cycle…",
            size=18,
            weight=ft.FontWeight.BOLD,
            color=MUTED,
            selectable=True,
        )
        self.lbl_agent_posture = ft.Text("Posture: —", size=12, color=MUTED, selectable=True)
        self.lbl_agent_world = ft.Text(
            "World: — (built each cycle from book / news / opportunities)",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.lbl_agent_judgment = ft.Text(
            "Judgment: — (stance / thesis / dismissed after Judge)",
            size=13,
            color=MUTED,
            selectable=True,
        )
        self.lbl_agent_read = ft.Text(
            "Focus: — (appears after the next Judge cycle)",
            size=13,
            color=MUTED,
            selectable=True,
        )
        self.lbl_agent_news = ft.Text(
            "News (World): (none yet)",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.lbl_agent_opps = ft.Text(
            "Features (heuristic): (none yet)",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.lbl_agent_params = ft.Text("Params: —", size=12, color=MUTED, selectable=True)
        self.lbl_agent_structure = ft.Text(
            "Structure grade: —", size=12, color=MUTED, selectable=True
        )
        self.lbl_agent_plan = ft.Text("Trade plan: —", size=12, color=MUTED, selectable=True)
        self.lbl_ledger_snippet = ft.Text("—", color=MUTED, size=11, selectable=True)
        self.lbl_cycle_log = ft.Text(
            "No activity yet — Connect IBKR, then Start agent",
            color=MUTED,
            size=12,
            selectable=True,
        )
        self.lbl_clock = ft.Text("—", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_session_badge = ft.Text("—", size=11, weight=ft.FontWeight.W_600, color=AMBER)
        self.lbl_countdown_title = ft.Text("Close time", size=11, color=MUTED)
        self.lbl_countdown = ft.Text("—", size=11, color=TEXT)
        self.lbl_data_age = ft.Text("n/a", size=11, color=TEXT)
        self.lbl_mandate_health = ft.Text(
            "green — protected", size=11, weight=ft.FontWeight.W_600, color=GREEN
        )
        self.lbl_pulse_narrative = ft.Text(
            "Reality Pulse idle — Start for live awareness.", size=13, color=TEXT, selectable=True
        )
        self.lbl_order_suite = ft.Text(
            "Order suite idle — open Test Suite to re-test types.", size=12, color=MUTED, selectable=True
        )
        self.lbl_risk_halt = ft.Text("Halt: clear", size=14, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_risk_status = ft.Text(
            "Set posture — wide envelope; agent sizes per trade (set_risk).",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.lbl_risk_envelope = ft.Text("", size=12, color=MUTED, selectable=True)
        self.risk_dd_posture = ft.Dropdown(
            label="Capacity posture",
            value="balanced",
            options=[
                ft.dropdown.Option("defensive", "Defensive"),
                ft.dropdown.Option("balanced", "Balanced"),
                ft.dropdown.Option("aggressive", "Aggressive"),
            ],
            width=280,
            text_size=13,
            color=TEXT,
            label_style=ft.TextStyle(color=MUTED, size=12),
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
        )
        self.risk_sw_gates = ft.Switch(value=True, active_color=BLUE)
        self.risk_sw_auto_panic = ft.Switch(value=True, active_color=BLUE)
        self.risk_sw_defined = ft.Switch(value=False, active_color=BLUE)
        self.risk_sw_cash = ft.Switch(value=False, active_color=BLUE)
        # Risk capital sliders (% of NetLiq / drawdown).
        self.risk_labels: dict[str, ft.Text] = {}
        self.risk_sliders: dict[str, ft.Slider] = {}
        for key, title, left, right, vmax in (
            ("max_risk_per_trade_pct", "Max risk / trade % NL", "0.25%", "6%", 6.0),
            ("daily_loss_limit_pct", "Daily loss limit % NL", "1%", "15%", 15.0),
            ("max_position_pct", "Max position % NL", "2%", "35%", 35.0),
            ("max_peak_drawdown_pct", "Peak drawdown %", "0=off", "35%", 35.0),
            ("max_option_premium_pct", "Max option premium %", "0=off", "12%", 12.0),
        ):
            val_lbl = ft.Text("0", size=13, weight=ft.FontWeight.W_600, color=TEXT, width=44)
            self.risk_labels[key] = val_lbl

            def _on_risk(e, k=key, lbl=val_lbl):
                v = round(float(e.control.value or 0), 2)
                lbl.value = f"{v:g}"
                try:
                    self._safe_update()
                except Exception:
                    pass

            self.risk_sliders[key] = ft.Slider(
                min=0,
                max=vmax,
                divisions=max(1, int(vmax * 4)),
                value=0,
                label="{value}",
                active_color=BLUE,
                inactive_color=BORDER,
                on_change=_on_risk,
                expand=True,
            )
            self.risk_sliders[key].data = (title, left, right)
        # Controls dials — attention + toolbox + book capacity.
        self.control_labels: dict[str, ft.Text] = {}
        self.control_sliders: dict[str, ft.Slider] = {}
        for key, title, left, right in (
            (
                "control_deliberation_pct",
                "Deliberation (System 1 ↔ System 2)",
                "S1 lean / quiet when protected",
                "S2 mega-worker / require Act",
            ),
            (
                "control_budget_pct",
                "Intelligence budget",
                "protect API $",
                "more frequent Grok",
            ),
            (
                "control_frequency_pct",
                "Trade frequency",
                "patient — few entries / quality",
                "higher rate OK — process/streams",
            ),
            (
                "control_rotation_pct",
                "Capital rotation",
                "hold protected book OK",
                "redeploy / free cash for better setups",
            ),
            (
                "control_complexity_pct",
                "Structure complexity",
                "stock brackets / exits only",
                "full multi-leg toolbox",
            ),
        ):
            val_lbl = ft.Text("50", size=13, weight=ft.FontWeight.W_600, color=TEXT, width=36)
            self.control_labels[key] = val_lbl

            def _on_change(e, k=key, lbl=val_lbl):
                v = int(round(float(e.control.value or 0)))
                lbl.value = str(v)
                try:
                    self._safe_update()
                except Exception:
                    pass

            self.control_sliders[key] = ft.Slider(
                min=0,
                max=100,
                divisions=20,
                value=50,
                label="{value}",
                active_color=BLUE,
                inactive_color=BORDER,
                on_change=_on_change,
                expand=True,
            )
            self.control_sliders[key].data = (title, left, right)
        self.capacity_label = ft.Text("6", size=13, weight=ft.FontWeight.W_600, color=TEXT, width=36)

        def _on_cap(e):
            self.capacity_label.value = str(int(round(float(e.control.value or 0))))
            try:
                self._safe_update()
            except Exception:
                pass

        self.capacity_slider = ft.Slider(
            min=0,
            max=25,
            divisions=25,
            value=6,
            label="{value}",
            active_color=BLUE,
            inactive_color=BORDER,
            on_change=_on_cap,
            expand=True,
        )
        # Universe sandbox checkboxes + legal-set browser
        from abcxauto.universe import ARENA_CATALOG, arena_checkbox_label, load_allowlist

        self.universe_checks: dict[str, ft.Checkbox] = {}
        for arena_id in ARENA_CATALOG:
            self.universe_checks[arena_id] = ft.Checkbox(
                label=arena_checkbox_label(arena_id),
                value=arena_id in (load_allowlist().get("enabled_arenas") or []),
                fill_color=BLUE,
            )
        self.universe_custom_tf = ft.TextField(
            label="Custom tickers (comma-separated)",
            value=",".join(load_allowlist().get("custom_symbols") or []),
            dense=True,
            text_size=13,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            expand=True,
        )
        self.universe_exclude_tf = ft.TextField(
            label="Exclude tickers",
            value=",".join(load_allowlist().get("exclude_symbols") or []),
            dense=True,
            text_size=13,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            expand=True,
        )
        self.universe_filter_tf = ft.TextField(
            label="Filter legal set",
            hint_text="symbol / arena / source — display only, not ranked",
            dense=True,
            text_size=13,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            expand=True,
            on_change=self._on_universe_filter,
        )
        self.lbl_universe_status = ft.Text("", size=12, color=MUTED, selectable=True)
        self.lbl_universe_hint = ft.Text(
            "Save keeps arenas. Refresh pulls IBKR membership (MDA seed if offline).",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.universe_legal_list = ft.Column(spacing=2, tight=True, scroll=ft.ScrollMode.AUTO)
        self.lbl_agent_universe = ft.Text(
            "Universe: —",
            size=12,
            color=MUTED,
            selectable=True,
        )
        # Hydrate from risk_settings.json immediately (not only when Risk tab opens).
        try:
            load_risk_settings()
            self._load_risk_form()
        except Exception:
            logger.exception("initial risk form load failed")
        self.suite_status: dict[str, ft.Text] = {}
        self.suite_filter = "all"  # all | stock | manage | options
        self.lbl_suite_chip = ft.Text("Suite: idle", size=12, color=MUTED)
        self._scorecard_last_refresh = 0.0
        self.lbl_sc_day = ft.Text("Today", size=12, color=MUTED)
        self.lbl_sc_proposals = ft.Text("0", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_sc_allowed = ft.Text("0", size=22, weight=ft.FontWeight.BOLD, color=GREEN)
        self.lbl_sc_rejected = ft.Text("0", size=22, weight=ft.FontWeight.BOLD, color=RED)
        self.lbl_sc_dispatch_ok = ft.Text("0", size=22, weight=ft.FontWeight.BOLD, color=GREEN)
        self.lbl_sc_dispatch_failed = ft.Text("0", size=22, weight=ft.FontWeight.BOLD, color=RED)
        self.lbl_sc_halts = ft.Text("0", size=22, weight=ft.FontWeight.BOLD, color=MUTED)
        self.lbl_sc_hold_trade = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_sc_netliq = ft.Text("—", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_sc_equity_empty = ft.Text(
            "No data yet — journal populates as the agent trades", color=MUTED, size=12
        )
        self.sc_equity_spark = ft.Container(height=56, expand=True)
        self.lbl_sc_agent_ret = ft.Text("—", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.sc_dispatch_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(c, color=MUTED, size=13))
                for c in ("Time", "Status", "Result")
            ],
            rows=[],
            heading_row_color=BG,
            border=ft.Border.all(1, BORDER),
            data_row_min_height=44,
        )
        self.brain_action = ft.Text("—", size=20, weight=ft.FontWeight.BOLD, color=TEXT)
        self.brain_rationale = ft.Text(
            "Start autonomous mode to see Grok decisions.", color=MUTED, size=13, selectable=True
        )
        self.content = ft.Container(expand=True, padding=0)
        self.sidebar_btns: dict[str, ft.Container] = {}
        self.sidebar_icons: dict[str, ft.Icon] = {}
        self.sidebar_labels: dict[str, ft.Text] = {}
        self.sidebar_icon_pair: dict[str, tuple] = {}
        self.lbl_center_title = ft.Text("Dashboard", size=20, weight=ft.FontWeight.BOLD, color=TEXT)
        self.dash_tab = "live"  # compat; Dashboard is a single surface now
        self.dash_tab_labels: dict[str, ft.Text] = {}
        self.dash_tab_bars: dict[str, ft.Container] = {}
        self.dash_tabs_row = ft.Container(visible=False, height=0)
        self.btn_run = self._btn("Start agent", WHITE, self._toggle_run)
        self.btn_run.tooltip = (
            "Start the Grok agent loop (connects IBKR if needed; requires XAI_API_KEY)"
        )
        self.btn_run.width = 228
        self.btn_connect = self._btn(
            "Connect IBKR", TEXT, self._toggle_connect, outlined=True
        )
        self.btn_connect.tooltip = "Connect to TWS/Gateway only — no agent cycles"
        self.btn_connect.width = 228
        self.btn_flatten = self._btn(
            "Close All Positions", TEXT, self._panic, outlined=True
        )
        self.btn_flatten.tooltip = "Close every open position on the connected account"
        self.btn_flatten.width = 228
        self.lbl_account_name = ft.Text(
            "IBKR", size=14, weight=ft.FontWeight.BOLD, color=TEXT
        )
        self.lbl_account_id = ft.Text("Not connected", size=13, color=MUTED)
        self.lbl_account_mode = ft.Text("Paper", size=12, weight=ft.FontWeight.W_600, color=GREEN)
        self.btn_account_mode = ft.Container(
            content=self.lbl_account_mode,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, BORDER),
            border_radius=999,
            ink=True,
            tooltip="Switch Paper / Live",
            on_click=self._toggle_trading_mode,
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
        self.lbl_account_model = ft.Text("Grok —", size=12, color=MUTED)

    def build(self) -> None:
        p, cfg = self.page, get_config()
        p.title = PRO_TITLE
        p.bgcolor = BG
        p.padding = 0
        p.theme_mode = ft.ThemeMode.DARK
        try:
            p.window.visible = True
            p.window.width = 1280
            p.window.height = 820
            p.window.min_width = 1000
            p.window.min_height = 700
        except Exception:
            pass
        self.lbl_account_model.value = f"Grok {getattr(cfg, 'model', '—')}"
        left = self._left_rail(cfg)
        center = self._center_column()
        right = self._right_rail()
        shell = ft.Row(
            [left, center, right],
            spacing=0,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        root = ft.Row(
            [
                ft.Container(expand=True),
                shell,
                ft.Container(expand=True),
            ],
            expand=True,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        try:
            p.controls.clear()
        except Exception:
            pass
        p.add(root)
        self._show_tab("overview")
        self._sync_widgets()
        p.update()
        p.run_task(self._poll_loop)
        p.run_task(self._clock_loop)
        p.run_task(self._reveal_window)
        if probe := os.environ.get("ABCXAUTO_UI_PROBE"):
            Path(probe).write_text(
                json.dumps({"title": p.title, "tab": self.tab, "ui_built": True,
                            "engine": "ProEngine"}, indent=2),
                encoding="utf-8",
            )

    def _avatar(self, letter: str = "A", size: int = 40) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=size // 2,
            bgcolor=SURFACE,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(letter, size=size // 2, weight=ft.FontWeight.BOLD, color=TEXT),
        )

    def _left_rail(self, cfg) -> ft.Container:
        nav_items = [self._nav_btn(k, label, outlined, filled)
                     for k, label, outlined, filled in NAV]
        self._apply_suite_nav_visibility()
        logo = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            content=ft.Row(
                [
                    ft.Image(
                        src=LOGO_SRC,
                        width=40,
                        height=40,
                        fit=ft.BoxFit.CONTAIN,
                        error_content=ft.Text("A", size=22, weight=ft.FontWeight.BOLD, color=BLUE),
                    ),
                    ft.Text("ABCXAUTO", size=18, weight=ft.FontWeight.BOLD, color=TEXT),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._sync_ibkr_account_label()
        self.account_bar = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border_radius=999,
            content=ft.Row(
                [
                    self._avatar("I", 40),
                    ft.Column(
                        [
                            self.lbl_account_name,
                            self.lbl_account_id,
                            self.btn_account_mode,
                        ],
                        spacing=4,
                        tight=True,
                        expand=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        rail_body = ft.Column(
            [
                logo,
                *nav_items,
                ft.Container(height=16),
                self.btn_connect,
                ft.Container(height=8),
                self.btn_run,
                ft.Container(height=8),
                self.btn_flatten,
                ft.Container(expand=True),
                self.account_bar,
            ],
            spacing=2,
            expand=True,
        )
        # No Stack overlay on the rail — accounts popup uses page.overlay so
        # Connect/Start stay clickable.
        return ft.Container(
            width=260,
            bgcolor=BG,
            padding=ft.Padding.only(left=8, right=12, top=4, bottom=12),
            content=rail_body,
        )

    def _rail_outline_btn(self, text: str, color: str, on_click) -> ft.Container:
        return ft.Container(
            content=ft.Text(
                text,
                size=14,
                weight=ft.FontWeight.BOLD,
                color=color,
                text_align=ft.TextAlign.CENTER,
            ),
            bgcolor=BG,
            border=ft.Border.all(1, BORDER if color == TEXT else color),
            border_radius=999,
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            alignment=ft.Alignment.CENTER,
            ink=True,
            on_click=on_click,
        )

    def _center_column(self) -> ft.Container:
        self.lbl_center_subtitle = ft.Text(
            "Live ops while the agent runs — facts only, shell does not rank.",
            size=12,
            color=MUTED,
        )
        header = ft.Container(
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=16, top=14, bottom=10),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [self.lbl_center_title, self.lbl_center_subtitle],
                spacing=4,
            ),
        )
        return ft.Container(
            width=600,
            bgcolor=BG,
            border=ft.Border(
                left=ft.BorderSide(1, BORDER),
                right=ft.BorderSide(1, BORDER),
            ),
            content=ft.Column(
                [header, self.content],
                spacing=0,
                expand=True,
            ),
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

        account_card = self._happen_card(
            "Account",
            ft.Column(
                [
                    ft.Text("Total value", size=12, color=MUTED),
                    self.lbl_equity,
                    ft.Row(
                        [
                            _ret_col("Today", self.lbl_pnl),
                            self.col_ret_1w,
                            self.col_ret_3m,
                            self.col_ret_1y,
                        ],
                        spacing=16,
                        wrap=True,
                    ),
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
                    ft.Row(
                        [self.lbl_countdown_title, self.lbl_countdown],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            ft.Text("IBKR refresh", size=11, color=MUTED),
                            self.lbl_data_age,
                        ],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            ft.Text("Risk posture", size=11, color=MUTED),
                            self.lbl_mandate_health,
                        ],
                        spacing=8,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
        )
        news_card = self._happen_card(
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
            width=320,
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=8, top=8, bottom=12),
            content=ft.Column(
                [
                    account_card,
                    ft.Container(height=12),
                    news_card,
                ],
                spacing=0,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
        )

    def _happen_card(self, title: str, body: ft.Control) -> ft.Container:
        return ft.Container(
            bgcolor=SURFACE,
            border_radius=16,
            padding=16,
            content=ft.Column(
                [
                    ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT),
                    body,
                ],
                spacing=10,
            ),
        )

    def _nav_btn(self, key: str, label: str, outlined, filled) -> ft.Container:
        ic = ft.Icon(outlined, size=26, color=TEXT)
        lab = ft.Text(label, size=20, color=TEXT, weight=ft.FontWeight.W_400)
        self.sidebar_icons[key] = ic
        self.sidebar_labels[key] = lab
        self.sidebar_icon_pair[key] = (outlined, filled)
        c = ft.Container(
            content=ft.Row([ic, lab], spacing=20),
            padding=ft.Padding.symmetric(horizontal=12, vertical=12),
            border_radius=999,
            ink=True,
            on_click=lambda e, k=key: self._show_tab(k),
        )
        self.sidebar_btns[key] = c
        return c

    def _show_tab(self, key: str) -> None:
        if key == "suite" and not get_config().is_paper:
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text("Test Suite is paper-only"), bgcolor=AMBER, open=True
                )
            )
            key = "overview"
        self.tab = key
        for k, btn in self.sidebar_btns.items():
            active = k == key
            btn.bgcolor = HOVER if active else None
            pair = self.sidebar_icon_pair.get(k)
            if k in self.sidebar_icons and pair:
                outlined, filled = pair
                self.sidebar_icons[k].icon = filled if active else outlined
                self.sidebar_icons[k].color = TEXT
            if k in self.sidebar_labels:
                self.sidebar_labels[k].weight = (
                    ft.FontWeight.BOLD if active else ft.FontWeight.W_400
                )
                self.sidebar_labels[k].color = TEXT
        titles = {
            "overview": "Dashboard",
            "positions": "Positions",
            "controls": "Controls",
            "universe": "Universe",
            "risk": "Risk",
            "scorecard": "Scorecard",
            "suite": "Test Suite",
        }
        subtitles = {
            "overview": "Live ops while the agent runs — facts only, shell does not rank.",
            "positions": "Book table + working orders + session fills.",
            "controls": "Attention + toolbox — disjoint from Risk and Universe.",
            "universe": "Scanner sandbox — legal names for hunt / scan_request.",
            "risk": "Capital survival — $/%% gates and halt.",
            "scorecard": "Forward-test journal — gates, equity, dispatches.",
            "suite": "Paper order gym — place/cancel mechanics.",
        }
        self.lbl_center_title.value = titles.get(key, "Dashboard")
        if hasattr(self, "lbl_center_subtitle"):
            self.lbl_center_subtitle.value = subtitles.get(key, "")
            self.lbl_center_subtitle.visible = bool(subtitles.get(key))
        builders = {
            "overview": self._page_overview,
            "positions": self._page_positions,
            "controls": self._page_controls,
            "universe": self._page_universe,
            "risk": self._page_risk,
            "scorecard": self._page_scorecard,
            "suite": self._page_suite,
        }
        if key in ("risk", "controls"):
            try:
                load_risk_settings()
                self._load_risk_form()
            except Exception:
                logger.exception("risk/controls form reload before nav failed")
        if key == "universe":
            try:
                self._load_universe_form()
            except Exception:
                logger.exception("universe form reload failed")
        self.content.content = builders.get(key, self._page_overview)()
        if key in ("risk", "controls"):
            self._load_risk_form()
        if key == "scorecard":
            self._refresh_scorecard(force=True)
        if key == "suite":
            self._refresh_suite_statuses()
        self._safe_update()

    def _set_dash_tab(self, key: str) -> None:
        """Compat no-op — Dashboard is a single live-ops surface."""
        self.dash_tab = key or "live"
        if self.tab == "overview":
            self.content.content = self._page_overview()
        self._safe_update()

    def _safe_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass

    def _section(self, title: str, *body: ft.Control) -> ft.Container:
        """Clean center panel — hairline separator, no social-post chrome."""
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT),
                    *body,
                ],
                spacing=8,
                tight=True,
            ),
        )

    def _section_header(self, title: str, on_refresh) -> ft.Row:
        """Title row with a circular refresh control."""
        return ft.Row(
            [
                ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT, expand=True),
                ft.Container(
                    width=32,
                    height=32,
                    border_radius=16,
                    border=ft.Border.all(1, BORDER),
                    bgcolor=SURFACE,
                    alignment=ft.Alignment.CENTER,
                    ink=True,
                    tooltip="Refresh",
                    on_click=on_refresh,
                    content=ft.Icon(ft.Icons.REFRESH, size=16, color=TEXT),
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _section_refresh(self, title: str, on_refresh, *body: ft.Control) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [self._section_header(title, on_refresh), *body],
                spacing=8,
                tight=True,
            ),
        )

    def _log_entry(self, title: str, *body: ft.Control, letter: str = "C") -> ft.Container:
        """Post-style row reserved for logging / activity feed."""
        return self._post(title, "@log", *body, letter=letter)

    def _post(
        self,
        name: str,
        handle: str,
        *body: ft.Control,
        letter: str = "A",
    ) -> ft.Container:
        """Post-style log row — avatar + title + body, hairline separator."""
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Row(
                [
                    self._avatar(letter, 40),
                    ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Text(name, size=15, weight=ft.FontWeight.BOLD, color=TEXT),
                                    ft.Text(handle, size=15, color=MUTED),
                                ],
                                spacing=6,
                            ),
                            *body,
                        ],
                        spacing=4,
                        expand=True,
                        tight=True,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

    def _stat_line(self, label: str, value: ft.Control) -> ft.Row:
        return ft.Row(
            [
                ft.Text(label, size=13, color=MUTED),
                value,
            ],
            spacing=8,
            wrap=True,
        )

    def _card(self, title: str, value: ft.Control) -> ft.Container:
        """Compact metric chip used inside timeline posts / scorecard."""
        return ft.Container(
            bgcolor=BG,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            content=ft.Column(
                [
                    ft.Text(title, size=12, color=MUTED, weight=ft.FontWeight.W_500),
                    value,
                ],
                spacing=2,
                tight=True,
            ),
        )

    def _panel(self, title: str, *controls: ft.Control) -> ft.Container:
        return self._section(title, *controls)

    def _btn(self, text: str, color: str, on_click, *, outlined: bool = False) -> ft.Button:
        if outlined:
            return ft.Button(
                text,
                bgcolor=BG,
                color=TEXT,
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=999),
                    side=ft.BorderSide(1, BORDER),
                    padding=ft.Padding.symmetric(horizontal=18, vertical=12),
                ),
                on_click=on_click,
            )
        fg = "#ffffff" if color in (BLUE, RED, GREEN, LIKE) else "#0f1419"
        if color in (AMBER, WHITE):
            fg = "#0f1419"
        return ft.Button(
            text,
            bgcolor=color,
            color=fg,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            ),
            on_click=on_click,
        )

    def _page_overview(self) -> ft.Column:
        return ft.Column(
            [self._dash_live()],
            spacing=0,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def _dash_live(self) -> ft.Column:
        """Single Dashboard — insight while the agent is running."""
        status = ft.Row(
            [
                self._card("Mode", self.lbl_mode),
                self._card("Cycles", self.lbl_cycles),
                self._card("Risk", self.lbl_risk),
                self._card("Halt", self.lbl_book_halt),
                self._card("Unprotected", self.lbl_book_unprotected),
                self._card("Day PnL", self.lbl_book_pnl),
            ],
            spacing=8,
            wrap=True,
        )
        return ft.Column(
            [
                self._section_refresh(
                    "Live",
                    self._refresh_agent_tab,
                    ft.Text(
                        "What matters this second: open risk, last cycle, pace. "
                        "Orders & fills → Positions. Shell does not rank.",
                        size=12,
                        color=MUTED,
                    ),
                    status,
                    self.lbl_dash_pace,
                    self.lbl_dash_attention,
                    self.lbl_pulse_narrative,
                ),
                self._section(
                    "Now",
                    self.lbl_agent_now,
                    self.lbl_agent_plan,
                    self.lbl_agent_structure,
                ),
                self._section(
                    "Last cycle",
                    self.lbl_agent_judgment,
                    self.lbl_agent_read,
                    self.brain_action,
                    self.brain_rationale,
                    self.lbl_proposal,
                    self.lbl_agent_params,
                ),
                self._section(
                    "Context",
                    self.lbl_agent_world,
                    self.lbl_agent_posture,
                    self.lbl_agent_universe,
                    self.lbl_book_decision,
                    self.lbl_agent_opps,
                ),
                self._section(
                    "Activity",
                    ft.Text(
                        "Newest first — JUDGE / ACT, blocks, fills, pace, connect.",
                        size=12,
                        color=MUTED,
                    ),
                    self.lbl_cycle_log,
                ),
            ],
            spacing=0,
        )

    def _dash_book(self) -> ft.Column:
        """Compat — blotter moved to Positions."""
        return self._dash_live()

    def _dash_agent(self) -> ft.Column:
        """Compat shim → live Dashboard."""
        return self._dash_live()

    def _dash_log(self) -> ft.Column:
        """Compat shim → live Dashboard."""
        return self._dash_live()

    def _refresh_book_tab(self, _=None) -> None:
        err = self.engine.request_snapshot()
        if err:
            self._toast(err, color=AMBER)
        else:
            self._toast("Refreshing book snapshot…", color=BLUE)
        self._sync_widgets()
        self._safe_update()

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
        try:
            self.engine._apply_open_risk(note=False)
        except Exception:
            pass
        if err:
            self._toast("Dashboard refreshed (broker snapshot unavailable)", color=AMBER)
        else:
            self._toast("Refreshing dashboard…", color=BLUE)
        self._sync_widgets()
        self._safe_update()

    def _refresh_log_tab(self, _=None) -> None:
        """Compat — same as dashboard refresh."""
        self._refresh_agent_tab(_)

    def _control_row(self, key: str) -> ft.Column:
        slider = self.control_sliders[key]
        title, left, right = slider.data
        return ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=TEXT),
                        self.control_labels[key],
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                slider,
                ft.Row(
                    [
                        ft.Text(f"0 · {left}", size=11, color=MUTED, expand=True),
                        ft.Text(f"{right} · 100", size=11, color=MUTED),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
            ],
            spacing=2,
            tight=True,
        )

    def _risk_row(self, key: str) -> ft.Column:
        slider = self.risk_sliders[key]
        title, left, right = slider.data
        return ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=TEXT),
                        self.risk_labels[key],
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                slider,
                ft.Row(
                    [
                        ft.Text(f"0 · {left}", size=11, color=MUTED, expand=True),
                        ft.Text(f"{right}", size=11, color=MUTED),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
            ],
            spacing=2,
            tight=True,
        )

    def _page_controls(self) -> ft.Column:
        dials = ft.Column(
            [
                ft.Text(
                    "How the agent works the book — attention, frequency, structures, slots. "
                    "Universe (scanner sandbox) is its own tab. Risk owns capital death knobs. "
                    "Goal: book return on startup cash > cost of the model.",
                    size=12,
                    color=MUTED,
                ),
                self._control_row("control_deliberation_pct"),
                self._control_row("control_budget_pct"),
                self._control_row("control_frequency_pct"),
                self._control_row("control_rotation_pct"),
                self._control_row("control_complexity_pct"),
                ft.Container(height=4),
                ft.Row(
                    [
                        ft.Text(
                            "Book capacity (max open positions)",
                            size=13,
                            weight=ft.FontWeight.W_600,
                            color=TEXT,
                        ),
                        self.capacity_label,
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                self.capacity_slider,
                ft.Row(
                    [
                        ft.Text("0 · unlimited gate off", size=11, color=MUTED, expand=True),
                        ft.Text("25 slots", size=11, color=MUTED),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Text(
                    "Capital rotation is process only — shell never auto-sells. "
                    "High + thin cash → Grok is authorized to trim/exit to free room.",
                    size=11,
                    color=MUTED,
                ),
                ft.Container(height=4),
                ft.Row(
                    [self._btn("Save controls", BLUE, self._save_controls)],
                    spacing=10,
                ),
            ],
            spacing=12,
            tight=True,
        )
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Text("Controls", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(
                                "Deliberation · Budget · Frequency · Rotation · Complexity · "
                                "Capacity. Disjoint from Risk and Universe.",
                                color=MUTED,
                                size=14,
                            ),
                        ],
                        spacing=6,
                    ),
                ),
                ft.Container(
                    padding=16,
                    expand=True,
                    content=ft.Column(
                        [
                            self.lbl_risk_status,
                            ft.Container(height=8),
                            self._section("Attention · toolbox · capacity", dials),
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=8,
                    ),
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _page_universe(self) -> ft.Column:
        from abcxauto.universe import ARENA_CATALOG

        groups: dict[str, list] = {}
        for arena_id, meta in ARENA_CATALOG.items():
            g = str(meta.get("group") or "other")
            groups.setdefault(g, []).append(self.universe_checks[arena_id])
        group_titles = {
            "caps": "Cap bands",
            "scans": "Live scans",
            "etfs": "Index ETFs",
            "commodities": "Commodities / macro",
            "industries": "Industries",
        }
        sections = []
        for g, checks in groups.items():
            sections.append(
                self._section(
                    group_titles.get(g, g.replace("_", " ").title()),
                    ft.Column(checks, spacing=2, tight=True),
                )
            )
        self._render_universe_legal_list()
        legal_panel = ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=12,
            padding=12,
            content=ft.Column(
                [
                    ft.Text(
                        "Legal set (arena / scan order — not ranked)",
                        size=13,
                        weight=ft.FontWeight.W_600,
                        color=TEXT,
                    ),
                    self.universe_filter_tf,
                    ft.Container(
                        height=260,
                        content=self.universe_legal_list,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )
        body = ft.Column(
            [
                ft.Text(
                    "Enable arenas → Refresh pulls membership from IBKR when connected. "
                    "MDA-seed arenas are honest static lists (not live IBKR industry scans). "
                    "Grok picks inside; shell never ranks.",
                    size=12,
                    color=MUTED,
                ),
                *sections,
                ft.Container(height=4),
                self.universe_custom_tf,
                self.universe_exclude_tf,
                self.lbl_universe_status,
                self.lbl_universe_hint,
                ft.Row(
                    [
                        self._btn("Save arenas", BLUE, self._save_universe),
                        self._btn("Refresh membership", GREEN, self._refresh_universe),
                    ],
                    spacing=10,
                ),
                legal_panel,
            ],
            spacing=10,
            tight=True,
        )
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Text("Universe", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(
                                "Scanner sandbox — which names are legal for hunt / scan_request.",
                                color=MUTED,
                                size=14,
                            ),
                        ],
                        spacing=6,
                    ),
                ),
                ft.Container(
                    padding=16,
                    expand=True,
                    content=ft.Column(
                        [body],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=8,
                    ),
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _page_risk(self) -> ft.Column:
        def _sw_row(label: str, sw: ft.Switch) -> ft.Row:
            return ft.Row(
                [ft.Text(label, size=13, color=TEXT, expand=True), sw],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )

        preset = ft.Column(
            [
                self.risk_dd_posture,
                ft.Text(
                    "Capital preset only — seeds Risk sliders. Never touches Controls "
                    "or Universe.",
                    size=12,
                    color=MUTED,
                ),
                ft.Row(
                    [self._btn("Apply capital preset", BLUE, self._apply_posture)],
                    spacing=10,
                ),
            ],
            spacing=8,
            tight=True,
        )
        knobs = ft.Column(
            [
                _sw_row("Pre-trade gates (halt latch)", self.risk_sw_gates),
                _sw_row("Auto-panic on daily-loss breach", self.risk_sw_auto_panic),
                _sw_row("Defined-risk options only", self.risk_sw_defined),
                _sw_row("Cash-only sizing (block SHORT stock)", self.risk_sw_cash),
                ft.Container(height=8),
                ft.Text(
                    "How the account can die — size, daily loss, drawdown. "
                    "Exits always bypass halt.",
                    size=12,
                    color=MUTED,
                ),
                *[self._risk_row(k) for k in self.risk_sliders],
                ft.Container(height=4),
                ft.Row(
                    [self._btn("Save risk", BLUE, self._save_risk_gates)],
                    spacing=10,
                ),
            ],
            spacing=10,
            tight=True,
        )
        halt_actions = ft.Row(
            [
                self._btn("Resume", GREEN, self._resume_halt, outlined=True),
                self._btn("Halt entries", RED, self._manual_halt, outlined=True),
            ],
            spacing=10,
            wrap=True,
        )
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Text("Risk", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(
                                "Capital survival gates and halt. Disjoint from Controls "
                                "and Universe.",
                                color=MUTED,
                                size=14,
                            ),
                        ],
                        spacing=6,
                    ),
                ),
                ft.Container(
                    padding=16,
                    expand=True,
                    content=ft.Column(
                        [
                            self.lbl_risk_halt,
                            self.lbl_risk_status,
                            self.lbl_risk_envelope,
                            ft.Container(height=8),
                            self._section("Capital preset", preset),
                            ft.Container(height=8),
                            self._section("Gates", knobs),
                            ft.Container(height=8),
                            self._section("Halt", halt_actions),
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=8,
                    ),
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _load_risk_form(self) -> None:
        snap = risk_config_snapshot(reload=True)
        posture = str(snap.get("risk_posture") or "").strip().lower()
        if posture not in RISK_POSTURES:
            posture = "balanced"
        self.risk_dd_posture.value = posture
        self.risk_dd_posture.options = [
            ft.dropdown.Option("defensive", "Defensive"),
            ft.dropdown.Option("balanced", "Balanced"),
            ft.dropdown.Option("aggressive", "Aggressive"),
        ]
        self.risk_sw_gates.value = bool(snap.get("risk_gates_enabled", True))
        self.risk_sw_auto_panic.value = bool(snap.get("auto_panic_on_breach", False))
        self.risk_sw_defined.value = bool(snap.get("defined_risk_only", False))
        self.risk_sw_cash.value = bool(snap.get("cash_only", False))
        for key, slider in self.risk_sliders.items():
            try:
                v = float(snap.get(key, 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            v = max(0.0, min(float(slider.max or 100), v))
            slider.value = v
            self.risk_labels[key].value = f"{v:g}"
        for key, slider in self.control_sliders.items():
            raw = snap.get(key, 50)
            if key == "control_complexity_pct" and raw is None:
                raw = snap.get("control_options_pct", 50)
            try:
                v = int(max(0, min(100, int(float(raw)))))
            except (TypeError, ValueError):
                v = 50
            slider.value = float(v)
            self.control_labels[key].value = str(v)
        try:
            cap = int(snap.get("max_open_positions", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        cap = max(0, min(25, cap))
        self.capacity_slider.value = float(cap)
        self.capacity_label.value = str(cap)
        self._sync_risk_envelope_label()
        self._sync_risk_halt_label()

    def _load_universe_form(self) -> None:
        from abcxauto.universe import (
            arena_checkbox_label,
            load_allowlist,
            universe_status_summary,
        )

        al = load_allowlist()
        enabled = set(al.get("enabled_arenas") or [])
        for arena_id, cb in self.universe_checks.items():
            cb.value = arena_id in enabled
            cb.label = arena_checkbox_label(arena_id)
        self.universe_custom_tf.value = ",".join(al.get("custom_symbols") or [])
        self.universe_exclude_tf.value = ",".join(al.get("exclude_symbols") or [])
        self.lbl_universe_status.value = universe_status_summary()
        self.lbl_universe_status.color = MUTED
        self._render_universe_legal_list()

    def _on_universe_filter(self, _=None) -> None:
        self._render_universe_legal_list()
        self._safe_update()

    def _render_universe_legal_list(self) -> None:
        from abcxauto.universe import membership_rows

        q = str(getattr(self.universe_filter_tf, "value", None) or "")
        rows = membership_rows(query=q)
        controls: list[ft.Control] = []
        if not rows:
            controls.append(
                ft.Text(
                    "No legal symbols yet — enable arenas and Refresh membership.",
                    size=12,
                    color=MUTED,
                )
            )
        else:
            # Header
            controls.append(
                ft.Row(
                    [
                        ft.Text("Symbol", size=11, color=MUTED, width=72, weight=ft.FontWeight.W_600),
                        ft.Text("Arena", size=11, color=MUTED, width=110, weight=ft.FontWeight.W_600),
                        ft.Text("Source", size=11, color=MUTED, expand=True, weight=ft.FontWeight.W_600),
                    ],
                    spacing=8,
                )
            )
            for r in rows[:200]:
                src = str(r.get("source") or "?")
                src_color = (
                    GREEN
                    if src == "ibkr"
                    else (BLUE if src in ("custom",) else MUTED)
                )
                controls.append(
                    ft.Row(
                        [
                            ft.Text(
                                str(r.get("symbol") or ""),
                                size=12,
                                color=TEXT,
                                width=72,
                                weight=ft.FontWeight.W_600,
                                selectable=True,
                            ),
                            ft.Text(
                                str(r.get("arena") or ""),
                                size=12,
                                color=MUTED,
                                width=110,
                                selectable=True,
                            ),
                            ft.Text(src, size=12, color=src_color, expand=True, selectable=True),
                        ],
                        spacing=8,
                    )
                )
            if len(rows) > 200:
                controls.append(
                    ft.Text(f"… {len(rows) - 200} more (filter to narrow)", size=11, color=MUTED)
                )
        self.universe_legal_list.controls = controls

    def _sync_risk_envelope_label(self) -> None:
        try:
            env = risk_envelope_snapshot()
            eff = env.get("effective_risk_posture") or "(none)"
            posture = env.get("risk_posture") or "(none)"
            clamp = " · live-clamped to balanced" if env.get("live_clamped") else ""
            path = risk_settings_path()
            self.lbl_risk_status.value = (
                f"Posture {posture} (effective {eff}){clamp} — "
                f"loaded from {path.name}"
            )
            self.lbl_risk_status.color = MUTED
            cur = env.get("current") or {}
            box = env.get("envelope") or {}
            if box:
                rt = box.get("max_risk_per_trade_pct") or {}
                self.lbl_risk_envelope.value = (
                    f"{path} · risk/trade {cur.get('max_risk_per_trade_pct')} "
                    f"[{rt.get('floor')}-{rt.get('ceil')}]; "
                    f"daily {cur.get('daily_loss_limit_pct')}; "
                    f"pos {cur.get('max_position_pct')}"
                )
            else:
                self.lbl_risk_envelope.value = (
                    f"{path} · apply a posture to enable agent set_risk."
                )
        except Exception:
            self.lbl_risk_envelope.value = ""

    def _sync_risk_halt_label(self) -> None:
        try:
            from abcxauto.risk_gates import get_risk_gate
            gate = get_risk_gate()
            if gate.is_halted:
                reason = (gate.halt_reason or "halted")[:120]
                self.lbl_risk_halt.value = f"Halt: {reason}"
                self.lbl_risk_halt.color = RED
            else:
                self.lbl_risk_halt.value = "Halt: clear"
                self.lbl_risk_halt.color = GREEN
        except Exception:
            self.lbl_risk_halt.value = "Halt: n/a"
            self.lbl_risk_halt.color = MUTED

    def _apply_posture(self, _=None) -> None:
        """Risk-tab capital preset — never touches Controls or Universe."""
        try:
            posture = str(self.risk_dd_posture.value or "balanced").strip().lower()
            apply_risk_posture(posture)
            path = risk_settings_path()
            self._load_risk_form()
            self.lbl_risk_status.value = (
                f"Capital preset {posture} applied → {path}"
            )
            self.lbl_risk_status.color = GREEN
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text(f"Capital preset {posture} saved"),
                    bgcolor=BLUE,
                    open=True,
                )
            )
        except Exception as e:
            self.lbl_risk_status.value = f"Preset apply failed: {e}"
            self.lbl_risk_status.color = RED
            try:
                self.page.overlay.append(
                    ft.SnackBar(ft.Text(f"Preset apply failed: {e}"), bgcolor=RED, open=True)
                )
            except Exception:
                pass
        self._safe_update()

    def _save_risk_gates(self, _=None) -> None:
        """Save Risk capital toggles + sliders only."""
        try:
            payload: dict[str, Any] = {
                "risk_gates_enabled": bool(self.risk_sw_gates.value),
                "auto_panic_on_breach": bool(self.risk_sw_auto_panic.value),
                "defined_risk_only": bool(self.risk_sw_defined.value),
                "cash_only": bool(self.risk_sw_cash.value),
            }
            posture = str(self.risk_dd_posture.value or "").strip().lower()
            if posture in RISK_POSTURES:
                payload["risk_posture"] = posture
            for key, slider in self.risk_sliders.items():
                payload[key] = float(slider.value or 0)
            update_risk_config(**payload)
            path = risk_settings_path()
            self._load_risk_form()
            self.lbl_risk_status.value = f"Risk gates saved → {path}"
            self.lbl_risk_status.color = GREEN
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text(f"Risk gates saved to {path.name}"),
                    bgcolor=BLUE,
                    open=True,
                )
            )
        except Exception as e:
            self.lbl_risk_status.value = f"Save risk failed: {e}"
            self.lbl_risk_status.color = RED
            try:
                self.page.overlay.append(
                    ft.SnackBar(ft.Text(f"Save risk failed: {e}"), bgcolor=RED, open=True)
                )
            except Exception:
                pass
        self._safe_update()

    def _save_controls(self, _=None) -> None:
        """Persist Controls dials + book capacity — never Risk capital keys."""
        try:
            payload = {
                key: int(round(float(slider.value or 50)))
                for key, slider in self.control_sliders.items()
            }
            payload["max_open_positions"] = int(
                round(float(self.capacity_slider.value or 0))
            )
            update_controls_config(**payload)
            path = risk_settings_path()
            self._load_risk_form()
            self.lbl_risk_status.value = f"Controls saved → {path}"
            self.lbl_risk_status.color = GREEN
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text(f"Controls saved to {path.name}"),
                    bgcolor=BLUE,
                    open=True,
                )
            )
        except Exception as e:
            self.lbl_risk_status.value = f"Save controls failed: {e}"
            self.lbl_risk_status.color = RED
            try:
                self.page.overlay.append(
                    ft.SnackBar(ft.Text(f"Save controls failed: {e}"), bgcolor=RED, open=True)
                )
            except Exception:
                pass
        self._safe_update()

    def _save_universe(self, _=None) -> None:
        try:
            from abcxauto.universe import normalize_symbols, save_allowlist

            enabled = [
                k for k, cb in self.universe_checks.items() if bool(cb.value)
            ]
            custom = normalize_symbols(
                [x.strip() for x in str(self.universe_custom_tf.value or "").split(",")]
            )
            exclude = normalize_symbols(
                [x.strip() for x in str(self.universe_exclude_tf.value or "").split(",")]
            )
            save_allowlist(
                {
                    "enabled_arenas": enabled,
                    "custom_symbols": custom,
                    "exclude_symbols": exclude,
                }
            )
            self._load_universe_form()
            self.lbl_universe_hint.value = (
                "Arenas saved. Legal set is stale until you Refresh membership "
                "(IBKR when connected)."
            )
            self.lbl_universe_hint.color = AMBER
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text("Arenas saved — Refresh membership to pull IBKR"),
                    bgcolor=BLUE,
                    open=True,
                )
            )
        except Exception as e:
            self.lbl_universe_status.value = f"Save universe failed: {e}"
            try:
                self.page.overlay.append(
                    ft.SnackBar(ft.Text(f"Save failed: {e}"), bgcolor=RED, open=True)
                )
            except Exception:
                pass
        self._safe_update()

    def _refresh_universe(self, _=None) -> None:
        async def _run():
            try:
                from abcxauto.universe import normalize_symbols, refresh_legal_set, save_allowlist

                enabled = [
                    k for k, cb in self.universe_checks.items() if bool(cb.value)
                ]
                custom = normalize_symbols(
                    [
                        x.strip()
                        for x in str(self.universe_custom_tf.value or "").split(",")
                    ]
                )
                exclude = normalize_symbols(
                    [
                        x.strip()
                        for x in str(self.universe_exclude_tf.value or "").split(",")
                    ]
                )
                save_allowlist(
                    {
                        "enabled_arenas": enabled,
                        "custom_symbols": custom,
                        "exclude_symbols": exclude,
                    }
                )
                conn = getattr(self.engine, "conn", None)
                if conn is None:
                    try:
                        from abcxauto.broker.connector import get_ibkr_connector

                        conn = get_ibkr_connector()
                    except Exception:
                        conn = None
                al = await refresh_legal_set(conn, persist=True)
                self._load_universe_form()
                connected = bool(getattr(conn, "connected", False))
                self.lbl_universe_hint.value = (
                    "Membership refreshed from IBKR."
                    if connected and "ibkr" in str(al.get("source") or "")
                    else "Membership refreshed (MDA seed / offline path — Connect for IBKR pulls)."
                )
                self.lbl_universe_hint.color = GREEN if connected else AMBER
                self.page.overlay.append(
                    ft.SnackBar(
                        ft.Text(
                            f"Legal set · {len(al.get('legal_symbols') or [])} symbols"
                        ),
                        bgcolor=GREEN,
                        open=True,
                    )
                )
            except Exception as e:
                self.lbl_universe_status.value = f"Refresh failed: {e}"
                try:
                    self.page.overlay.append(
                        ft.SnackBar(ft.Text(f"Refresh failed: {e}"), bgcolor=RED, open=True)
                    )
                except Exception:
                    pass
            self._safe_update()

        try:
            self.page.run_task(_run)
        except Exception:
            asyncio.get_event_loop().create_task(_run())

    # Back-compat alias if anything still wires the old handler.
    def _apply_risk(self, _=None) -> None:
        self._apply_posture(_)

    def _resume_halt(self, _=None) -> None:
        try:
            from abcxauto.risk_gates import get_risk_gate
            get_risk_gate().resume()
            self.lbl_risk_status.value = "Trading resumed (halt cleared)."
            self.lbl_risk_status.color = GREEN
        except Exception as e:
            self.lbl_risk_status.value = f"Resume failed: {e}"
            self.lbl_risk_status.color = RED
        self._sync_risk_halt_label()
        self._safe_update()

    def _manual_halt(self, _=None) -> None:
        try:
            from abcxauto.risk_gates import get_risk_gate
            get_risk_gate().halt("manual halt from Risk tab", kind="halt")
            self.lbl_risk_status.value = "New entries halted."
            self.lbl_risk_status.color = AMBER
        except Exception as e:
            self.lbl_risk_status.value = f"Halt failed: {e}"
            self.lbl_risk_status.color = RED
        self._sync_risk_halt_label()
        self._safe_update()

    def _page_positions(self) -> ft.Column:
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text(
                                                "Positions",
                                                size=20,
                                                weight=ft.FontWeight.BOLD,
                                                color=TEXT,
                                            ),
                                            ft.Text(
                                                "Book + order blotter — conId is the source of truth.",
                                                color=MUTED,
                                                size=14,
                                            ),
                                        ],
                                        spacing=4,
                                        expand=True,
                                    ),
                                    ft.Container(
                                        width=32,
                                        height=32,
                                        border_radius=16,
                                        border=ft.Border.all(1, BORDER),
                                        bgcolor=SURFACE,
                                        alignment=ft.Alignment.CENTER,
                                        ink=True,
                                        tooltip="Refresh blotter",
                                        on_click=self._refresh_book_tab,
                                        content=ft.Icon(ft.Icons.REFRESH, size=16, color=TEXT),
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        spacing=6,
                    ),
                ),
                ft.Container(
                    padding=12,
                    expand=True,
                    content=ft.Column(
                        [
                            self.lbl_pos_summary,
                            self.pos_table,
                            ft.Divider(color=BORDER, height=1),
                            ft.Text(
                                "Working orders",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT,
                            ),
                            self.lbl_working_orders,
                            ft.Container(height=8),
                            ft.Text(
                                "Session fills",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT,
                            ),
                            self.lbl_recent_fills,
                            ft.Divider(color=BORDER, height=1),
                            ft.Text("Ledger", size=15, weight=ft.FontWeight.BOLD, color=TEXT),
                            self.lbl_ledger_snippet,
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=8,
                    ),
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _page_scorecard(self) -> ft.Column:
        today = ft.Row(
            [
                self._card("Proposals", self.lbl_sc_proposals),
                self._card("Allowed", self.lbl_sc_allowed),
                self._card("Rejected", self.lbl_sc_rejected),
                self._card("Dispatched OK", self.lbl_sc_dispatch_ok),
                self._card("Failed", self.lbl_sc_dispatch_failed),
                self._card("Halts", self.lbl_sc_halts),
                self._card("Hold vs trade", self.lbl_sc_hold_trade),
            ],
            spacing=8,
            wrap=True,
        )
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Text("Scorecard", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(
                                "Forward-test journal — daily gates, equity, and recent dispatches.",
                                color=MUTED,
                                size=14,
                            ),
                            self.lbl_sc_day,
                        ],
                        spacing=6,
                    ),
                ),
                self._section("Today", today),
                self._section(
                    "Equity",
                    ft.Row(
                        [
                            ft.Column(
                                [ft.Text("NetLiq", size=13, color=MUTED), self.lbl_sc_netliq],
                                spacing=4,
                            ),
                            ft.Column(
                                [
                                    ft.Text("Agent return", size=13, color=MUTED),
                                    self.lbl_sc_agent_ret,
                                ],
                                spacing=4,
                            ),
                        ],
                        spacing=24,
                    ),
                    self.lbl_sc_equity_empty,
                    self.sc_equity_spark,
                ),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                    content=ft.Text(
                        "Recent activity", color=MUTED, size=13, weight=ft.FontWeight.W_600
                    ),
                ),
                ft.Container(
                    padding=12,
                    expand=True,
                    content=ft.Column(
                        [self.sc_dispatch_table], scroll=ft.ScrollMode.AUTO, expand=True
                    ),
                ),
            ],
            expand=True,
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
        )


    def _sync_ibkr_account_label(self) -> None:
        s = self.engine.state
        cfg = get_config()
        paper = bool(cfg.is_paper)
        aid = str(getattr(s, "ibkr_account_id", "") or "")
        aname = str(getattr(s, "ibkr_account_name", "") or "")
        self.lbl_account_mode.value = "Paper" if paper else "Live"
        self.lbl_account_mode.color = GREEN if paper else RED
        self.btn_account_mode.border = ft.Border.all(1, GREEN if paper else RED)
        if s.connected and aid:
            self.lbl_account_name.value = aname or f"IBKR {aid}"
            self.lbl_account_id.value = aid
        elif s.connected:
            self.lbl_account_name.value = aname or "IBKR"
            self.lbl_account_id.value = "Account id pending…"
        else:
            self.lbl_account_name.value = "IBKR"
            self.lbl_account_id.value = "Not connected"

    def _toggle_trading_mode(self, _=None) -> None:
        """Flip Paper ↔ Live from the account chip (live requires confirm phrase)."""
        if get_config().is_paper:
            self._open_live_confirm_dialog()
        else:
            try:
                self.engine.switch_trading_mode("paper")
                self._after_mode_change()
            except Exception as e:
                self.page.overlay.append(
                    ft.SnackBar(ft.Text(str(e)), bgcolor=RED, open=True)
                )
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
            except Exception as e:
                self.page.overlay.append(
                    ft.SnackBar(ft.Text(str(e)), bgcolor=RED, open=True)
                )
                self._safe_update()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            title=ft.Text("Switch to Live?", color=TEXT),
            content=ft.Column(
                [
                    ft.Text(
                        "Real-money mode. Type the exact confirm phrase:",
                        size=13,
                        color=MUTED,
                    ),
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
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _after_mode_change(self) -> None:
        self._sync_ibkr_account_label()
        self._apply_suite_nav_visibility()
        self.page.overlay.append(
            ft.SnackBar(
                ft.Text(f"Mode → {self.lbl_account_mode.value}"),
                bgcolor=BLUE,
                open=True,
            )
        )
        self._safe_update()

    def _apply_suite_nav_visibility(self) -> None:
        paper = bool(get_config().is_paper)
        btn = self.sidebar_btns.get("suite")
        if btn is not None:
            btn.visible = paper
        if not paper and self.tab == "suite":
            self._show_tab("overview")

    def _page_suite(self) -> ft.Column:



        from abcxauto.order_suite import SUITE_STRATEGIES
        from abcxauto.strategy_params import OPTION_STRATEGIES

        manage_keys = {
            "oca", "modify_stop", "modify_target", "cancel_order",
            "trailing_stop", "trailing_stop_limit", "roll_option",
        }
        stockish, options, manage = [], [], []
        for name in SUITE_STRATEGIES:
            if name in OPTION_STRATEGIES and name != "roll_option":
                options.append(name)
            elif name in manage_keys:
                manage.append(name)
            else:
                stockish.append(name)

        filt = self.suite_filter
        groups = [
            ("stock", "Stock / auction / algo", stockish),
            ("manage", "Manage / protect", manage),
            ("options", "Options", options),
        ]
        if filt != "all":
            groups = [g for g in groups if g[0] == filt]

        def _filter_chip(key: str, label: str) -> ft.Container:
            on = self.suite_filter == key
            return ft.Container(
                content=ft.Text(
                    label,
                    size=12,
                    weight=ft.FontWeight.BOLD if on else ft.FontWeight.W_500,
                    color=TEXT if on else MUTED,
                ),
                bgcolor=HOVER if on else BG,
                border=ft.Border.all(1, BLUE if on else BORDER),
                border_radius=999,
                padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                ink=True,
                on_click=lambda e, k=key: self._set_suite_filter(k),
            )

        def _rows(names: list[str]) -> list[ft.Control]:
            out: list[ft.Control] = []
            for name in names:
                status = self.suite_status.get(name)
                if status is None:
                    status = ft.Text("—", size=12, color=MUTED, selectable=True)
                    self.suite_status[name] = status
                out.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                        border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(
                                            name, size=14,
                                            weight=ft.FontWeight.W_600, color=TEXT,
                                        ),
                                        status,
                                    ],
                                    spacing=2,
                                    expand=True,
                                    tight=True,
                                ),
                                ft.TextButton(
                                    "Test",
                                    style=ft.ButtonStyle(color=BLUE),
                                    on_click=lambda e, n=name: self._test_strategy(n),
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            return out

        def _group(title: str, names: list[str]) -> list[ft.Control]:
            if not names:
                return []
            return [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                    content=ft.Text(
                        title, size=13, weight=ft.FontWeight.BOLD, color=MUTED
                    ),
                ),
                *_rows(names),
            ]

        header = ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Text(
                        "Starts the engine if needed, then place→cancel each type on paper IBKR. Paper mode only.",
                        color=MUTED,
                        size=13,
                    ),
                    ft.Row(
                        [
                            _filter_chip("all", "All"),
                            _filter_chip("stock", "Stock"),
                            _filter_chip("manage", "Manage"),
                            _filter_chip("options", "Options"),
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        [self._btn("Re-test all", BLUE, self._retest_suite)],
                        spacing=8,
                    ),
                    self.lbl_order_suite,
                ],
                spacing=10,
            ),
        )
        body: list[ft.Control] = [header]
        for _, title, names in groups:
            body.extend(_group(title, names))
        return ft.Column(
            body,
            spacing=0,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def _set_suite_filter(self, key: str) -> None:
        self.suite_filter = key
        if self.tab == "suite":
            self.content.content = self._page_suite()
        self._safe_update()

    def _refresh_suite_statuses(self) -> None:
        suite = getattr(self.engine.state, "order_suite", None) or {}
        by_name = {
            str(r.get("strategy")): r
            for r in (suite.get("results") or [])
            if r.get("strategy")
        }
        for name, lbl in self.suite_status.items():
            row = by_name.get(name)
            if not row:
                continue
            ok = bool(row.get("pass"))
            detail = str(row.get("detail") or row.get("mode") or "")[:80]
            lbl.value = f"{'PASS' if ok else 'FAIL'} — {detail}"
            lbl.color = GREEN if ok else RED

    def _test_strategy(self, strategy: str) -> None:
        if not get_config().is_paper:
            self.page.overlay.append(
                ft.SnackBar(ft.Text("Test Suite is paper-only"), bgcolor=AMBER, open=True)
            )
            self._safe_update()
            return
        status = self.suite_status.get(strategy)
        if status is None:
            status = ft.Text("—", size=12, color=MUTED, selectable=True)
            self.suite_status[strategy] = status
        status.value = "Starting paper IBKR…"
        status.color = AMBER
        self._safe_update()
        try:
            row = self.engine.run_strategy_test(strategy)
        except Exception as e:
            row = {"pass": False, "detail": str(e)[:200], "mode": "broker_fail"}
        detail = str(row.get("detail") or row.get("mode") or "")[:80]
        if row.get("mode") == "paper_pending":
            status.value = f"Running on paper… {detail}"
            status.color = BLUE
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text(f"{strategy}: running on paper…"),
                    bgcolor=BLUE,
                    open=True,
                )
            )
        else:
            ok = bool(row.get("pass"))
            status.value = f"{'PASS' if ok else 'FAIL'} — {detail}"
            status.color = GREEN if ok else RED
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text(f"{strategy}: {'PASS' if ok else 'FAIL'}"),
                    bgcolor=GREEN if ok else RED,
                    open=True,
                )
            )
        self._sync_widgets()
        self._safe_update()

    def _toast(self, msg: str, *, color: str = BLUE) -> None:
        self.page.overlay.append(ft.SnackBar(ft.Text(msg), bgcolor=color, open=True))

    def _toggle_run(self, _=None) -> None:
        s = self.engine.state
        if s.running and not getattr(s, "paused", False) and getattr(s, "autonomous", False):
            self.engine.pause_engine()
            self._toast("Agent stopped — IBKR stays connected", color=AMBER)
        else:
            err = self.engine.start()
            if err:
                self._toast(err, color=RED)
            else:
                self._toast("Starting agent (Grok cycles)…", color=BLUE)
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
        if err:
            self._toast(err, color=RED)
        else:
            self._toast("Connecting to IBKR (TWS/Gateway)…", color=BLUE)
        self._sync_widgets()
        self._safe_update()

    def _open_disconnect_confirm_dialog(self) -> None:
        s = self.engine.state
        agent_on = bool(s.running) and getattr(s, "autonomous", False)
        warn = (
            "This stops the agent and tears down the IBKR link. "
            "Open orders and positions are left as-is at the broker."
            if agent_on
            else (
                "This tears down the IBKR link. "
                "Open orders and positions are left as-is at the broker."
            )
        )

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
            content=ft.Text(warn, size=13, color=MUTED),
            actions=[
                ft.TextButton("Cancel", on_click=_cancel),
                ft.TextButton(
                    "Disconnect",
                    on_click=_confirm,
                    style=ft.ButtonStyle(color=RED),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _refresh_connect_btn(self) -> None:
        s = self.engine.state
        linked = bool(s.connected) or (
            self.engine.worker is not None and self.engine.worker.is_alive()
        )
        if linked:
            self.btn_connect.content = "Disconnect IBKR"
            self.btn_connect.color = RED
            self.btn_connect.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                side=ft.BorderSide(1, RED),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            )
        else:
            self.btn_connect.content = "Connect IBKR"
            self.btn_connect.color = TEXT
            self.btn_connect.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                side=ft.BorderSide(1, BORDER),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            )

    def _refresh_run_btn(self) -> None:
        s = self.engine.state
        running = bool(s.running) and getattr(s, "autonomous", False) and not getattr(s, "paused", False)
        if running:
            self.btn_run.content = "Stop agent"
            self.btn_run.bgcolor = SURFACE
            self.btn_run.color = TEXT
            self.btn_run.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                side=ft.BorderSide(1, BORDER),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            )
        else:
            self.btn_run.content = "Start agent"
            self.btn_run.bgcolor = WHITE
            self.btn_run.color = "#0f1419"
            self.btn_run.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            )

    def _refresh_service_status(self) -> None:
        try:
            from abcxauto.connections import connection_status
            st = connection_status(self.engine.conn)
        except Exception:
            st = {}
        s = self.engine.state
        ibkr_ok = bool(s.connected)
        xai_ok = bool(st.get("xai_configured"))
        mda_ok = bool(st.get("mda_configured"))
        mode = str(st.get("trading_mode") or get_config().trading_mode or "paper")
        if s.status == "Connecting" and not ibkr_ok:
            self.dot_conn.bgcolor = AMBER
            self.lbl_ibkr_status.value = "Connecting…"
            self.lbl_ibkr_status.color = AMBER
        else:
            self.dot_conn.bgcolor = GREEN if ibkr_ok else RED
            self.lbl_ibkr_status.value = f"Connected ({mode})" if ibkr_ok else "Disconnected"
            self.lbl_ibkr_status.color = GREEN if ibkr_ok else MUTED
        self.dot_xai.bgcolor = GREEN if xai_ok else RED
        self.lbl_xai_status.value = "Ready" if xai_ok else "Missing key"
        self.lbl_xai_status.color = GREEN if xai_ok else MUTED
        self.dot_mda.bgcolor = GREEN if mda_ok else RED
        self.lbl_mda_status.value = "Ready" if mda_ok else "Not configured"
        self.lbl_mda_status.color = GREEN if mda_ok else MUTED
        # Surface one-shot connect/start failures from the worker.
        err = getattr(s, "last_error", None)
        if err:
            s.last_error = None
            self._toast(str(err), color=RED)

    def _start(self, _=None) -> None:
        err = self.engine.start()
        if err:
            self.page.overlay.append(ft.SnackBar(ft.Text(err), bgcolor=RED, open=True))
        self._sync_widgets()
        self._safe_update()

    def _pause(self, _=None) -> None:
        self.engine.pause_engine()
        self._sync_widgets()
        self._safe_update()

    def _stop(self, _=None) -> None:
        self.engine.stop_engine()
        self._sync_widgets()
        self._safe_update()

    def _panic(self, _=None) -> None:
        self.engine.panic()
        self._sync_widgets()
        self._safe_update()

    def _retest_suite(self, _=None) -> None:
        """Manual re-test: paper brokerage place→cancel when connected."""
        if not get_config().is_paper:
            self.page.overlay.append(
                ft.SnackBar(ft.Text("Test Suite is paper-only"), bgcolor=AMBER, open=True)
            )
            self._safe_update()
            return
        self.engine.run_manual_suite()
        self.page.overlay.append(
            ft.SnackBar(ft.Text("Starting paper suite (place→cancel)…"), bgcolor=BLUE, open=True)
        )
        self._safe_update()

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
            self.engine.drain_apply()
            self._sync_widgets()
            if self.tab == "scorecard":
                self._refresh_scorecard(force=False)
            self._safe_update()
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
                self._refresh_account_performance()
                await self._refresh_returns()
                await self._refresh_news()
                self._safe_update()
            except Exception:
                pass
            await asyncio.sleep(1.0)

    def _apply_clock(self, pulse: dict) -> None:
        view = pulse_clock_view(pulse)
        self.lbl_clock.value = view.get("clock") or "—"
        status = (view.get("session_status") or "closed").lower()
        self.lbl_session_badge.value = view.get("session") or "—"
        self.lbl_session_badge.color = (
            GREEN if status == "regular"
            else (AMBER if status in ("premarket", "postmarket") else MUTED)
        )
        self.lbl_countdown_title.value = (
            "Open time" if view.get("countdown_to") == "open" else "Close time"
        )
        self.lbl_countdown.value = view.get("countdown_human") or "—"
        self.lbl_data_age.value = view.get("ibkr_refresh") or "n/a"
        if pulse.get("narrative"):
            self.lbl_pulse_narrative.value = pulse["narrative"]

    def _format_return_pct(self, value) -> tuple[str, str]:
        """Return (label, color) for a fractional return."""
        if value is None:
            return "—", MUTED
        try:
            pct = float(value) * 100.0
        except (TypeError, ValueError):
            return "—", MUTED
        color = GREEN if pct >= 0 else RED
        return f"{pct:+.2f}%", color

    def _render_news_list(self, items: list[dict], *, fallback: str = "") -> None:
        rows: list[ft.Control] = []
        for it in (items or [])[:10]:
            hl = str(it.get("headline") or "").strip()
            if not hl:
                continue
            sym = str(it.get("symbol") or "").upper()
            src = str(it.get("source") or "")
            if src.startswith("http"):
                # keep host only for compactness
                try:
                    from urllib.parse import urlparse
                    host = urlparse(src).netloc or src
                except Exception:
                    host = src
            else:
                host = src
            meta = " · ".join(x for x in (sym, host) if x)
            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=8),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Text(hl, size=13, color=TEXT, weight=ft.FontWeight.W_500),
                            ft.Text(meta or "news", size=11, color=MUTED),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                )
            )
        if not rows:
            text = fallback or "No headlines yet."
            rows = [
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=8),
                    content=ft.Text(text, size=13, color=MUTED, selectable=True),
                )
            ]
        self.news_list.controls = rows

    def _refresh_account_performance(self) -> None:
        """IBKR live equity/daily; horizon returns come from journal NAV history."""
        s = self.engine.state
        self.lbl_equity.value = f"${float(s.equity or 0):,.0f}"
        self.lbl_pnl.value = f"${float(s.pnl or 0):+,.2f}"
        self.lbl_pnl.color = GREEN if float(s.pnl or 0) >= 0 else RED
        if not getattr(self, "_ret_cache", None):
            self.lbl_ret_source.value = "IBKR NAV — building history…"
            self.lbl_ret_source.color = MUTED

    def _nav_disclaimer(self, perf: dict) -> str:
        """Short tracking-since / updated line for the Account card."""
        src = str((perf or {}).get("source") or "none")
        if src != "ibkr_nav":
            return "IBKR NAV — building history…"
        start = (perf or {}).get("history_start")
        days = (perf or {}).get("history_days")
        as_of = (perf or {}).get("as_of")
        start_bit = "—"
        if start:
            try:
                dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                start_bit = dt.strftime("%Y-%m-%d")
            except ValueError:
                start_bit = str(start)[:10]
        days_bit = f" ({int(days)}d)" if days is not None else ""
        updated_bit = ""
        if as_of:
            try:
                dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
                updated_bit = f" · updated {dt.strftime('%H:%MZ')}"
            except ValueError:
                updated_bit = f" · updated {str(as_of)[:16]}"
        return f"IBKR NAV since {start_bit}{days_bit}{updated_bit}"

    def _apply_return_perf(self, perf: dict) -> None:
        self.lbl_ret_source.value = self._nav_disclaimer(perf)
        self.lbl_ret_source.color = MUTED
        for lbl_attr, col_attr, key in (
            ("lbl_ret_1w", "col_ret_1w", "ret_1w"),
            ("lbl_ret_3m", "col_ret_3m", "ret_3m"),
            ("lbl_ret_1y", "col_ret_1y", "ret_1y"),
        ):
            raw = (perf or {}).get(key)
            label, color = self._format_return_pct(raw)
            getattr(self, lbl_attr).value = label
            getattr(self, lbl_attr).color = color
            col = getattr(self, col_attr, None)
            if col is not None:
                col.visible = raw is not None

    async def _refresh_returns(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and getattr(self, "_ret_last_fetch", 0) and (now - self._ret_last_fetch) < 120.0:
            return
        self._ret_last_fetch = now
        try:
            from abcxauto.account_returns import compute_account_returns
            perf = compute_account_returns(
                equity=self.engine.state.equity,
                daily_pnl=self.engine.state.pnl,
            )
            self._ret_cache = perf
            self._apply_return_perf(perf)
        except Exception:
            self.lbl_ret_source.value = "IBKR NAV — error"
            self.lbl_ret_source.color = MUTED


    async def _refresh_news(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._news_last_fetch) < 60.0:
            return
        self._news_last_fetch = now
        try:
            from abcxauto.news_feed import fetch_agent_news
            unique = await fetch_agent_news(
                self.engine.state.positions, force=force, per_symbol=5
            )
        except Exception:
            unique = []
        self._news_cache = unique
        self._render_news_list(unique)

    def _sync_widgets(self) -> None:
        s = self.engine.state
        self._sync_ibkr_account_label()
        self._apply_suite_nav_visibility()
        self.lbl_cycles.value = str(s.cycles)
        self.lbl_equity.value = f"${s.equity:,.0f}"
        self.lbl_pnl.value = f"${s.pnl:+.2f}"
        self.lbl_pnl.color = GREEN if s.pnl >= 0 else RED
        self._refresh_account_performance()
        self.lbl_risk.value = s.risk
        mode_color = GREEN if s.running else (AMBER if getattr(s, "paused", False) else MUTED)
        self.lbl_status.value = self.lbl_mode.value = s.status
        self.lbl_status.color = self.lbl_mode.color = mode_color
        self._refresh_run_btn()
        self._refresh_connect_btn()
        self._refresh_service_status()
        stance = str(getattr(s, "stance", "") or "").strip()
        thesis = str(getattr(s, "thesis", "") or "").strip()
        dismissed = str(getattr(s, "dismissed", "") or "").strip()
        intent = getattr(s, "intent", None) or {}
        stage_err = str(getattr(s, "stage_error", "") or "").strip()
        if stance or thesis:
            jlines = [f"Stance: {stance or '—'}"]
            if thesis:
                jlines.append(f"Thesis: {thesis}")
            if dismissed:
                jlines.append(f"Dismissed: {dismissed}")
            if intent:
                kind = intent.get("kind") or ""
                isym = intent.get("symbol") or ""
                idir = intent.get("direction") or ""
                if kind or isym:
                    jlines.append(f"Intent: {kind} {isym} {idir}".strip())
                else:
                    jlines.append(f"Intent: {json.dumps(intent, default=str)[:180]}")
            if stage_err:
                jlines.append(f"Stage error: {stage_err}")
            self.lbl_agent_judgment.value = "\n".join(jlines)
            self.lbl_agent_judgment.color = TEXT
        else:
            self.lbl_agent_judgment.value = (
                "Judgment: — (stance / thesis after next Judge cycle)"
            )
            self.lbl_agent_judgment.color = MUTED
        market_read = str(getattr(s, "market_read", "") or "").strip()
        if market_read:
            focus_short = market_read if len(market_read) <= 220 else market_read[:217] + "…"
            self.lbl_agent_read.value = f"Focus: {focus_short}"
            self.lbl_agent_read.color = TEXT
        else:
            self.lbl_agent_read.value = "Focus: —"
            self.lbl_agent_read.color = MUTED
        rationale = str(s.brain_rationale or "").strip()
        if rationale and rationale != "—":
            why = rationale if len(rationale) <= 240 else rationale[:237] + "…"
            self.brain_rationale.value = f"Why: {why}"
            self.brain_rationale.color = TEXT
        else:
            self.brain_rationale.value = "Why: —"
            self.brain_rationale.color = MUTED
        act = s.last_action or {}
        strat = s.brain_strat or act.get("strategy") or act.get("action") or "—"
        result = s.last_result or {}
        status = self._format_result_status(result)
        params = getattr(s, "last_params", None) or act.get("params") or {}
        self.brain_action.value = f"{strat}  ·  {status}"
        blocked = status.lower().startswith(("blocked", "rejected", "fail", "error"))
        filled = "fill" in status.lower() or result.get("filled") is True
        self.brain_action.color = RED if blocked else (GREEN if filled else TEXT)
        bit = f"  ·  {json.dumps(result, default=str)[:140]}" if result else ""
        self.lbl_proposal.value = f"{strat}  ·  conId={act.get('target_conId') or '—'}{bit}"
        if params:
            # Prefer operator-readable prices over raw JSON dump.
            sym = params.get("symbol") or intent.get("symbol") or ""
            direction = params.get("direction") or params.get("action") or ""
            qty = params.get("quantity") or ""
            stop = params.get("stop_price")
            tgt = params.get("target_price")
            entry = params.get("entry_price") or result.get("entry_price")
            parts = [p for p in (str(sym), str(direction), f"x{qty}" if qty != "" else "") if p]
            if stop is not None:
                parts.append(f"stop={stop}")
            if tgt is not None:
                parts.append(f"tgt={tgt}")
            if entry is not None:
                parts.append(f"entry={entry}")
            self.lbl_agent_params.value = (
                "Params: " + (" ".join(parts) if parts else json.dumps(params, default=str)[:220])
            )
            self.lbl_agent_params.color = TEXT
        else:
            self.lbl_agent_params.value = "Params: —"
            self.lbl_agent_params.color = MUTED
        grade = str(getattr(s, "structure_grade", "") or "").strip()
        lessons = getattr(s, "structure_lessons", None) or []
        if grade or lessons:
            gline = f"Structure grade: {grade or '—'}"
            if lessons:
                last = lessons[0]
                gline += (
                    f"\nPrior lesson (journal): "
                    f"{last.get('outcome') or last.get('reason_code')} "
                    f"{last.get('symbol') or ''} "
                    f"{str(last.get('message') or '')[:100]}"
                )
            self.lbl_agent_structure.value = gline
            bad = grade not in ("", "ok", "hold", "set_risk", "—")
            geom = "geometry" in grade or "scrape" in grade
            self.lbl_agent_structure.color = RED if bad and geom else (AMBER if bad else TEXT)
        else:
            self.lbl_agent_structure.value = "Structure grade: — (shell grades Grok's geometry)"
            self.lbl_agent_structure.color = MUTED
        # One-line "Now" headline — what to look at without reading the feed.
        intent_sym = str(intent.get("symbol") or params.get("symbol") or "").upper()
        intent_dir = str(intent.get("direction") or params.get("direction") or "")
        intent_bit = f" {intent_sym} {intent_dir}".rstrip() if (intent_sym or intent_dir) else ""
        stance_bit = (stance or "—").upper()
        if int(getattr(s, "cycles", 0) or 0) <= 0 and not stance and strat in ("—", "", None):
            self.lbl_agent_now.value = "Waiting for first cycle…"
            self.lbl_agent_now.color = MUTED
        else:
            now = f"c{s.cycles}  {stance_bit}{intent_bit}  →  {strat}  ·  {status}"
            if grade and grade not in ("ok", "hold", "set_risk", ""):
                now += f"  [{grade}]"
            pace = getattr(s, "pace", None) or {}
            if pace.get("tier"):
                wake = pace.get("wake_reason") or ""
                wake_bit = f" wake={wake}" if wake else ""
                now += (
                    f"  ·  pace={pace.get('tier')}"
                    f"/{int(float(pace.get('sleep_s') or 0))}s"
                    f"{wake_bit}"
                )
            self.lbl_agent_now.value = now
            self.lbl_agent_now.color = (
                RED if blocked or (grade and "geometry" in grade)
                else (GREEN if filled else TEXT)
            )
        posture = str(getattr(s, "risk_posture", "") or "").strip()
        if not posture:
            try:
                posture = str(get_config().risk_posture or "").strip()
            except Exception:
                posture = ""
        posture = posture or "—"
        self.lbl_agent_posture.value = f"Posture: {posture}"
        self.lbl_agent_posture.color = TEXT if posture != "—" else MUTED
        regime = getattr(s, "regime", None) or {}
        port_risk = getattr(s, "portfolio_risk", None) or {}
        world_bits = []
        if regime:
            mix = regime.get("feature_mix_bias") or regime.get("trend_bias")
            world_bits.append(
                f"Feature mix (heuristic): {mix}/{regime.get('vol_proxy')} "
                f"phase={regime.get('session_phase')} — not regime truth"
            )
        if port_risk:
            world_bits.append(
                f"Book: n={port_risk.get('n_positions')} "
                f"top={port_risk.get('top_symbol')} "
                f"{port_risk.get('top_concentration_pct')}%"
            )
        idle_n = (getattr(s, "world_state", None) or {}).get("idle_streak")
        if idle_n is not None:
            world_bits.append(f"Idle streak: {idle_n}")
        self.lbl_agent_world.value = (
            "\n".join(world_bits) if world_bits else "World: (awaiting cycle)"
        )
        self.lbl_agent_world.color = TEXT if world_bits else MUTED
        plan = getattr(s, "trade_plan", None)
        if not plan:
            try:
                from abcxauto.trade_plan import load_trade_plan

                loaded = load_trade_plan()
                if loaded:
                    plan = loaded.to_dict()
                    s.trade_plan = plan
            except Exception:
                plan = None
        if plan:
            try:
                from abcxauto.trade_plan import ActiveTradePlan, format_open_risk_line

                line = format_open_risk_line(ActiveTradePlan.from_dict(plan))
            except Exception:
                line = (
                    f"OPEN RISK  {plan.get('symbol')} {plan.get('direction')} "
                    f"x{plan.get('quantity')} stop={plan.get('stop_price')} "
                    f"tgt={plan.get('target_price')}"
                )
            self.lbl_agent_plan.value = line
            paused = bool(getattr(s, "paused", False)) or not bool(
                getattr(s, "autonomous", False) or getattr(s, "running", False)
            )
            self.lbl_agent_plan.color = AMBER if paused and s.connected else TEXT
            # Prefer open-risk headline when agent is stopped/paused with risk.
            if paused or int(getattr(s, "cycles", 0) or 0) <= 0:
                self.lbl_agent_now.value = line
                self.lbl_agent_now.color = AMBER if paused else TEXT
        else:
            self.lbl_agent_plan.value = "Open risk: (flat / no plan)"
            self.lbl_agent_plan.color = MUTED
        try:
            from abcxauto.memory import get_journal

            div = get_journal().strategy_diversity(limit=40)
            if div.get("n_distinct"):
                strats = ", ".join(div.get("strategies") or [])[:80]
                self.lbl_agent_plan.value = (
                    f"{self.lbl_agent_plan.value}\n"
                    f"Strategy mix (observe): {div['n_distinct']} types — {strats}"
                )
        except Exception:
            pass
        news = getattr(s, "news_items", None) or []
        if news:
            nlines = []
            for it in news[:10]:
                sym = str(it.get("symbol") or "?").upper()
                hl = str(it.get("headline") or "").strip()
                if hl:
                    nlines.append(f"• [{sym}] {hl}")
            self.lbl_agent_news.value = "News (World):\n" + "\n".join(nlines)
            self.lbl_agent_news.color = TEXT
        else:
            self.lbl_agent_news.value = "News (World): (none this cycle)"
            self.lbl_agent_news.color = MUTED
        try:
            from abcxauto.universe import universe_glance_line

            self.lbl_agent_universe.value = universe_glance_line()
            self.lbl_agent_universe.color = TEXT
        except Exception:
            self.lbl_agent_universe.value = "Universe: (unavailable)"
            self.lbl_agent_universe.color = MUTED
        opps = getattr(s, "opportunities", None) or []
        if opps:
            bits = []
            # Preserve seed / legal order — never alphabetize (A* bias).
            for idea in list(opps)[:12]:
                bits.append(
                    f"- {idea.get('symbol')} "
                    f"mda/{idea.get('freshness') or 'delayed'} "
                    f"last={idea.get('mda_last') or idea.get('last')} "
                    f"dist20={idea.get('dist20')} ret5={idea.get('ret5')}"
                )
            ibkr_sym = getattr(s, "ibkr_live_symbol", None) or (
                (getattr(s, "world_state", None) or {}).get("ibkr_live_symbol")
            )
            ibkr_last = getattr(s, "ibkr_live_last", None)
            if ibkr_last is None:
                ibkr_last = (getattr(s, "world_state", None) or {}).get("ibkr_live_last")
            head = "SCAN TAPE (MDA delayed; unranked; Grok picks):\n" + "\n".join(bits)
            if ibkr_sym and ibkr_last is not None:
                head += f"\nIBKR live: {ibkr_sym} last={ibkr_last}"
            self.lbl_agent_opps.value = head
            self.lbl_agent_opps.color = TEXT
        else:
            self.lbl_agent_opps.value = (
                "SCAN TAPE: (none — check MDA / Start agent; Grok may scan_request)"
            )
            self.lbl_agent_opps.color = MUTED
        inv = getattr(s, "inventory", "") or ""
        self.lbl_ledger_snippet.value = inv[:2500] if inv else "Ledger empty"
        port = getattr(s, "portfolio", None) or {}
        netliq = float(port.get("net_liquidation") if port.get("net_liquidation") is not None
                       else s.equity or 0)
        daily = float(port.get("daily_pnl") if port.get("daily_pnl") is not None else s.pnl or 0)
        unprotected_n = int(
            port.get("unprotected_count") if port.get("unprotected_count") is not None
            else getattr(s, "unprotected_count", 0) or 0
        )
        decision = str(port.get("last_decision") or getattr(s, "last_decision", None) or "—")
        halted = bool(port.get("halted") if "halted" in port else getattr(s, "halted", False))
        self.lbl_book_netliq.value = f"${netliq:,.0f}"
        self.lbl_book_pnl.value = f"${daily:+.2f}"
        self.lbl_book_pnl.color = GREEN if daily >= 0 else RED
        self.lbl_book_unprotected.value = str(unprotected_n)
        self.lbl_book_unprotected.color = RED if unprotected_n else GREEN
        self.lbl_book_decision.value = f"Last decision: {decision}"
        self.lbl_book_decision.color = AMBER if decision == "hold" else TEXT
        self.lbl_book_decision.size = 12
        self.lbl_book_halt.value = "HALTED" if halted else "clear"
        self.lbl_book_halt.color = RED if halted else GREEN
        pace = getattr(s, "pace", None) or {}
        if pace.get("tier"):
            wake = pace.get("wake_reason") or ""
            budget = pace.get("budget") or pace.get("reason") or ""
            wake_bit = f" · wake={wake}" if wake else ""
            budget_bit = f" · {budget}" if budget and budget != "ok" else ""
            self.lbl_dash_pace.value = (
                f"Pace: {pace.get('tier')} / {int(float(pace.get('sleep_s') or 0))}s"
                f"{wake_bit}{budget_bit}"
            )
            self.lbl_dash_pace.color = (
                RED if str(pace.get("tier")) == "protect" else TEXT
            )
        else:
            self.lbl_dash_pace.value = "Pace: — (Start agent for adaptive sleep)"
            self.lbl_dash_pace.color = MUTED
        ws = getattr(s, "world_state", None) or {}
        cap = ws.get("capacity") if isinstance(ws, dict) else {}
        if not isinstance(cap, dict):
            cap = {}
        idle = ws.get("idle_streak") if isinstance(ws, dict) else None
        open_n = cap.get("open_count")
        slots = cap.get("slots_left")
        max_open = cap.get("max_open_positions")
        allows = cap.get("allows_new_risk")
        att_bits = []
        if open_n is not None or max_open is not None:
            att_bits.append(
                f"book {open_n if open_n is not None else '?'} / "
                f"{max_open if max_open not in (None, 0) else '∞'} open"
            )
        if slots is not None:
            att_bits.append(f"slots_left={slots}")
        if allows is not None:
            att_bits.append("new-risk ok" if allows else "new-risk blocked")
        if idle is not None:
            att_bits.append(f"idle_streak={idle}")
        att_bits.append(f"netliq={self.lbl_book_netliq.value}")
        self.lbl_dash_attention.value = (
            "Attention: " + " · ".join(att_bits) if att_bits else "Attention: —"
        )
        self.lbl_dash_attention.color = TEXT
        health = getattr(s, "mandate_health", "green") or "green"
        health_label = getattr(s, "mandate_health_label", "") or ""
        self.lbl_mandate_health.value = (
            f"{health} — {health_label}" if health_label else str(health)
        )
        self.lbl_mandate_health.color = (
            RED if health == "red" else (AMBER if health == "amber" else GREEN)
        )
        if pulse := (getattr(s, "reality_pulse", None) or {}):
            self._apply_clock(pulse)
        suite = getattr(s, "order_suite", None) or {}
        if suite or getattr(s, "order_suite_summary", None):
            summary = (
                getattr(s, "order_suite_summary", None) or suite.get("summary") or "—"
            )[:900]
            self.lbl_order_suite.value = summary
            rate = float(suite.get("pass_rate") or 0)
            self.lbl_order_suite.color = GREEN if rate >= 0.9 else AMBER
            passed = suite.get("passed")
            failed = suite.get("failed")
            mode = suite.get("mode") or "dry_run"
            if passed is not None:
                self.lbl_suite_chip.value = (
                    f"Suite: {passed} pass / {failed or 0} fail ({mode})"
                )
                self.lbl_suite_chip.color = GREEN if rate >= 0.9 else AMBER
            if self.tab == "suite":
                self._refresh_suite_statuses()
        else:
            self.lbl_suite_chip.value = "Suite: idle"
            self.lbl_suite_chip.color = MUTED
        self.pos_table.rows = self._position_rows(s.positions)
        self.lbl_pos_summary.value = self._positions_summary(s.positions)
        self.lbl_working_orders.value = self._format_working_orders(
            getattr(s, "open_orders", None) or []
        )
        self.lbl_working_orders.color = TEXT if (s.open_orders or []) else MUTED
        self.lbl_recent_fills.value = self._format_recent_fills(
            getattr(s, "recent_fills", None) or []
        )
        self.lbl_recent_fills.color = TEXT if (getattr(s, "recent_fills", None) or []) else MUTED
        self.lbl_cycle_log.value = self._cycle_log_text(s.records)
        if self.tab == "risk":
            self._sync_risk_halt_label()

    @staticmethod
    def _format_result_status(res: object) -> str:
        """Short operator-facing ACT outcome from a cycle result dict."""
        if not isinstance(res, dict) or not res:
            return "—"
        if res.get("success") is True:
            if res.get("filled") is True:
                ep = res.get("entry_price")
                return f"filled @{ep}" if ep not in (None, "") else "filled"
            return "ok"
        status = str(res.get("status") or "").strip()
        note = str(
            res.get("note") or res.get("reason_code") or res.get("error") or ""
        ).strip()
        if status and note and note.lower() not in status.lower():
            combo = f"{status}: {note}"
            return combo if len(combo) <= 120 else combo[:117] + "…"
        return status or note or ("fail" if res.get("success") is False else "ok")

    def _cycle_log_text(self, records: list[dict]) -> str:
        lines: list[str] = []
        for r in reversed(list(records or [])[-40:]):
            kind = str(r.get("type") or "cycle").lower()
            ts = str(r.get("ts") or "")[-8:]  # HH:MM:SS when ISO
            if "T" in str(r.get("ts") or ""):
                ts = str(r.get("ts") or "").split("T", 1)[-1][:8]
            if kind in ("error", "err"):
                lines.append(f"{ts}  ERR  {r.get('msg', 'error')}")
            elif kind == "panic":
                lines.append(f"{ts}  FLATTEN  Close All Positions")
            elif kind in (
                "connect", "start", "pause", "disconnect", "log", "open_risk",
            ):
                tag = kind.upper()
                lines.append(f"{ts}  {tag}  {r.get('msg') or '—'}")
            elif kind == "order_suite":
                lines.append(f"{ts}  SUITE  {r.get('msg') or r.get('lab_summary') or '—'}")
            else:
                strat = r.get("strat") or (r.get("action_obj") or {}).get("strategy") or "—"
                res = r.get("result") or {}
                status = self._format_result_status(res)
                posture = str(r.get("risk_posture") or "").strip()
                stance = str(r.get("stance") or (r.get("judgment") or {}).get("stance") or "")
                opps = r.get("opportunities") or []
                top = ""
                if opps:
                    o0 = opps[0]
                    top = (
                        f"  top={o0.get('symbol')} {o0.get('bias')} "
                        f"({o0.get('score')})"
                    )
                elif posture:
                    top = "  top=(none)"
                params = r.get("params") or (r.get("action_obj") or {}).get("params") or {}
                param_bit = ""
                if strat not in ("hold", "skipped", "blocked", "—") and params:
                    sym = params.get("symbol") or ""
                    qty = params.get("quantity") or ""
                    direction = params.get("direction") or params.get("action") or ""
                    stop = params.get("stop_price")
                    param_bit = f"  {sym} {direction} x{qty}".rstrip()
                    if stop is not None:
                        param_bit += f" stop={stop}"
                focus = str(r.get("market_read") or "").replace("\n", " ").strip()
                rationale = str(r.get("rationale") or "").replace("\n", " ").strip()
                if len(focus) > 120:
                    focus = focus[:117] + "…"
                if len(rationale) > 120:
                    rationale = rationale[:117] + "…"
                post_bit = f"  [{posture}]" if posture else ""
                stance_bit = f"  {stance}" if stance else ""
                lines.append(
                    f"{ts}  #{r.get('cycle', '?')}  JUDGE{stance_bit}{post_bit}{top}"
                )
                if focus:
                    lines.append(f"         focus: {focus}")
                lines.append(
                    f"{ts}  #{r.get('cycle', '?')}  ACT  {strat}  {status}{param_bit}"
                )
                if rationale:
                    lines.append(f"         why: {rationale}")
                sgrade = str(r.get("structure_grade") or "").strip()
                if sgrade and sgrade not in ("ok", "hold", "set_risk"):
                    lines.append(f"         structure: {sgrade}")
                stage_err = str(r.get("stage_error") or "").strip()
                if stage_err and stage_err not in status:
                    lines.append(f"         block: {stage_err[:140]}")
        return "\n".join(lines) if lines else (
            "No activity yet — Connect IBKR, then Start agent"
        )

    def _format_working_orders(self, orders: list[dict]) -> str:
        if not orders:
            return "No working orders"
        lines = [f"{len(orders)} working"]
        for o in (orders or [])[:25]:
            sym = o.get("symbol") or "?"
            side = o.get("action") or o.get("side") or "?"
            qty = o.get("quantity") or o.get("qty") or "?"
            status = o.get("status") or "?"
            oid = o.get("order_id") or o.get("orderId") or "—"
            otype = o.get("order_type") or o.get("orderType") or ""
            px = o.get("lmt_price") or o.get("aux_price")
            px_s = f" @ {px}" if px not in (None, "", 0) else ""
            lines.append(f"• {sym:<6}  {side} {qty}  {otype}{px_s}  {status}  id={oid}")
        return "\n".join(lines)

    def _format_recent_fills(self, fills: list[dict]) -> str:
        if not fills:
            return "No fills this session"
        recent = list(fills or [])[-20:][::-1]
        lines = [f"{len(fills)} fills this session (newest first)"]
        for f in recent:
            sym = f.get("symbol") or "?"
            side = f.get("side") or "?"
            qty = f.get("quantity") or f.get("shares") or "?"
            px = f.get("price")
            try:
                px_s = f"{float(px):.2f}" if px is not None else "—"
            except (TypeError, ValueError):
                px_s = str(px or "—")
            ts = str(f.get("ts") or "")[:19].replace("T", " ")
            lines.append(f"• {ts}  {sym:<6}  {side} {qty} @ {px_s}")
        return "\n".join(lines)

    def _refresh_scorecard(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._scorecard_last_refresh) < _SCORECARD_REFRESH_S:
            return
        self._scorecard_last_refresh = now
        try:
            journal = get_journal()
            summary = journal.daily_summary()
            curve = journal.equity_curve(limit=120)
            dispatches = journal.recent_dispatches(limit=15)
            hold_n, trade_n = self._hold_trade_counts(journal)
        except Exception:
            summary = {
                "day": "—", "proposals": 0, "allowed": 0, "rejected": 0,
                "dispatch_ok": 0, "dispatch_failed": 0, "halts": 0,
            }
            curve, dispatches = [], []
            hold_n = int(getattr(self.engine.state, "hold_count", 0) or 0)
            trade_n = int(getattr(self.engine.state, "trade_count", 0) or 0)

        self.lbl_sc_day.value = f"Day {summary.get('day') or '—'} (UTC)"
        self.lbl_sc_proposals.value = str(int(summary.get("proposals") or 0))
        self.lbl_sc_allowed.value = str(int(summary.get("allowed") or 0))
        self.lbl_sc_rejected.value = str(int(summary.get("rejected") or 0))
        self.lbl_sc_dispatch_ok.value = str(int(summary.get("dispatch_ok") or 0))
        self.lbl_sc_dispatch_failed.value = str(int(summary.get("dispatch_failed") or 0))
        halts = int(summary.get("halts") or 0)
        self.lbl_sc_halts.value = str(halts)
        self.lbl_sc_halts.color = AMBER if halts > 0 else MUTED
        if hold_n + trade_n:
            self.lbl_sc_hold_trade.value = f"{hold_n}/{trade_n}"
            self.lbl_sc_hold_trade.color = TEXT
        else:
            self.lbl_sc_hold_trade.value = "—"
            self.lbl_sc_hold_trade.color = MUTED

        curve_pts = [float(v) for _, v in curve if v is not None]
        if curve_pts:
            self.lbl_sc_netliq.value = f"${curve_pts[-1]:,.0f}"
            self.lbl_sc_netliq.color = (
                GREEN if len(curve_pts) < 2 or curve_pts[-1] >= curve_pts[0] else RED
            )
            self.lbl_sc_equity_empty.visible = False
            self.sc_equity_spark.content = _equity_spark_control(curve_pts)
            self.sc_equity_spark.visible = True
            if len(curve_pts) >= 2 and curve_pts[0]:
                ret = (curve_pts[-1] / curve_pts[0] - 1) * 100
                self.lbl_sc_agent_ret.value = f"{ret:+.2f}%"
                self.lbl_sc_agent_ret.color = GREEN if ret >= 0 else RED
            else:
                self.lbl_sc_agent_ret.value = "—"
                self.lbl_sc_agent_ret.color = MUTED
        else:
            self.lbl_sc_netliq.value = "—"
            self.lbl_sc_netliq.color = MUTED
            self.lbl_sc_equity_empty.visible = True
            self.lbl_sc_equity_empty.value = (
                "No data yet — journal populates as the agent trades"
            )
            self.sc_equity_spark.content = _equity_spark_control([])
            self.sc_equity_spark.visible = True
            self.lbl_sc_agent_ret.value = "—"
            self.lbl_sc_agent_ret.color = MUTED
        self._refresh_dispatch_table(dispatches)

    def _hold_trade_counts(self, journal: Any) -> tuple[int, int]:
        recent = None
        for meth in ("recent_decisions", "recent_proposals"):
            if hasattr(journal, meth):
                try:
                    recent = getattr(journal, meth)(limit=50) or None
                except Exception:
                    recent = None
                if recent:
                    break
        if not recent:
            return (
                int(getattr(self.engine.state, "hold_count", 0) or 0),
                int(getattr(self.engine.state, "trade_count", 0) or 0),
            )
        hold_n = trade_n = 0
        for item in recent:
            strat = str(
                (item or {}).get("strategy")
                or (item or {}).get("action")
                or (item or {}).get("decision")
                or ""
            ).lower()
            if strat in ("hold", "skipped", "blocked", "set_risk", "none", ""):
                hold_n += 1
            else:
                trade_n += 1
        return hold_n, trade_n

    def _refresh_dispatch_table(self, dispatches: list) -> None:
        rows: list[ft.DataRow] = []
        for d in (dispatches or [])[:15]:
            ok = bool(d.get("ok"))
            ts = str(d.get("ts") or "—")
            if len(ts) > 19:
                ts = ts[:19].replace("T", " ")
            result = d.get("result")
            if result is None:
                summary = str(d.get("result_json") or "")[:80] or "—"
            elif isinstance(result, dict):
                summary = json.dumps(result, default=str)[:80]
            else:
                summary = str(result)[:80]
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(ts, color=MUTED, size=11)),
                ft.DataCell(ft.Text(
                    "OK" if ok else "FAILED", color=GREEN if ok else RED,
                    weight=ft.FontWeight.BOLD, size=11,
                )),
                ft.DataCell(ft.Text(summary, color=MUTED, size=10, selectable=True)),
            ]))
        if not rows:
            rows = [ft.DataRow(cells=[
                ft.DataCell(ft.Text(
                    "No data yet — journal populates as the agent trades", color=MUTED
                )),
                ft.DataCell(ft.Text("")),
                ft.DataCell(ft.Text("")),
            ])]
        self.sc_dispatch_table.rows = rows

    def _positions_summary(self, positions: list[dict]) -> str:
        if not positions:
            return "No open positions"
        bits = []
        for p in positions[:8]:
            sym = p.get("symbol") or "?"
            sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
            qty = p.get("quantity", p.get("qty", 0))
            try:
                qty_s = f"{float(qty):+g}"
            except (TypeError, ValueError):
                qty_s = str(qty)
            pnl = float(p.get("unrealized_pnl") or p.get("unrealizedPNL") or 0)
            bits.append(f"{sym} {sec} {qty_s}  uPnL {pnl:+.2f}")
        line = "  ·  ".join(bits)
        extra = len(positions) - 8
        if extra > 0:
            line = f"{line}  ·  +{extra} more"
        return f"{len(positions)} open  ·  {line}"

    def _position_rows(self, positions: list[dict]) -> list[ft.DataRow]:
        if not positions:
            return [ft.DataRow(cells=[ft.DataCell(ft.Text("No positions", color=MUTED))] * 6)]
        rows = []
        for p in positions[:50]:
            sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
            pnl = float(p.get("unrealized_pnl") or p.get("unrealizedPNL") or 0)
            qty = p.get("quantity", p.get("qty", 0))
            try:
                qty_s = f"{float(qty):+g}"
            except (TypeError, ValueError):
                qty_s = str(qty)
            if sec.startswith("OPT"):
                exp = p.get("expiration") or p.get("lastTradeDateOrContractMonth") or ""
                details = f"{exp} {p.get('strike', '')}{p.get('right', '')}"
            else:
                details = str(p.get("exchange") or "SMART")
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(
                    str(p.get("conId") or p.get("con_id") or "?"),
                    color=AMBER, selectable=True, weight=ft.FontWeight.W_600,
                )),
                ft.DataCell(ft.Text(str(p.get("symbol", "?")), color=TEXT)),
                ft.DataCell(ft.Text(sec, color=BLUE if sec.startswith("OPT") else MUTED)),
                ft.DataCell(ft.Text(qty_s, color=TEXT)),
                ft.DataCell(ft.Text(f"{pnl:+.2f}", color=GREEN if pnl >= 0 else RED)),
                ft.DataCell(ft.Text(details, color=MUTED, size=11)),
            ]))
        return rows


def _equity_spark_control(vals: list[float]) -> ft.Control:
    """Compact NetLiq spark: last value + unicode bars (no SVG / SPY overlay)."""
    if not vals:
        return ft.Text("Awaiting equity data — click Start", color=MUTED, size=12)
    color = GREEN if len(vals) < 2 or vals[-1] >= vals[0] else RED
    if len(vals) == 1:
        return ft.Text(f"${vals[0]:,.0f}", color=color, size=14, weight=ft.FontWeight.BOLD)
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    spark = "".join(blocks[min(7, int((v - lo) / span * 7))] for v in vals[-48:])
    ret = (vals[-1] / vals[0] - 1) * 100 if vals[0] else 0.0
    return ft.Column([
        ft.Text(f"${vals[-1]:,.0f}  ({ret:+.2f}%)", color=color, size=14,
                weight=ft.FontWeight.BOLD),
        ft.Text(spark, color=color, size=16),
    ], spacing=2)


def main(page: ft.Page) -> None:
    ProTerminal(page).build()


def write_launch_probe(path: str | Path) -> None:
    Path(path).write_text(
        f"title={TITLE}\nexpected={TITLE}\nstatus=Safe\nmainloop_ready=True\n",
        encoding="utf-8",
    )


def run_app() -> None:
    setup_file_logging()
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
