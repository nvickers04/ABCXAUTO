"""ABCXAUTO Pro Desktop v0.1 — Flet shell over ProEngine."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
from pathlib import Path
from typing import Any

import flet as ft

from abcxauto.config import get_config
from abcxauto.pro_engine import ProEngine
from abcxauto.reality_pulse import build_reality_pulse, pulse_clock_view
from abcxauto.rocket import TWEAKS

BG, CARD, CARD2, BORDER = "#0b0e14", "#151b26", "#1c2433", "#2a3548"
TEXT, MUTED, GREEN, RED, BLUE, AMBER = (
    "#e8edf4",
    "#8b9bb4",
    "#00d47e",
    "#ff4d6d",
    "#4dabf7",
    "#ffc857",
)
NAV = [
    ("overview", "Overview", ft.Icons.DASHBOARD_OUTLINED),
    ("positions", "Positions Ledger", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED),
    ("brain", "AI Brain", ft.Icons.PSYCHOLOGY_OUTLINED),
    ("logs", "Logs & Evolution", ft.Icons.TIMELINE_OUTLINED),
    ("settings", "Settings", ft.Icons.SETTINGS_OUTLINED),
]
FILTERS = [
    "All",
    "Trades",
    "Closes",
    "Decisions",
    "Improvements",
    "Errors",
    "Position Mismatches",
]
# Canonical window title — keep TITLE + PRO_TITLE in sync for launch probes.
TITLE = "ABCXAUTO Pro v0.1.1"
PRO_TITLE = TITLE


class ProTerminal:
    def __init__(self, page: ft.Page):
        self.page = page
        self.engine = ProEngine()
        self.tab = "overview"
        self.filter = "All"
        self.raw_json = False
        self.pin_insight = ""
        self.lbl_mode = ft.Text("Safe", size=20, color=MUTED)
        self._build_refs()
        self._load_pin_insight()
        self._sync_widgets()

    def _build_refs(self) -> None:
        self.lbl_equity = ft.Text("$0", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_pnl = ft.Text("$+0.00", size=16, weight=ft.FontWeight.W_600, color=GREEN)
        self.dot_conn = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.lbl_status = ft.Text("Safe", color=MUTED, size=12)
        self.lbl_cycles = ft.Text("0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_risk = ft.Text("—", color=MUTED)
        self.sparkline = ft.Container(height=220, expand=True)
        _pos_cols = ("conId", "Symbol", "Type", "Qty", "uPnL", "Details")
        self.ov_pos_table = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(c, color=MUTED)) for c in _pos_cols],
            rows=[],
            heading_row_color=CARD2,
            border=ft.Border.all(1, BORDER),
        )
        self.pos_table = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(c, color=MUTED)) for c in _pos_cols],
            rows=[],
            heading_row_color=CARD2,
            border=ft.Border.all(1, BORDER),
        )
        self.lbl_proposal = ft.Text(
            "No proposal yet — START AUTONOMOUS or wait for a cycle.",
            color=MUTED,
            size=12,
            selectable=True,
        )
        self.lbl_ledger_snippet = ft.Text("—", color=MUTED, size=10, selectable=True)
        # Market Clock (situational awareness heart on the chrome)
        self.lbl_clock = ft.Text("—", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_session_badge = ft.Text("—", size=11, weight=ft.FontWeight.W_600, color=AMBER)
        self.lbl_countdown = ft.Text("—", size=11, color=MUTED)
        self.lbl_data_age = ft.Text("data n/a", size=10, color=MUTED)
        self.lbl_pulse_narrative = ft.Text(
            "Reality Pulse idle — START for live awareness.",
            size=11,
            color=MUTED,
            selectable=True,
        )
        self.lbl_kahneman = ft.Text(
            "Kahneman System 2 idle — START for deliberative traces.",
            size=11,
            color=MUTED,
            selectable=True,
        )
        self.brain_action = ft.Text("—", size=18, color=BLUE)
        self.brain_rationale = ft.Text(
            "Start autonomous mode to see Grok decisions.", color=MUTED, selectable=True
        )
        self.brain_json = ft.Text("", selectable=True, size=11, color=AMBER)
        self.log_search = ft.TextField(
            hint_text="Search cycles, actions, tweaks…",
            border_color=BORDER,
            bgcolor=CARD,
            color=TEXT,
            expand=True,
            on_change=self._on_search,
        )
        self.log_filter = ft.Dropdown(
            value="All",
            options=[ft.dropdown.Option(f) for f in FILTERS],
            width=160,
            border_color=BORDER,
            bgcolor=CARD,
            color=TEXT,
            on_select=lambda e: self._set_filter(getattr(e.control, "value", None) or "All"),
        )
        self.log_stats = ft.Row(spacing=12)
        self.log_timeline = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)
        self.pin_insight_field = ft.TextField(
            multiline=True,
            min_lines=3,
            max_lines=6,
            hint_text="Pin Insight — jot ideas for next iteration…",
            border_color=BORDER,
            bgcolor=CARD,
            color=TEXT,
            expand=True,
            on_blur=self._save_pin_insight,
        )
        self.content = ft.Container(expand=True, padding=20)
        self.sidebar_btns: dict[str, ft.Container] = {}

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
        top = ft.Container(
            bgcolor=CARD,
            padding=ft.Padding.symmetric(horizontal=20, vertical=12),
            content=ft.Row(
                [
                    ft.Text("ABCXAUTO", weight=ft.FontWeight.BOLD, size=18, color=TEXT),
                    ft.Text("•", color=MUTED),
                    ft.Text("PAPER", color=AMBER, size=13, weight=ft.FontWeight.W_600),
                    ft.Text("•", color=MUTED),
                    ft.Text(f"Grok {cfg.model}", color=BLUE, size=13, weight=ft.FontWeight.W_600),
                    ft.Container(width=16),
                    # Live Market Clock — awareness heart in chrome
                    ft.Container(
                        bgcolor=CARD2,
                        border=ft.Border.all(1, BORDER),
                        border_radius=10,
                        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        self.lbl_clock,
                                        ft.Container(
                                            bgcolor="#1a2332",
                                            border_radius=6,
                                            padding=ft.Padding.symmetric(
                                                horizontal=8, vertical=2
                                            ),
                                            content=self.lbl_session_badge,
                                        ),
                                    ],
                                    spacing=8,
                                ),
                                ft.Row(
                                    [self.lbl_countdown, self.lbl_data_age],
                                    spacing=12,
                                ),
                            ],
                            spacing=2,
                            tight=True,
                        ),
                    ),
                    ft.Container(expand=True),
                    ft.Column(
                        [ft.Text("Equity", size=10, color=MUTED), self.lbl_equity],
                        spacing=0,
                        horizontal_alignment=ft.CrossAxisAlignment.END,
                    ),
                    ft.Container(width=20),
                    ft.Column(
                        [ft.Text("PnL", size=10, color=MUTED), self.lbl_pnl],
                        spacing=0,
                        horizontal_alignment=ft.CrossAxisAlignment.END,
                    ),
                    ft.Container(width=20),
                    ft.Row(
                        [self.dot_conn, ft.Text("IBKR", size=12, color=MUTED)], spacing=6
                    ),
                ]
            ),
        )
        # Avoid expand spacers inside unbounded Columns — they leave the
        # Flutter desktop client stuck on the "Working..." splash.
        side = ft.Container(
            width=200,
            bgcolor=CARD,
            padding=12,
            content=ft.Column(
                [
                    ft.Text("NAVIGATION", size=10, color=MUTED, weight=ft.FontWeight.BOLD),
                    *[self._nav_btn(k, label, icon) for k, label, icon in NAV],
                    ft.Container(height=24),
                    ft.Text("Engine", size=10, color=MUTED),
                    self.lbl_status,
                ],
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        body = ft.Row(
            [side, ft.VerticalDivider(width=1, color=BORDER), self.content],
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        root = ft.Column(
            [top, ft.Divider(height=1, color=BORDER), body],
            expand=True,
            spacing=0,
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

    def _nav_btn(self, key: str, label: str, icon) -> ft.Container:
        c = ft.Container(
            content=ft.Row(
                [ft.Icon(icon, size=18, color=MUTED), ft.Text(label, size=13, color=TEXT)],
                spacing=10,
            ),
            padding=10,
            border_radius=8,
            ink=True,
            on_click=lambda e, k=key: self._show_tab(k),
        )
        self.sidebar_btns[key] = c
        return c

    def _show_tab(self, key: str) -> None:
        self.tab = key
        for k, btn in self.sidebar_btns.items():
            btn.bgcolor = CARD2 if k == key else None
        builders = {
            "overview": self._page_overview,
            "positions": self._page_positions,
            "brain": self._page_brain,
            "logs": self._page_logs,
            "settings": self._page_settings,
        }
        self.content.content = builders[key]()
        self._safe_update()

    def _safe_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass

    def _card(self, title: str, value: ft.Control, sub: str = "") -> ft.Container:
        return ft.Container(
            bgcolor=CARD,
            border=ft.Border.all(1, BORDER),
            border_radius=12,
            padding=16,
            expand=True,
            content=ft.Column(
                [
                    ft.Text(title, size=11, color=MUTED, weight=ft.FontWeight.W_600),
                    value,
                    ft.Text(sub, size=10, color=MUTED) if sub else ft.Container(),
                ],
                spacing=4,
            ),
        )

    def _btn(self, text: str, color: str, on_click) -> ft.Button:
        return ft.Button(
            text,
            bgcolor=color,
            color="#fff",
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            on_click=on_click,
        )

    def _page_overview(self) -> ft.Column:
        return ft.Column(
            [
                ft.Row(
                    [
                        self._btn("▶  START AUTONOMOUS", GREEN, self._start),
                        self._btn("❚❚  PAUSE", AMBER, self._pause),
                        self._btn("⚠  PANIC FLATTEN", RED, self._panic),
                        self._btn("⚡  FORCE TWEAK", BLUE, self._force_tweak),
                        self._btn("✓  VALIDATE & EXECUTE", BLUE, self._validate_execute),
                    ],
                    spacing=10,
                    wrap=True,
                ),
                ft.Container(height=8),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=12,
                    content=ft.Column(
                        [
                            ft.Text(
                                "Reality Pulse",
                                color=MUTED,
                                size=11,
                                weight=ft.FontWeight.W_600,
                            ),
                            self.lbl_pulse_narrative,
                            ft.Container(height=6),
                            ft.Text(
                                "Kahneman System 2",
                                color=MUTED,
                                size=11,
                                weight=ft.FontWeight.W_600,
                            ),
                            self.lbl_kahneman,
                        ],
                        spacing=4,
                    ),
                ),
                ft.Row(
                    [
                        self._card("Cycles", self.lbl_cycles, "autonomous iterations"),
                        self._card("Risk", self.lbl_risk, "protection compliance"),
                        self._card("Mode", self.lbl_mode, "engine state"),
                    ],
                    spacing=12,
                ),
                ft.Row(
                    [
                        ft.Container(
                            bgcolor=CARD,
                            border=ft.Border.all(1, BORDER),
                            border_radius=12,
                            padding=16,
                            expand=2,
                            content=ft.Column(
                                [
                                    ft.Text("Equity Curve", color=MUTED, size=12),
                                    self.sparkline,
                                ],
                                expand=True,
                            ),
                        ),
                        ft.Container(
                            bgcolor=CARD,
                            border=ft.Border.all(1, BORDER),
                            border_radius=12,
                            padding=12,
                            expand=1,
                            content=ft.Column(
                                [
                                    ft.Text(
                                        "Live Positions (conId)",
                                        color=MUTED,
                                        size=12,
                                    ),
                                    ft.Column(
                                        [self.ov_pos_table],
                                        scroll=ft.ScrollMode.AUTO,
                                        expand=True,
                                    ),
                                ],
                                expand=True,
                            ),
                        ),
                    ],
                    spacing=12,
                    expand=True,
                ),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=14,
                    content=ft.Column(
                        [
                            ft.Text(
                                "Proposed order breakdown",
                                color=MUTED,
                                size=12,
                                weight=ft.FontWeight.W_600,
                            ),
                            self.lbl_proposal,
                        ],
                        spacing=6,
                    ),
                ),
            ],
            spacing=12,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def _page_positions(self) -> ft.Column:
        return ft.Column(
            [
                ft.Text(
                    "Positions Ledger",
                    size=20,
                    weight=ft.FontWeight.BOLD,
                    color=TEXT,
                ),
                ft.Text(
                    "conId is the single source of truth — STK and OPT for the same symbol "
                    "are distinct legs.",
                    color=MUTED,
                    size=12,
                ),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=12,
                    expand=True,
                    content=ft.Column(
                        [self.pos_table, ft.Divider(color=BORDER), self.lbl_ledger_snippet],
                        scroll=ft.ScrollMode.AUTO,
                    ),
                ),
            ],
            expand=True,
        )

    def _page_brain(self) -> ft.Column:
        return ft.Column(
            [
                ft.Text(
                    "AI Brain — Latest Grok Decision",
                    size=20,
                    weight=ft.FontWeight.BOLD,
                    color=TEXT,
                ),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=20,
                    content=ft.Column(
                        [
                            ft.Text("Strategy", color=MUTED, size=11),
                            self.brain_action,
                            ft.Divider(color=BORDER),
                            ft.Text("Rationale", color=MUTED, size=11),
                            self.brain_rationale,
                            ft.Divider(color=BORDER),
                            ft.Text("Raw JSON", color=MUTED, size=11),
                            self.brain_json,
                        ],
                        spacing=8,
                    ),
                ),
            ],
            expand=True,
        )

    def _page_logs(self) -> ft.Column:
        self._rebuild_log_stats()
        return ft.Column(
            [
                ft.Text("Logs & Evolution", size=22, weight=ft.FontWeight.BOLD, color=TEXT),
                self.log_stats,
                ft.Row(
                    [
                        self.log_search,
                        self.log_filter,
                        ft.Switch(
                            label="Raw JSON",
                            value=self.raw_json,
                            active_color=BLUE,
                            on_change=lambda e: self._toggle_raw(e.control.value),
                        ),
                        ft.TextButton("Export All", on_click=self._export_all),
                        ft.TextButton("Clear", on_click=self._clear_logs),
                    ]
                ),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=12,
                    expand=True,
                    content=self.log_timeline,
                ),
                ft.Text("Pin Insight", color=MUTED, size=11),
                self.pin_insight_field,
            ],
            spacing=10,
            expand=True,
        )

    def _page_settings(self) -> ft.Column:
        cfg = get_config()
        return ft.Column(
            [
                ft.Text("Settings", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=20,
                    content=ft.Column(
                        [
                            ft.Text(
                                f"XAI API Key: {'✓ set' if cfg.xai_api_key else '✗ missing'}",
                                color=TEXT,
                            ),
                            ft.Text(f"IBKR: {cfg.ibkr_host}:{cfg.ibkr_port}", color=MUTED),
                            ft.Text(
                                f"Cycle sleep: {TWEAKS.get('cycle_sleep_s', 8)}s", color=MUTED
                            ),
                            ft.Text(
                                f"Tweaks: {json.dumps(TWEAKS) or '{}'}",
                                size=11,
                                color=AMBER,
                                selectable=True,
                            ),
                            ft.Text(
                                "Fallback Tk cockpit: python -m abcxauto --tk",
                                size=11,
                                color=MUTED,
                            ),
                        ],
                        spacing=8,
                    ),
                ),
            ],
            expand=True,
        )

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

    def _force_tweak(self, _=None) -> None:
        msg = self.engine.force_tweak()
        self.page.overlay.append(
            ft.SnackBar(ft.Text(f"FORCE TWEAK: {msg}"), bgcolor=BLUE, open=True)
        )
        self._safe_update()

    def _validate_execute(self, _=None) -> None:
        impact = self.engine.validate_last_impact()
        if not impact.get("ok"):
            self.page.overlay.append(
                ft.SnackBar(
                    ft.Text(f"Blocked: {impact.get('message')}"),
                    bgcolor=RED,
                    open=True,
                )
            )
            self._sync_widgets()
            self._safe_update()
            return
        self.engine.execute_last_proposal()
        self.page.overlay.append(
            ft.SnackBar(
                ft.Text(impact.get("gate") or "Executing validated proposal…"),
                bgcolor=GREEN,
                open=True,
            )
        )
        self._sync_widgets()
        self._safe_update()

    async def _reveal_window(self) -> None:
        """Force the desktop client past the Working… splash once controls exist."""
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
            prev_cycles = self.engine.state.cycles
            self.engine.drain_apply()
            self._sync_widgets()
            if self.engine.state.cycles != prev_cycles and self.tab == "logs":
                self._refresh_timeline()
            self._safe_update()
            await asyncio.sleep(0.12)

    async def _clock_loop(self) -> None:
        """Refresh Market Clock even when the rocket is idle."""
        while True:
            try:
                pulse = self.engine.state.reality_pulse
                if not pulse:
                    pulse = build_reality_pulse(
                        ibkr_connected=self.engine.state.connected,
                        positions=self.engine.state.positions,
                        account=None,
                    )
                self._apply_clock(pulse)
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
            GREEN
            if status == "regular"
            else (AMBER if status in ("premarket", "postmarket") else MUTED)
        )
        self.lbl_countdown.value = view.get("countdown") or "—"
        self.lbl_data_age.value = f"data {view.get('data_age') or 'n/a'}"
        if pulse.get("narrative"):
            self.lbl_pulse_narrative.value = pulse["narrative"]

    def _sync_widgets(self) -> None:
        s = self.engine.state
        self.lbl_cycles.value = str(s.cycles)
        self.lbl_equity.value = f"${s.equity:,.0f}"
        self.lbl_pnl.value = f"${s.pnl_chg:+.2f}"
        self.lbl_pnl.color = GREEN if s.pnl_chg >= 0 else RED
        self.lbl_risk.value = s.risk
        self.lbl_status.value = s.status
        mode_color = (
            GREEN if s.running else (AMBER if getattr(s, "paused", False) else MUTED)
        )
        self.lbl_status.color = mode_color
        self.lbl_mode.value = s.status
        self.lbl_mode.color = mode_color
        self.dot_conn.bgcolor = GREEN if s.connected else RED
        self.brain_action.value = s.brain_strat
        self.brain_rationale.value = s.brain_rationale
        self.brain_json.value = json.dumps(s.last_action, indent=2)
        act = s.last_action or {}
        impact = getattr(s, "last_impact", None) or act.get("_impact") or {}
        self.lbl_proposal.value = (
            f"strategy={s.brain_strat}  target_conId={act.get('target_conId') or '—'}\n"
            f"params={json.dumps(act.get('params') or {}, default=str)[:280]}\n"
            f"result={json.dumps(s.last_result or {}, default=str)[:200]}\n"
            f"impact={impact.get('gate') or '—'}"
        )
        inv = getattr(s, "inventory", "") or ""
        self.lbl_ledger_snippet.value = inv[:2500] if inv else "Ledger empty"
        pulse = getattr(s, "reality_pulse", None) or {}
        if pulse:
            self._apply_clock(pulse)
        ktrace = getattr(s, "kahneman_trace", None) or ""
        kobj = getattr(s, "kahneman", None) or {}
        if ktrace:
            self.lbl_kahneman.value = ktrace[:1200]
        elif kobj:
            self.lbl_kahneman.value = json.dumps(kobj, default=str)[:1200]
        rows = self._position_rows(s.positions)
        self.ov_pos_table.rows = rows
        self.pos_table.rows = rows
        self.sparkline.content = _equity_chart_control(s.equity_hist)

    def _position_rows(self, positions: list[dict]) -> list[ft.DataRow]:
        if not positions:
            return [
                ft.DataRow(
                    cells=[ft.DataCell(ft.Text("No positions", color=MUTED))] * 6
                )
            ]
        rows = []
        for p in positions[:50]:
            sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
            pnl = float(
                p.get("unrealized_pnl")
                or p.get("unrealizedPNL")
                or 0
            )
            pnl_color = GREEN if pnl >= 0 else RED
            qty = p.get("quantity", p.get("qty", 0))
            try:
                qty_s = f"{float(qty):+g}"
            except (TypeError, ValueError):
                qty_s = str(qty)
            details = ""
            if sec.startswith("OPT"):
                exp = p.get("expiration") or p.get("lastTradeDateOrContractMonth") or ""
                details = f"{exp} {p.get('strike', '')}{p.get('right', '')}"
            else:
                details = str(p.get("exchange") or "SMART")
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(
                                str(p.get("conId") or p.get("con_id") or "?"),
                                color=AMBER,
                                selectable=True,
                                weight=ft.FontWeight.W_600,
                            )
                        ),
                        ft.DataCell(ft.Text(str(p.get("symbol", "?")), color=TEXT)),
                        ft.DataCell(
                            ft.Text(
                                sec,
                                color=BLUE if sec.startswith("OPT") else MUTED,
                            )
                        ),
                        ft.DataCell(ft.Text(qty_s, color=TEXT)),
                        ft.DataCell(ft.Text(f"{pnl:+.2f}", color=pnl_color)),
                        ft.DataCell(ft.Text(details, color=MUTED, size=11)),
                    ]
                )
            )
        return rows

    def _rebuild_log_stats(self) -> None:
        s = self.engine.state
        uplifts = [r.get("pnl_chg", 0) for r in s.records if r.get("type") == "cycle"]
        avg = sum(uplifts) / len(uplifts) if uplifts else 0
        best = max(uplifts, default=0)
        attempts = getattr(s, "close_attempts", 0) or 0
        ok = getattr(s, "close_ok", 0) or 0
        close_rate = f"{(100 * ok // attempts) if attempts else 0}% ({ok}/{attempts})"
        mismatches = getattr(s, "mismatches", 0) or 0
        self.log_stats.controls = [
            self._stat_chip("Improvements", str(len(s.tweaks)), AMBER),
            self._stat_chip("Avg ΔPnL", f"${avg:+.2f}", BLUE),
            self._stat_chip("Best cycle", f"${best:+.2f}", GREEN),
            self._stat_chip("Close success", close_rate, GREEN if attempts else MUTED),
            self._stat_chip("Mismatches", str(mismatches), RED if mismatches else GREEN),
            self._stat_chip("Cycles", str(s.cycles), TEXT),
        ]

    def _stat_chip(self, label: str, val: str, color: str) -> ft.Container:
        return ft.Container(
            bgcolor=CARD2,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            content=ft.Column(
                [
                    ft.Text(label, size=10, color=MUTED),
                    ft.Text(val, size=16, color=color, weight=ft.FontWeight.BOLD),
                ],
                spacing=2,
            ),
        )

    def _filtered_records(self) -> list[dict]:
        q = (self.log_search.value or "").lower()
        out = []
        for r in self.engine.state.records:
            if r.get("type") == "error" and self.filter not in ("All", "Errors"):
                continue
            if r.get("type") == "cycle":
                if self.filter == "Trades" and r.get("strat") in ("hold", None):
                    continue
                if self.filter == "Closes" and "close" not in str(
                    r.get("action", {})
                ).lower() and "flatten" not in str(r.get("action", {})).lower():
                    continue
                if self.filter == "Decisions" and not r.get("action_obj"):
                    continue
                if self.filter == "Improvements" and (
                    not r.get("tweak") or r.get("tweak") == "none"
                ):
                    continue
                if self.filter == "Errors":
                    continue
                if self.filter == "Position Mismatches":
                    if (
                        "mismatch" not in str(r).lower()
                        and "conid" not in str(r.get("validation", "")).lower()
                    ):
                        continue
            if q and q not in json.dumps(r, default=str).lower():
                continue
            out.append(r)
        return list(reversed(out))

    def _refresh_timeline(self) -> None:
        self._rebuild_log_stats()
        self.log_timeline.controls = []
        for r in self._filtered_records():
            if r.get("type") == "error":
                self.log_timeline.controls.append(self._err_card(r))
            elif r.get("type") == "panic":
                self.log_timeline.controls.append(self._panic_card(r))
            else:
                self.log_timeline.controls.append(self._cycle_card(r))
        if not self.log_timeline.controls:
            self.log_timeline.controls.append(
                ft.Text("No cycles yet — click START AUTONOMOUS", color=MUTED)
            )

    def _err_card(self, r: dict) -> ft.Container:
        return ft.Container(
            bgcolor="#2a1520",
            border=ft.Border.all(1, RED),
            border_radius=10,
            padding=12,
            content=ft.Text(r.get("msg", "error"), color=RED, selectable=True),
        )

    def _panic_card(self, r: dict) -> ft.Container:
        body = [ft.Text("PANIC FLATTEN — per-position closes", color=RED, weight=ft.FontWeight.BOLD)]
        before = r.get("before_ledger") or []
        if before:
            body.append(ft.Text("Before ledger:", color=MUTED, size=10))
            for p in before[:5]:
                cid = p.get("conId") or p.get("con_id") or "?"
                body.append(
                    ft.Text(
                        f"  conId={cid} {p.get('symbol')} {p.get('sec_type', 'STK')} pos={p.get('quantity')}",
                        size=9,
                        color=MUTED,
                        selectable=True,
                    )
                )
        for pr in r.get("position_results") or []:
            cid = pr.get("conId") or pr.get("con_id") or "?"
            body.append(
                ft.Text(
                    f"conId={cid} via {pr.get('method', '?')} → {'OK' if pr.get('success') else 'FAIL'}",
                    color=GREEN if pr.get("success") else RED,
                    size=11,
                    selectable=True,
                )
            )
            if pr.get("reasoning"):
                body.append(
                    ft.Text(pr["reasoning"], color=MUTED, size=10, selectable=True)
                )
        return ft.Container(
            bgcolor="#2a1018",
            border=ft.Border.all(1, RED),
            border_radius=10,
            padding=12,
            content=ft.Column(body, spacing=4),
        )

    def _cycle_card(self, r: dict) -> ft.Container:
        n, strat, chg = r.get("cycle", 0), r.get("strat", "hold"), r.get("pnl_chg", 0)
        has_tw = r.get("tweak") and r.get("tweak") != "none"
        inv = r.get("inventory") or ""
        pulse = r.get("reality_pulse") or {}
        narrative = (pulse.get("narrative") if isinstance(pulse, dict) else None) or ""
        body: list[ft.Control] = [
            ft.Text(
                f"Snapshot → {len(r.get('positions') or [])} pos | equity ${r.get('equity', 0):,.0f}",
                color=MUTED,
                size=11,
            ),
            (
                ft.Container(
                    bgcolor="#121820",
                    border_radius=8,
                    padding=8,
                    content=ft.Column(
                        [
                            ft.Text(
                                "Reality Pulse",
                                size=10,
                                color=AMBER,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                narrative or json.dumps(pulse, default=str)[:600],
                                size=10,
                                color=MUTED,
                                selectable=True,
                            ),
                        ],
                        spacing=2,
                    ),
                )
                if pulse
                else ft.Container()
            ),
            (
                ft.Container(
                    bgcolor="#141a12",
                    border_radius=8,
                    padding=8,
                    content=ft.Column(
                        [
                            ft.Text(
                                "Kahneman System 2",
                                size=10,
                                color=GREEN,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                r.get("kahneman_trace")
                                or json.dumps(r.get("kahneman") or {}, default=str)[:800]
                                or "—",
                                size=10,
                                color=MUTED,
                                selectable=True,
                            ),
                        ],
                        spacing=2,
                    ),
                )
                if (r.get("kahneman_trace") or r.get("kahneman"))
                else ft.Container()
            ),
            (
                ft.Text(
                    f"Inventory (before/after snapshot):\n{inv or '—'}",
                    color=MUTED,
                    size=9,
                    selectable=True,
                )
                if inv
                else ft.Container()
            ),
            ft.Text(
                f"Reasoning: {r.get('reasoning_chain') or r.get('rationale') or '—'}",
                color=TEXT,
                size=12,
                selectable=True,
            ),
            ft.Text(
                f"Validation: {r.get('validation') or '—'}",
                color=AMBER,
                size=10,
                selectable=True,
            ),
            ft.Text(
                f"Action: {strat} → {json.dumps(r.get('result', {}), default=str)[:200]}",
                color=BLUE,
                size=11,
                selectable=True,
            ),
            ft.Text(f"PnL Δ ${chg:+.2f}", color=GREEN if chg >= 0 else RED, size=12),
            ft.TextButton(
                "Validate Order Impact", on_click=lambda e, rec=r: self._validate_impact(rec)
            ),
        ]
        if has_tw:
            tw_obj, before = r.get("tweak_obj") or {}, r.get("tweak_before") or {}
            body.insert(
                0,
                ft.Container(
                    bgcolor="#2a2510",
                    border_radius=8,
                    padding=10,
                    margin=ft.Margin.only(bottom=8),
                    content=ft.Column(
                        [
                            ft.Text(
                                f"✦ Self-tweak: {r.get('tweak')}",
                                color=AMBER,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("Before", size=10, color=MUTED),
                                            ft.Text(
                                                _diff_text(
                                                    before, tw_obj.get("config") or {}, "before"
                                                ),
                                                size=11,
                                                color=RED,
                                                selectable=True,
                                            ),
                                        ],
                                        expand=True,
                                    ),
                                    ft.Column(
                                        [
                                            ft.Text("After", size=10, color=MUTED),
                                            ft.Text(
                                                _diff_text(
                                                    before, tw_obj.get("config") or {}, "after"
                                                ),
                                                size=11,
                                                color=GREEN,
                                                selectable=True,
                                            ),
                                        ],
                                        expand=True,
                                    ),
                                ]
                            ),
                            ft.Row(
                                [
                                    ft.TextButton(
                                        "Apply Again",
                                        on_click=lambda e, o=tw_obj: self._apply_tweak(o),
                                    ),
                                    ft.TextButton(
                                        "Grok Deep Analyze",
                                        on_click=lambda e, o=tw_obj: self._analyze_tweak(o),
                                    ),
                                    ft.TextButton(
                                        "Copy JSON",
                                        on_click=lambda e, o=tw_obj: self._copy(
                                            json.dumps(o, indent=2)
                                        ),
                                    ),
                                    ft.TextButton(
                                        "Replay Cycle",
                                        on_click=lambda e, rec=r: self._replay(rec),
                                    ),
                                    ft.TextButton(
                                        "Validate Order Impact",
                                        on_click=lambda e, rec=r: self._validate_impact(rec),
                                    ),
                                ]
                            ),
                        ],
                        spacing=4,
                    ),
                ),
            )
        if self.raw_json:
            body.append(
                ft.Text(
                    json.dumps(r, indent=2, default=str)[:3000],
                    size=10,
                    color=AMBER,
                    selectable=True,
                )
            )
        return ft.Container(
            bgcolor=CARD2 if has_tw else CARD,
            border=ft.Border.all(1, AMBER if has_tw else BORDER),
            border_radius=10,
            padding=0,
            content=ft.ExpansionTile(
                title=ft.Text(
                    f"Cycle {n}  •  {strat}  •  Δ${chg:+.2f}",
                    color=AMBER if has_tw else TEXT,
                    weight=ft.FontWeight.W_600,
                ),
                subtitle=ft.Text(r.get("ts", ""), size=10, color=MUTED),
                controls=[ft.Container(padding=12, content=ft.Column(body, spacing=6))],
            ),
        )

    def _apply_tweak(self, tw: dict) -> None:
        msg = self.engine.apply_tweak_manual(tw)
        self.page.overlay.append(
            ft.SnackBar(ft.Text(f"Applied: {msg}"), bgcolor=GREEN, open=True)
        )
        self._safe_update()

    def _analyze_tweak(self, tw: dict) -> None:
        threading.Thread(
            target=lambda: asyncio.run(self.engine.grok_analyze_tweak(tw)), daemon=True
        ).start()

    def _replay(self, rec: dict) -> None:
        s = self.engine.state
        s.brain_strat = rec.get("strat", "—")
        s.brain_rationale = rec.get("rationale", "—")
        s.last_action = rec.get("action_obj") or {}
        if tw := rec.get("tweak_obj"):
            self._apply_tweak(tw)
        self._sync_widgets()
        self._show_tab("brain")

    def _validate_impact(self, rec: dict) -> None:
        """Simulate order impact using live ledger (AC4) — conId-level gate."""
        from abcxauto.rocket import simulate_close_impact

        s = self.engine.state
        act = rec.get("action_obj") or rec.get("action") or {}
        impact = simulate_close_impact(act, s.positions or rec.get("positions") or [])
        s.last_impact = impact
        s.brain_rationale = impact.get("gate") or "—"
        s.brain_strat = f"validate:{act.get('strategy') or act.get('action') or '?'}"
        self._sync_widgets()
        self.page.overlay.append(
            ft.SnackBar(
                ft.Text(impact.get("gate") or "validated"),
                bgcolor=GREEN if impact.get("ok") else RED,
                open=True,
            )
        )
        self._safe_update()

    def _copy(self, text: str) -> None:
        try:
            if hasattr(self.page, "set_clipboard"):
                self.page.set_clipboard(text)
            elif hasattr(self.page, "clipboard"):
                self.page.clipboard.set(text)
            self.page.overlay.append(ft.SnackBar(ft.Text("Copied"), bgcolor=BLUE, open=True))
        except Exception as e:
            self.page.overlay.append(
                ft.SnackBar(ft.Text(f"Copy failed: {e}"), bgcolor=RED, open=True)
            )
        self._safe_update()

    def _set_filter(self, val: str) -> None:
        self.filter = val or "All"
        self._refresh_timeline()
        self._safe_update()

    def _on_search(self, _=None) -> None:
        self._refresh_timeline()
        self._safe_update()

    def _toggle_raw(self, val: bool) -> None:
        self.raw_json = val
        self._refresh_timeline()
        self._safe_update()

    def _save_pin_insight(self, _=None) -> None:
        self.pin_insight = self.pin_insight_field.value or ""
        try:
            Path("pin_insight.txt").write_text(self.pin_insight, encoding="utf-8")
        except OSError:
            pass

    def _load_pin_insight(self) -> None:
        try:
            self.pin_insight = Path("pin_insight.txt").read_text(encoding="utf-8")
            self.pin_insight_field.value = self.pin_insight
        except OSError:
            pass

    def _export_all(self, _=None) -> None:
        s = self.engine.state
        payload = json.dumps(
            {"records": s.records, "tweaks": s.tweaks, "pin_insight": self.pin_insight},
            indent=2,
            default=str,
        )
        try:
            Path("logs_export.json").write_text(payload, encoding="utf-8")
        except OSError:
            pass
        self._copy(payload)

    def _clear_logs(self, _=None) -> None:
        self.engine.clear_logs()
        self._refresh_timeline()
        self._safe_update()


def _equity_chart_control(vals: list[float]) -> ft.Control:
    w, h = 640, 200
    if not vals:
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
            f'<rect width="{w}" height="{h}" fill="#0f131a" rx="8"/>'
            f'<text x="{w // 2}" y="{h // 2}" fill="#8b9bb4" font-size="12" '
            f'text-anchor="middle">Awaiting equity data — click START</text></svg>'
        )
        return ft.Image(
            src=f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}",
            height=h,
            fit=ft.BoxFit.CONTAIN,
        )
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.1, 500) if len(vals) > 1 else 500
    lo, hi = lo - pad, hi + pad
    span, color = hi - lo or 1, GREEN if vals[-1] >= vals[0] else RED
    if len(vals) == 1:
        x, y = w // 2, h - 30 - (vals[0] - lo) / span * (h - 50)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
            f'<rect width="{w}" height="{h}" fill="#0f131a" rx="8"/>'
            f'<circle cx="{x}" cy="{y:.1f}" r="5" fill="{color}"/>'
            f'<text x="44" y="18" fill="#8b9bb4" font-size="11">${vals[0]:,.0f}</text></svg>'
        )
        return ft.Image(
            src=f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}",
            height=h,
            fit=ft.BoxFit.CONTAIN,
        )
    xs = [40 + i * (w - 60) / (len(vals) - 1) for i in range(len(vals))]
    ys = [h - 30 - (v - lo) / span * (h - 50) for v in vals]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = (
        f"M{xs[0]:.1f},{h - 30} L"
        + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        + f" L{xs[-1]:.1f},{h - 30} Z"
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.35"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/>'
        f"</linearGradient></defs>"
        f'<rect width="{w}" height="{h}" fill="#0f131a" rx="8"/>'
        f'<path d="{area}" fill="url(#g)"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        + "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
            for x, y in zip(xs, ys)
        )
        + f'<text x="44" y="18" fill="#8b9bb4" font-size="11">${vals[-1]:,.0f}</text></svg>'
    )
    return ft.Image(
        src=f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}",
        height=h,
        fit=ft.BoxFit.CONTAIN,
    )


def _diff_text(before: dict, cfg: dict, side: str) -> str:
    after = {**before, **cfg}
    lines = []
    for k in sorted(set(before) | set(cfg)):
        if side == "before":
            lines.append(f"{k}: {before.get(k, '—')}")
        else:
            lines.append(f"{k}: {after.get(k, '—')}{'  ←' if k in cfg else ''}")
    return "\n".join(lines) or "(empty)"


def main(page: ft.Page) -> None:
    ProTerminal(page).build()


def write_launch_probe(path: str | Path) -> None:
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
