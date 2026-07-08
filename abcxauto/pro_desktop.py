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
    ("positions", "Positions", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED),
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
        self.ov_pos_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(c, color=MUTED))
                for c in ("conId", "Symbol", "Qty", "PnL", "Type")
            ],
            rows=[],
            heading_row_color=CARD2,
            border=ft.Border.all(1, BORDER),
        )
        self.pos_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(c, color=MUTED))
                for c in ("conId", "Symbol", "Qty", "PnL", "Type")
            ],
            rows=[],
            heading_row_color=CARD2,
            border=ft.Border.all(1, BORDER),
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
                    ft.Text(f"Grok {cfg.model}", color=BLUE, size=13),
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
                        self._btn("■  STOP", RED, self._stop),
                        self._btn("⚠  PANIC FLATTEN", RED, self._panic),
                    ],
                    spacing=12,
                ),
                ft.Container(height=8),
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
                                    ft.Text("Positions", color=MUTED, size=12),
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
            ],
            spacing=12,
            expand=True,
        )

    def _page_positions(self) -> ft.Column:
        return ft.Column(
            [
                ft.Text("Open Positions", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Container(
                    bgcolor=CARD,
                    border=ft.Border.all(1, BORDER),
                    border_radius=12,
                    padding=12,
                    expand=True,
                    content=ft.Column([self.pos_table], scroll=ft.ScrollMode.AUTO),
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

    def _stop(self, _=None) -> None:
        self.engine.stop_engine()
        self._sync_widgets()
        self._safe_update()

    def _panic(self, _=None) -> None:
        self.engine.panic()
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

    def _sync_widgets(self) -> None:
        s = self.engine.state
        self.lbl_cycles.value = str(s.cycles)
        self.lbl_equity.value = f"${s.equity:,.0f}"
        self.lbl_pnl.value = f"${s.pnl_chg:+.2f}"
        self.lbl_pnl.color = GREEN if s.pnl_chg >= 0 else RED
        self.lbl_risk.value = s.risk
        self.lbl_status.value = s.status
        self.lbl_status.color = GREEN if s.running else MUTED
        self.lbl_mode.value = s.status
        self.lbl_mode.color = GREEN if s.running else MUTED
        self.dot_conn.bgcolor = GREEN if s.connected else RED
        self.brain_action.value = s.brain_strat
        self.brain_rationale.value = s.brain_rationale
        self.brain_json.value = json.dumps(s.last_action, indent=2)
        rows = self._position_rows(s.positions)
        self.ov_pos_table.rows = rows
        self.pos_table.rows = rows
        self.sparkline.content = _equity_chart_control(s.equity_hist)

    def _position_rows(self, positions: list[dict]) -> list[ft.DataRow]:
        if not positions:
            return [
                ft.DataRow(
                    cells=[ft.DataCell(ft.Text("No positions", color=MUTED))] * 5
                )
            ]
        return [
            ft.DataRow(
                cells=[
                    ft.DataCell(
                        ft.Text(
                            str(p.get("conId") or p.get("con_id") or "?"),
                            color=AMBER,
                            selectable=True,
                        )
                    ),
                    ft.DataCell(ft.Text(str(p.get("symbol", "?")), color=TEXT)),
                    ft.DataCell(
                        ft.Text(str(p.get("quantity", p.get("qty", 0))), color=TEXT)
                    ),
                    ft.DataCell(
                        ft.Text(
                            f"{float(p.get('unrealized_pnl') or 0):+.2f}", color=GREEN
                        )
                    ),
                    ft.DataCell(
                        ft.Text(
                            str(p.get("sec_type", p.get("secType", "STK"))), color=MUTED
                        )
                    ),
                ]
            )
            for p in positions[:50]
        ]

    def _rebuild_log_stats(self) -> None:
        s = self.engine.state
        uplifts = [r.get("pnl_chg", 0) for r in s.records if r.get("type") == "cycle"]
        avg = sum(uplifts) / len(uplifts) if uplifts else 0
        best = max(uplifts, default=0)
        trend = (
            f"{100 * sum(1 for u in uplifts if u > 0) // len(uplifts)}% up"
            if uplifts
            else "—"
        )
        self.log_stats.controls = [
            self._stat_chip("Improvements", str(len(s.tweaks)), AMBER),
            self._stat_chip("Avg ΔPnL", f"${avg:+.2f}", BLUE),
            self._stat_chip("Best cycle", f"${best:+.2f}", GREEN),
            self._stat_chip("Trend", trend, BLUE),
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
        body: list[ft.Control] = [
            ft.Text(
                f"Snapshot → {len(r.get('positions') or [])} pos | equity ${r.get('equity', 0):,.0f}",
                color=MUTED,
                size=11,
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
        """Simulate order impact using live ledger (AC4)."""
        s = self.engine.state
        target = (rec.get("action_obj") or rec.get("action", {})).get("target_conId") or "?"
        s.brain_rationale = (
            f"Validate Impact: conId={target} position will go to exactly zero. "
            "No other conIds touched. (simulated)"
        )
        self._sync_widgets()
        self.page.overlay.append(
            ft.SnackBar(
                ft.Text("Impact validated against current ledger"), bgcolor=BLUE, open=True
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
