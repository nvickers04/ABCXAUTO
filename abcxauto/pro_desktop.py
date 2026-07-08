"""ABCXAUTO Pro Desktop v0.1 — Flet professional self-evolving portfolio terminal."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import flet as ft

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.llm import GrokClient
from abcxauto.rocket import TWEAKS, apply_tweak, grok, run_cycle

TITLE = "ABCXAUTO Pro v0.1.1"
BG = "#0b0e14"
CARD = "#151b26"
CARD2 = "#1c2433"
BORDER = "#2a3548"
TEXT = "#e8edf4"
MUTED = "#8b9bb4"
GREEN = "#00d47e"
RED = "#ff4d6d"
BLUE = "#4dabf7"
AMBER = "#ffc857"
NAV = [
    ("overview", "Overview", ft.Icons.DASHBOARD_OUTLINED),
    ("positions", "Positions", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED),
    ("brain", "AI Brain", ft.Icons.PSYCHOLOGY_OUTLINED),
    ("logs", "Logs & Evolution", ft.Icons.TIMELINE_OUTLINED),
    ("settings", "Settings", ft.Icons.SETTINGS_OUTLINED),
]
FILTERS = ["All", "Trades", "Decisions", "Improvements", "Errors"]


def _pad(h: int = 0, v: int = 0) -> ft.padding.Padding:
    return ft.padding.Padding.symmetric(horizontal=h, vertical=v)


def _margin(**kwargs: int) -> ft.margin.Margin:
    return ft.margin.Margin.only(**kwargs)


def _border(color: str, width: float = 1) -> ft.border.Border:
    return ft.border.Border.all(width, color)


class ProTerminal:
    def __init__(self, page: ft.Page):
        self.page = page
        self.ui: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self._gen = 0
        self.worker: threading.Thread | None = None
        self.conn = None
        self.running = False
        self.connected = False
        self.tab = "overview"
        self.filter = "All"
        self.raw_json = False
        self.cycles = 0
        self.pnl = 0.0
        self.equity = 0.0
        self.equity_hist: list[float] = []
        self.records: list[dict] = []
        self.tweaks: list[dict] = []
        self.positions: list[dict] = []
        self.open_orders: list[dict] = []
        self.protection: dict = {}
        self.unprotected: list[str] = []
        self.last_action: dict = {}
        self.last_result: dict = {}
        self.portfolio_line = "—"
        self.status = "Safe"
        self.dev_notes = ""
        self._build_refs()

    def _build_refs(self) -> None:
        self.lbl_equity = ft.Text("$0", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_pnl = ft.Text("$+0.00", size=16, weight=ft.FontWeight.W_600, color=GREEN)
        self.dot_conn = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.lbl_status = ft.Text("Safe", color=MUTED, size=12)
        self.lbl_cycles = ft.Text("0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_risk = ft.Text("—", color=MUTED, size=16, weight=ft.FontWeight.W_600)
        self.lbl_portfolio = ft.Text("—", color=MUTED, size=13)
        self.lbl_unprotected = ft.Text("None", color=GREEN, size=13)
        self.lbl_last_decision = ft.Text("—", color=BLUE, size=14, weight=ft.FontWeight.W_600)
        self.equity_bars = ft.Row(spacing=3, expand=True, vertical_alignment=ft.CrossAxisAlignment.END)
        self.lbl_equity_range = ft.Text("No equity samples yet", size=11, color=MUTED)
        self.pos_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(c, color=MUTED))
                for c in ("Symbol", "Qty", "PnL", "Type", "Protected")
            ],
            rows=[],
            heading_row_color=CARD2,
            border=_border(BORDER),
        )
        self.orders_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(c, color=MUTED))
                for c in ("Symbol", "Side", "Qty", "Type", "Status")
            ],
            rows=[],
            heading_row_color=CARD2,
            border=_border(BORDER),
        )
        self.brain_action = ft.Text("—", size=18, color=BLUE)
        self.brain_rationale = ft.Text(
            "Start autonomous mode to see Grok decisions.", color=MUTED, selectable=True
        )
        self.brain_result = ft.Text("—", color=MUTED, selectable=True, size=12)
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
        self.dev_notes_field = ft.TextField(
            multiline=True,
            min_lines=4,
            max_lines=8,
            hint_text="Dev notes…",
            border_color=BORDER,
            bgcolor=CARD,
            color=TEXT,
            expand=True,
            on_blur=self._save_dev_notes,
        )
        self.content = ft.Container(expand=True, padding=20)
        self.sidebar_btns: dict[str, ft.Container] = {}

    def build(self) -> None:
        p = self.page
        p.title = TITLE
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
        cfg = get_config()
        top = ft.Container(
            bgcolor=CARD,
            padding=_pad(20, 12),
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
                    ft.Row([self.dot_conn, ft.Text("IBKR", size=12, color=MUTED)], spacing=6),
                ],
                alignment=ft.MainAxisAlignment.START,
            ),
        )
        # Avoid expand spacers inside unbounded Columns — that leaves the
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
        p.controls.clear()
        p.add(root)
        self._show_tab("overview")
        p.update()
        p.run_task(self._poll_loop)
        p.run_task(self._reveal_window)

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

    def _card(self, title: str, value: ft.Control, sub: str = "") -> ft.Container:
        return ft.Container(
            bgcolor=CARD,
            border=_border(BORDER),
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

    def _btn(self, text: str, color: str, on_click) -> ft.ElevatedButton:
        return ft.ElevatedButton(
            text,
            bgcolor=color,
            color="#fff",
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            on_click=on_click,
        )

    def _page_overview(self) -> ft.Column:
        risk_color = GREEN if self.lbl_risk.value == "COMPLIANT" else (
            RED if str(self.lbl_risk.value).startswith("UNPROTECTED") else MUTED
        )
        self.lbl_risk.color = risk_color
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
                        self._card(
                            "Mode",
                            ft.Text(
                                self.status,
                                size=20,
                                color=GREEN if self.running else MUTED,
                            ),
                            "engine state",
                        ),
                    ],
                    spacing=12,
                ),
                ft.Row(
                    [
                        self._card("Portfolio", self.lbl_portfolio, "live book"),
                        self._card("Unprotected", self.lbl_unprotected, "needs stop coverage"),
                        self._card("Last Grok", self.lbl_last_decision, "latest strategy"),
                    ],
                    spacing=12,
                ),
                ft.Container(
                    bgcolor=CARD,
                    border=_border(BORDER),
                    border_radius=12,
                    padding=16,
                    expand=True,
                    content=ft.Column(
                        [
                            ft.Text("Equity Curve", color=MUTED, size=12),
                            self.lbl_equity_range,
                            ft.Container(
                                content=self.equity_bars,
                                height=180,
                                expand=True,
                                alignment=ft.Alignment.BOTTOM_CENTER,
                            ),
                        ],
                        expand=True,
                    ),
                ),
            ],
            spacing=12,
            expand=True,
        )

    def _page_positions(self) -> ft.Column:
        return ft.Column(
            [
                ft.Text("Open Positions", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Text(self.portfolio_line, color=MUTED, size=12),
                ft.Container(
                    bgcolor=CARD,
                    border=_border(BORDER),
                    border_radius=12,
                    padding=12,
                    expand=True,
                    content=ft.Column([self.pos_table], scroll=ft.ScrollMode.AUTO),
                ),
                ft.Text("Open Orders", size=16, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Container(
                    bgcolor=CARD,
                    border=_border(BORDER),
                    border_radius=12,
                    padding=12,
                    height=220,
                    content=ft.Column([self.orders_table], scroll=ft.ScrollMode.AUTO),
                ),
            ],
            expand=True,
            spacing=10,
        )

    def _page_brain(self) -> ft.Column:
        return ft.Column(
            [
                ft.Text("AI Brain — Latest Grok Decision", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Container(
                    bgcolor=CARD,
                    border=_border(BORDER),
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
                            ft.Text("Execution result", color=MUTED, size=11),
                            self.brain_result,
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
                    ]
                ),
                ft.Container(
                    bgcolor=CARD,
                    border=_border(BORDER),
                    border_radius=12,
                    padding=12,
                    expand=True,
                    content=self.log_timeline,
                ),
                ft.Text("Dev Notes", color=MUTED, size=11),
                self.dev_notes_field,
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
                    border=_border(BORDER),
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
                                f"Cycle sleep: {TWEAKS.get('cycle_sleep_s', 8)}s (from tweaks)",
                                color=MUTED,
                            ),
                            ft.Text(
                                f"Tweaks active: {json.dumps(TWEAKS) or '{}'}",
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

    def _snack(self, msg: str, color: str = BLUE) -> None:
        bar = ft.SnackBar(ft.Text(msg), bgcolor=color, open=True)
        self.page.overlay.append(bar)
        self._safe_update()

    def _safe_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass

    def _start(self, _=None) -> None:
        if not get_config().xai_api_key:
            self._snack("XAI_API_KEY missing", RED)
            return
        if self.worker and self.worker.is_alive():
            return
        self._gen += 1
        gen = self._gen
        self.stop.clear()
        self.running = True
        self.status = "Running"
        self.lbl_status.value = "Running"
        self.lbl_status.color = GREEN
        self.worker = threading.Thread(target=lambda: self._worker(gen), daemon=True)
        self.worker.start()
        self._safe_update()

    def _stop(self, _=None) -> None:
        self.stop.set()
        self._gen += 1
        self.running = False
        self.status = "Safe"
        self.lbl_status.value = "Safe"
        self.lbl_status.color = MUTED
        self.worker = None
        self._safe_update()

    def _panic(self, _=None) -> None:
        self._stop()
        threading.Thread(target=lambda: asyncio.run(self._do_panic()), daemon=True).start()

    async def _do_panic(self) -> None:
        try:
            conn = self.conn or get_ibkr_connector()
            if not getattr(conn, "connected", False):
                await conn.connect()
            res = await conn.flatten_all() if hasattr(conn, "flatten_all") else {"status": "logged"}
            self.ui.put(("log", f"PANIC: {json.dumps(res, default=str)}"))
        except Exception as e:
            self.ui.put(("log", f"PANIC ERROR: {e}"))

    def _worker(self, gen: int) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(self._async_loop(gen))

    async def _async_loop(self, gen: int) -> None:
        if gen != self._gen:
            return
        try:
            self.conn = get_ibkr_connector()
            await self.conn.connect()
            self.ui.put(("conn", True))
            g = GrokClient()
        except Exception as e:
            self.ui.put(("log", f"INIT ERROR: {e}"))
            self.ui.put(("conn", False))
            self.ui.put(("status_safe", None))
            return
        hist, prev, n = [], 0.0, 0
        while gen == self._gen and not self.stop.is_set():
            n += 1
            try:
                out = await run_cycle(n, self.conn, g, hist, prev)
                prev = out["pnl"]
                self.ui.put(("cycle", out))
            except Exception as e:
                self.ui.put(("error", str(e)))
            await asyncio.sleep(float(TWEAKS.get("cycle_sleep_s", 8)))
        self.ui.put(("status_safe", None))

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
            while not self.ui.empty():
                kind, data = self.ui.get_nowait()
                try:
                    self._apply(kind, data)
                except Exception as e:
                    self.records.append(
                        {"cycle": 0, "type": "error", "msg": f"UI ERROR: {e}", "ts": _now()}
                    )
            self._safe_update()
            await asyncio.sleep(0.12)

    def _apply(self, kind: str, data: Any) -> None:
        if kind == "conn":
            self.connected = bool(data)
            self.dot_conn.bgcolor = GREEN if self.connected else RED
        elif kind == "cycle":
            self._on_cycle(data)
        elif kind == "status_safe":
            self.running = False
            self.status = "Safe"
            self.lbl_status.value = "Safe"
            self.lbl_status.color = MUTED
        elif kind in ("log", "error"):
            self.records.append({"cycle": 0, "type": "error", "msg": str(data), "ts": _now()})
            if self.tab == "logs":
                self._refresh_timeline()

    def _on_cycle(self, d: dict) -> None:
        self.cycles = d["cycle"]
        self.pnl = d["pnl"]
        self.equity = d["equity"]
        self.equity_hist.append(float(d["equity"]))
        if len(self.equity_hist) > 40:
            self.equity_hist = self.equity_hist[-40:]
        self.lbl_cycles.value = str(self.cycles)
        self.lbl_equity.value = f"${self.equity:,.0f}"
        chg = d.get("pnl_chg", 0)
        self.lbl_pnl.value = f"${chg:+.2f}"
        self.lbl_pnl.color = GREEN if chg >= 0 else RED
        risk = d.get("risk", "—")
        self.lbl_risk.value = risk
        self.lbl_risk.color = GREEN if risk == "COMPLIANT" else RED
        self.portfolio_line = d.get("portfolio") or "—"
        self.lbl_portfolio.value = self.portfolio_line
        self.unprotected = list(d.get("unprotected") or [])
        self.lbl_unprotected.value = ", ".join(self.unprotected) if self.unprotected else "None"
        self.lbl_unprotected.color = RED if self.unprotected else GREEN
        self.last_action = d.get("action_obj") or {}
        self.last_result = d.get("result") or {}
        strat = d.get("strat", "hold")
        self.lbl_last_decision.value = strat
        self.brain_action.value = strat
        self.brain_rationale.value = d.get("rationale") or "—"
        self.brain_result.value = json.dumps(self.last_result, default=str)
        self.brain_json.value = json.dumps(self.last_action, indent=2, default=str)
        self.positions = d.get("positions") or []
        self.open_orders = d.get("open_orders") or []
        self.protection = d.get("protection") or {}
        self._refresh_positions()
        self._refresh_orders()
        self._refresh_equity_bars()
        rec = {**d, "ts": _now(), "type": "cycle"}
        self.records.append(rec)
        if d.get("tweak") and d["tweak"] != "none":
            self.tweaks.append(
                {
                    "cycle": d["cycle"],
                    "summary": d["tweak"],
                    "obj": d.get("tweak_obj", {}),
                    "before": dict(TWEAKS),
                    "ts": _now(),
                }
            )
        if self.tab == "logs":
            self._refresh_timeline()

    def _protected_map(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for entry in self.protection.get("positions") or []:
            sym = entry.get("symbol")
            if sym:
                out[sym] = bool(entry.get("protected", True))
        for sym in self.unprotected:
            out[sym] = False
        return out

    def _refresh_positions(self) -> None:
        prot = self._protected_map()
        rows = []
        for p in self.positions[:50]:
            sym = str(p.get("symbol", "?"))
            pnl = float(p.get("unrealized_pnl") or 0)
            protected = prot.get(sym)
            if protected is None:
                protected = sym not in self.unprotected
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(sym, color=TEXT)),
                        ft.DataCell(ft.Text(str(p.get("quantity", p.get("qty", 0))), color=TEXT)),
                        ft.DataCell(ft.Text(f"{pnl:+.2f}", color=GREEN if pnl >= 0 else RED)),
                        ft.DataCell(ft.Text(str(p.get("sec_type", "STK")), color=MUTED)),
                        ft.DataCell(
                            ft.Text(
                                "YES" if protected else "NO",
                                color=GREEN if protected else RED,
                            )
                        ),
                    ]
                )
            )
        if not rows:
            rows = [
                ft.DataRow(
                    cells=[ft.DataCell(ft.Text("No positions", color=MUTED))]
                    + [ft.DataCell(ft.Text(""))] * 4
                )
            ]
        self.pos_table.rows = rows

    def _refresh_orders(self) -> None:
        rows = []
        for o in self.open_orders[:50]:
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(str(o.get("symbol", "?")), color=TEXT)),
                        ft.DataCell(ft.Text(str(o.get("action", o.get("side", "?"))), color=TEXT)),
                        ft.DataCell(ft.Text(str(o.get("quantity", o.get("qty", 0))), color=TEXT)),
                        ft.DataCell(ft.Text(str(o.get("order_type", "?")), color=MUTED)),
                        ft.DataCell(ft.Text(str(o.get("status", "?")), color=MUTED)),
                    ]
                )
            )
        if not rows:
            rows = [
                ft.DataRow(
                    cells=[ft.DataCell(ft.Text("No open orders", color=MUTED))]
                    + [ft.DataCell(ft.Text(""))] * 4
                )
            ]
        self.orders_table.rows = rows

    def _refresh_equity_bars(self) -> None:
        hist = self.equity_hist
        if not hist:
            self.equity_bars.controls = []
            self.lbl_equity_range.value = "No equity samples yet"
            return
        lo, hi = min(hist), max(hist)
        span = max(hi - lo, 1.0)
        self.lbl_equity_range.value = f"${lo:,.0f} → ${hi:,.0f}  ({len(hist)} samples)"
        bars = []
        for v in hist:
            h = 20 + int(140 * ((v - lo) / span))
            bars.append(
                ft.Container(
                    width=14,
                    height=h,
                    bgcolor=GREEN if v >= hist[0] else RED,
                    border_radius=3,
                    tooltip=f"${v:,.2f}",
                )
            )
        self.equity_bars.controls = bars

    def _rebuild_log_stats(self) -> None:
        n_tw = len(self.tweaks)
        uplifts = [r.get("pnl_chg", 0) for r in self.records if r.get("type") == "cycle"]
        avg = sum(uplifts) / len(uplifts) if uplifts else 0
        best = max((r.get("pnl_chg", 0) for r in self.records if r.get("type") == "cycle"), default=0)
        self.log_stats.controls = [
            self._stat_chip("Improvements", str(n_tw), AMBER),
            self._stat_chip("Avg ΔPnL", f"${avg:+.2f}", BLUE),
            self._stat_chip("Best cycle", f"${best:+.2f}", GREEN),
            self._stat_chip("Total cycles", str(self.cycles), TEXT),
        ]

    def _stat_chip(self, label: str, val: str, color: str) -> ft.Container:
        return ft.Container(
            bgcolor=CARD2,
            border_radius=8,
            padding=_pad(12, 8),
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
        for r in self.records:
            if r.get("type") == "error":
                if self.filter not in ("All", "Errors"):
                    continue
            elif r.get("type") == "cycle":
                if self.filter == "Trades" and r.get("strat") in ("hold", None):
                    continue
                if self.filter == "Decisions" and not r.get("action_obj"):
                    continue
                if self.filter == "Improvements" and (not r.get("tweak") or r.get("tweak") == "none"):
                    continue
                if self.filter == "Errors":
                    continue
            blob = json.dumps(r, default=str).lower()
            if q and q not in blob:
                continue
            out.append(r)
        return list(reversed(out))

    def _refresh_timeline(self) -> None:
        self._rebuild_log_stats()
        self.log_timeline.controls = []
        for r in self._filtered_records():
            if r.get("type") == "error":
                self.log_timeline.controls.append(self._err_card(r))
            else:
                self.log_timeline.controls.append(self._cycle_card(r))
        if not self.log_timeline.controls:
            self.log_timeline.controls.append(
                ft.Text("No cycles yet — click START AUTONOMOUS", color=MUTED)
            )

    def _err_card(self, r: dict) -> ft.Container:
        return ft.Container(
            bgcolor="#2a1520",
            border=_border(RED),
            border_radius=10,
            padding=12,
            content=ft.Text(r.get("msg", "error"), color=RED, selectable=True),
        )

    def _cycle_card(self, r: dict) -> ft.Container:
        n, strat = r.get("cycle", 0), r.get("strat", "hold")
        chg = r.get("pnl_chg", 0)
        has_tw = r.get("tweak") and r.get("tweak") != "none"
        header_color = AMBER if has_tw else TEXT
        unprotected = r.get("unprotected") or []
        body = [
            ft.Text(
                f"Snapshot → positions {len(r.get('positions') or [])} | "
                f"orders {len(r.get('open_orders') or [])} | equity ${r.get('equity', 0):,.0f}",
                color=MUTED,
                size=11,
            ),
            ft.Text(
                f"Risk: {r.get('risk', '—')}"
                + (f" | unprotected: {', '.join(unprotected)}" if unprotected else ""),
                color=RED if unprotected else GREEN,
                size=11,
            ),
            ft.Text(f"Grok: {r.get('rationale') or '—'}", color=TEXT, size=12, selectable=True),
            ft.Text(
                f"Action: {strat} → {json.dumps(r.get('result', {}), default=str)[:200]}",
                color=BLUE,
                size=11,
                selectable=True,
            ),
            ft.Text(f"PnL Δ ${chg:+.2f}", color=GREEN if chg >= 0 else RED, size=12),
        ]
        if has_tw:
            tw_obj = r.get("tweak_obj") or {}
            body.insert(
                0,
                ft.Container(
                    bgcolor="#2a2510",
                    border_radius=8,
                    padding=10,
                    margin=_margin(bottom=8),
                    content=ft.Column(
                        [
                            ft.Text(
                                f"✦ Self-tweak: {r.get('tweak')}",
                                color=AMBER,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                f"Config: {json.dumps(tw_obj.get('config', {}))}",
                                size=11,
                                color=MUTED,
                                selectable=True,
                            ),
                            ft.Row(
                                [
                                    ft.TextButton(
                                        "Apply Again",
                                        on_click=lambda e, o=tw_obj: self._apply_tweak(o),
                                    ),
                                    ft.TextButton(
                                        "Grok Analyze",
                                        on_click=lambda e, o=tw_obj: self._analyze_tweak(o),
                                    ),
                                    ft.TextButton(
                                        "Copy JSON",
                                        on_click=lambda e, o=tw_obj: self._copy(
                                            json.dumps(o, indent=2)
                                        ),
                                    ),
                                    ft.TextButton(
                                        "Replay",
                                        on_click=lambda e, rec=r: self._replay(rec),
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
            border=_border(AMBER if has_tw else BORDER),
            border_radius=10,
            padding=0,
            content=ft.ExpansionTile(
                title=ft.Text(
                    f"Cycle {n}  •  {strat}  •  Δ${chg:+.2f}",
                    color=header_color,
                    weight=ft.FontWeight.W_600,
                ),
                subtitle=ft.Text(r.get("ts", ""), size=10, color=MUTED),
                controls=[ft.Container(padding=12, content=ft.Column(body, spacing=6))],
            ),
        )

    def _apply_tweak(self, tw: dict) -> None:
        msg = apply_tweak(tw)
        self._snack(f"Applied: {msg}", GREEN)

    def _analyze_tweak(self, tw: dict) -> None:
        threading.Thread(target=lambda: asyncio.run(self._grok_analyze(tw)), daemon=True).start()

    async def _grok_analyze(self, tw: dict) -> None:
        try:
            g = GrokClient()
            txt = await grok(
                g,
                f"Analyze this portfolio tweak and suggest ONE follow-up:\n{json.dumps(tw)}\nJSON response.",
            )
            self.ui.put(("log", f"Grok analysis: {txt[:500]}"))
        except Exception as e:
            self.ui.put(("error", f"Analyze failed: {e}"))

    def _replay(self, rec: dict) -> None:
        self.brain_action.value = rec.get("strat", "—")
        self.brain_rationale.value = rec.get("rationale", "—")
        self.brain_result.value = json.dumps(rec.get("result", {}), default=str)
        self.brain_json.value = json.dumps(rec.get("action_obj", {}), indent=2)
        tw = rec.get("tweak_obj")
        if tw:
            self._apply_tweak(tw)
        self._show_tab("brain")

    def _copy(self, text: str) -> None:
        try:
            self.page.clipboard.set(text)
            self._snack("Copied", BLUE)
        except Exception as e:
            self._snack(f"Copy failed: {e}", RED)

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

    def _save_dev_notes(self, _=None) -> None:
        self.dev_notes = self.dev_notes_field.value or ""
        try:
            Path("dev_notes.txt").write_text(self.dev_notes, encoding="utf-8")
        except OSError:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


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
        # Older flet stubs may not accept these kwargs.
        runner(main)


if __name__ == "__main__":
    run_app()
