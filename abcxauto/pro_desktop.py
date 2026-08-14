"""ABCXAUTO Pro — one-screen developer console over ProEngine."""

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
from abcxauto.pro_engine import ProEngine
from abcxauto.reality_pulse import build_reality_pulse, pulse_clock_view

logger = logging.getLogger(__name__)

BG = "#000000"
SURFACE = "#16181c"
BORDER = "#2f3336"
TEXT = "#e7e9ea"
MUTED = "#71767b"
GREEN = "#00ba7c"
RED = "#f4212e"
BLUE = "#1d9bf0"
AMBER = "#ffd400"
WHITE = "#ffffff"

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
TITLE = "ABCXAUTO Pro"
PRO_TITLE = TITLE


class ProTerminal:
    def __init__(self, page: ft.Page):
        self.page = page
        self.engine = ProEngine()
        self._think_sync_key = ""
        self._think_need_scroll = False
        self._build_refs()
        self._sync_widgets()

    def _build_refs(self) -> None:
        self.lbl_title = ft.Text("ABCXAUTO", size=18, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_model = ft.Text("Grok —", size=12, color=MUTED)
        self.lbl_status = ft.Text("Safe", size=13, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_mode = self.lbl_status
        self.lbl_clock = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=TEXT)
        self.lbl_session_badge = ft.Text("—", size=12, weight=ft.FontWeight.W_600, color=AMBER)
        self.dot_conn = ft.Container(width=8, height=8, border_radius=4, bgcolor=RED)
        self.dot_xai = ft.Container(width=8, height=8, border_radius=4, bgcolor=RED)
        self.dot_mda = ft.Container(width=8, height=8, border_radius=4, bgcolor=RED)
        self.lbl_ibkr_status = ft.Text("IBKR down", size=12, color=MUTED)
        self.lbl_xai_status = ft.Text("xAI", size=12, color=MUTED)
        self.lbl_mda_status = ft.Text("MDA", size=12, color=MUTED)
        self.lbl_link = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_banner = ft.Text("", size=12, color=AMBER, selectable=True, visible=False)
        self.lbl_tools = ft.Text("Tools: —", size=12, color=MUTED, selectable=True)
        self.lbl_playbook = ft.Text("Playbook: —", size=12, color=MUTED, selectable=True)
        self.lbl_score = ft.Text("Score: —", size=12, color=MUTED, selectable=True)
        self._score_last = 0.0
        self.lbl_account_name = ft.Text("IBKR", size=12, weight=ft.FontWeight.W_600, color=TEXT)
        self.lbl_account_id = ft.Text("Not connected", size=11, color=MUTED)
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
        self.tf_live_confirm = ft.TextField(
            label="Type live confirm phrase",
            password=True,
            can_reveal_password=True,
            dense=True,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            width=320,
        )
        self.lbl_cycles = ft.Text("0", size=20, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_equity = ft.Text("$0", size=20, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_pnl = ft.Text("$+0.00", size=16, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_book_netliq = self.lbl_equity
        self.lbl_book_pnl = self.lbl_pnl
        self.lbl_unprotected = ft.Text("0", size=20, weight=ft.FontWeight.BOLD, color=GREEN)
        self.lbl_book_unprotected = self.lbl_unprotected
        self.lbl_halt = ft.Text("clear", size=16, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_book_halt = self.lbl_halt
        self.lbl_risk = ft.Text("—", size=13, color=MUTED, selectable=True)
        self.lbl_pace = ft.Text("Pace: —", size=12, color=MUTED, selectable=True)
        self.lbl_dash_pace = self.lbl_pace
        self.lbl_last_send = ft.Text("Last send: —", size=13, color=MUTED, selectable=True)
        self.lbl_agent_judgment = self.lbl_last_send
        self.lbl_result = ft.Text("Result: —", size=13, color=MUTED, selectable=True)
        self.brain_action = self.lbl_result
        self.lbl_why = ft.Text("Why: —", size=12, color=MUTED, selectable=True)
        self.brain_rationale = self.lbl_why
        self.lbl_focus = ft.Text("Focus: —", size=12, color=MUTED, selectable=True)
        self.lbl_agent_read = self.lbl_focus
        self.lbl_stream_status = ft.Text("Grok stream", size=12, color=MUTED)
        self.think_live = ft.Text(
            "Grok stream: waiting for tools...",
            size=13,
            color=MUTED,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        self.btn_copy_stream = self._btn("Copy stream", outlined=True, on_click=self._copy_stream)
        self.think_scroll = ft.Column(
            [self.think_live],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=0,
        )
        self.lbl_positions = ft.Text("No open positions", size=12, color=MUTED, selectable=True)
        self.lbl_pos_summary = self.lbl_positions
        self.lbl_working_orders = ft.Text(
            "No working orders", size=12, color=MUTED, selectable=True
        )
        self.lbl_recent_fills = ft.Text(
            "No fills this session", size=12, color=MUTED, selectable=True
        )
        self.lbl_activity = ft.Text(
            "Connect IBKR, then Start.",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.lbl_cycle_log = self.lbl_activity
        self.lbl_risk_halt = self.lbl_halt
        self.lbl_risk_status = ft.Text("", size=12, color=MUTED, selectable=True)
        self.btn_connect = self._btn("Connect IBKR", outlined=True, on_click=self._toggle_connect)
        self.btn_run = self._btn("Start", filled=True, on_click=self._toggle_run)
        self.btn_halt = self._btn("Halt", outlined=True, on_click=self._toggle_halt)
        self.btn_refresh = self._btn("Refresh book", outlined=True, on_click=self._refresh_book)

    def _btn(self, text: str, *, on_click, filled: bool = False, outlined: bool = False) -> ft.Button:
        style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            side=ft.BorderSide(1, BORDER) if outlined or not filled else None,
        )
        return ft.Button(
            text,
            bgcolor=WHITE if filled else BG,
            color="#0f1419" if filled else TEXT,
            style=style,
            on_click=on_click,
        )

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
            p.window.min_width = 960
            p.window.min_height = 640
        except Exception:
            pass
        self.lbl_model.value = f"Grok {getattr(cfg, 'model', '—')}"
        try:
            p.controls.clear()
        except Exception:
            pass
        p.add(self._shell())
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
                    {"title": p.title, "ui_built": True, "engine": "ProEngine"},
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _shell(self) -> ft.Control:
        return ft.Column(
            [
                self._top_bar(),
                ft.Container(height=1, bgcolor=BORDER),
                ft.Row(
                    [
                        self._stream_pane(),
                        ft.Container(width=1, bgcolor=BORDER),
                        self._book_pane(),
                    ],
                    expand=True,
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
            expand=True,
            spacing=0,
        )

    def _top_bar(self) -> ft.Container:
        return ft.Container(
            bgcolor=BG,
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            content=ft.Row(
                [
                    self.lbl_title,
                    self.lbl_model,
                    self.btn_account_mode,
                    ft.Container(width=12),
                    self.dot_conn,
                    self.lbl_ibkr_status,
                    self.dot_xai,
                    self.lbl_xai_status,
                    self.dot_mda,
                    self.lbl_mda_status,
                    ft.Container(width=8),
                    self.lbl_session_badge,
                    self.lbl_clock,
                    self.lbl_link,
                    ft.Container(expand=True),
                    self.btn_connect,
                    self.btn_run,
                    self.btn_halt,
                    self.btn_refresh,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _stream_pane(self) -> ft.Container:
        return ft.Container(
            expand=True,
            bgcolor="#0a0a0a",
            padding=12,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Grok stream", size=14, weight=ft.FontWeight.W_600, color=TEXT),
                            ft.Container(expand=True),
                            self.lbl_stream_status,
                            self.lbl_status,
                            self.btn_copy_stream,
                        ],
                        spacing=10,
                    ),
                    ft.Container(content=self.think_scroll, expand=True),
                ],
                expand=True,
                spacing=8,
            ),
        )

    def _stat(self, label: str, value: ft.Control) -> ft.Container:
        return ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            padding=10,
            content=ft.Column(
                [ft.Text(label, size=11, color=MUTED), value],
                spacing=2,
                tight=True,
            ),
        )

    def _book_pane(self) -> ft.Container:
        return ft.Container(
            width=400,
            bgcolor=BG,
            padding=12,
            content=ft.Column(
                [
                    ft.Text("Book", size=14, weight=ft.FontWeight.W_600, color=TEXT),
                    self.lbl_banner,
                    ft.Row(
                        [
                            self._stat("Cycles", self.lbl_cycles),
                            self._stat("NetLiq", self.lbl_equity),
                            self._stat("Day PnL", self.lbl_pnl),
                        ],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            self._stat("Unprotected", self.lbl_unprotected),
                            self._stat("Halt", self.lbl_halt),
                        ],
                        spacing=8,
                    ),
                    self.lbl_account_name,
                    self.lbl_account_id,
                    self.lbl_risk,
                    self.lbl_pace,
                    self.lbl_score,
                    self.lbl_tools,
                    self.lbl_playbook,
                    ft.Container(height=1, bgcolor=BORDER),
                    self.lbl_last_send,
                    self.lbl_result,
                    self.lbl_why,
                    self.lbl_focus,
                    ft.Container(height=1, bgcolor=BORDER),
                    ft.Text("Positions", size=12, weight=ft.FontWeight.W_600, color=TEXT),
                    self.lbl_positions,
                    ft.Text("Working orders", size=12, weight=ft.FontWeight.W_600, color=TEXT),
                    self.lbl_working_orders,
                    ft.Text("Session fills", size=12, weight=ft.FontWeight.W_600, color=TEXT),
                    self.lbl_recent_fills,
                    ft.Container(height=1, bgcolor=BORDER),
                    ft.Text("Activity", size=12, weight=ft.FontWeight.W_600, color=TEXT),
                    ft.Container(content=self.lbl_activity, expand=True),
                    self.lbl_risk_status,
                ],
                spacing=6,
                expand=True,
                scroll=ft.ScrollMode.AUTO,
            ),
        )

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
        if danger:
            btn.bgcolor = BG
            btn.color = RED
            btn.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                side=ft.BorderSide(1, RED),
            )
            return
        btn.bgcolor = WHITE if filled else BG
        btn.color = "#0f1419" if filled else TEXT
        btn.style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
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
            self._toast(err or "Starting agent…", color=RED if err else BLUE)
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
        self._safe_update()

    def _refresh_score_line(self) -> None:
        try:
            from abcxauto.scorecard import compute_scorecard

            sc = compute_scorecard(equity=self.engine.state.equity)
        except Exception:
            self.lbl_score.value = "Score: —"
            self.lbl_score.color = MUTED
            return
        def _bit(tag: str, ret: Any, edge: Any, beat: Any) -> str:
            ret_s = f"{ret:+.2f}%" if ret is not None else "—"
            edge_s = f"{edge:+.2f}" if edge is not None else "—"
            if beat is True:
                mark = "BEAT"
            elif beat is False:
                mark = "behind"
            else:
                mark = "…"
            return f"{tag} {ret_s} e{edge_s} {mark}"

        focus = sc.get("fastest_beating") or sc.get("best_pace")
        win = (sc.get("windows") or {}).get(focus) if focus else None
        all_bit = _bit(
            "all",
            sc.get("book_return_pct"),
            sc.get("edge_usd"),
            sc.get("beating_model"),
        )
        if (
            isinstance(win, dict)
            and focus
            and focus != "inception"
            and win.get("book_return_pct") is not None
        ):
            short_bit = _bit(
                str(focus),
                win.get("book_return_pct"),
                win.get("edge_usd"),
                win.get("beating_model"),
            )
            self.lbl_score.value = f"Score: {short_bit}  ·  {all_bit}"
            beat = win.get("beating_model")
        else:
            self.lbl_score.value = f"Score: {all_bit}"
            beat = sc.get("beating_model")
        if beat is True or sc.get("beating_model") is True:
            self.lbl_score.color = GREEN
        elif beat is False or sc.get("beating_model") is False:
            self.lbl_score.color = AMBER
        else:
            self.lbl_score.color = MUTED

    def _copy_stream(self, _=None) -> None:
        text = str(getattr(self.engine.state, "think_live", "") or self.think_live.value or "")
        try:
            self.page.set_clipboard(text[-8000:])
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

    def _start(self, _=None) -> None:
        err = self.engine.start()
        if err:
            self._toast(err, color=RED)
        self._sync_widgets()
        self._safe_update()

    def _stop(self, _=None) -> None:
        self.engine.stop_engine()
        self._sync_widgets()
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
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _after_mode_change(self) -> None:
        self._sync_ibkr_account_label()
        self._toast(f"Mode → {self.lbl_account_mode.value}", color=BLUE)
        self._safe_update()

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

    def _refresh_run_btn(self) -> None:
        s = self.engine.state
        running = bool(s.running) and getattr(s, "autonomous", False) and not getattr(s, "paused", False)
        if running:
            self._set_btn_text(self.btn_run, "Stop", filled=False)
        else:
            self._set_btn_text(self.btn_run, "Start", filled=True)

    def _refresh_connect_btn(self) -> None:
        s = self.engine.state
        linked = bool(s.connected) or (
            self.engine.worker is not None and self.engine.worker.is_alive()
        )
        if linked:
            self._set_btn_text(self.btn_connect, "Disconnect IBKR", danger=True)
        else:
            self._set_btn_text(self.btn_connect, "Connect IBKR", outlined=True)

    def _refresh_halt_btn(self) -> None:
        halted = bool(getattr(self.engine.state, "halted", False))
        try:
            from abcxauto.risk_gates import get_risk_gate

            halted = halted or bool(get_risk_gate().is_halted)
        except Exception:
            pass
        if halted:
            self._set_btn_text(self.btn_halt, "Resume", danger=True)
            self.lbl_halt.value = "HALTED"
            self.lbl_halt.color = RED
        else:
            self._set_btn_text(self.btn_halt, "Halt", outlined=True)
            self.lbl_halt.value = "clear"
            self.lbl_halt.color = GREEN

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
            self.lbl_ibkr_status.value = "IBKR connecting"
            self.lbl_ibkr_status.color = AMBER
        else:
            self.dot_conn.bgcolor = GREEN if ibkr_ok else RED
            self.lbl_ibkr_status.value = f"IBKR {mode}" if ibkr_ok else "IBKR down"
            self.lbl_ibkr_status.color = GREEN if ibkr_ok else MUTED
        self.dot_xai.bgcolor = GREEN if xai_ok else RED
        self.lbl_xai_status.value = "xAI" if xai_ok else "xAI key"
        self.lbl_xai_status.color = GREEN if xai_ok else MUTED
        self.dot_mda.bgcolor = GREEN if mda_ok else RED
        self.lbl_mda_status.value = "MDA" if mda_ok else "MDA off"
        self.lbl_mda_status.color = GREEN if mda_ok else MUTED
        cfg = get_config()
        host = st.get("ibkr_host") or getattr(cfg, "ibkr_host", "") or "127.0.0.1"
        port = st.get("ibkr_port") or getattr(cfg, "ibkr_port", 0) or 0
        cid = st.get("ibkr_client_id") or getattr(cfg, "ibkr_client_id", 0) or 0
        self.lbl_link.value = f"{host}:{port} cid={cid}"
        err = getattr(s, "last_error", None)
        if err:
            s.last_error = None
            self._toast(str(err), color=RED)

    def _sync_think_stream(self) -> None:
        live = str(getattr(self.engine.state, "think_live", "") or "")
        status = str(getattr(self.engine.state, "status", "") or "").strip()
        n = len(live)
        self.lbl_stream_status.value = f"{n:,} chars" + (f" · {status}" if status else "")
        key = live[-24000:]
        if key == getattr(self, "_think_sync_key", None):
            return
        self._think_sync_key = key
        body = key.strip()
        if not body:
            self.think_live.value = "Grok stream: waiting for tools..."
            self.think_live.color = MUTED
        else:
            self.think_live.value = body[-1800:]
            self.think_live.color = TEXT
        self._think_need_scroll = True

    def _sync_widgets(self) -> None:
        s = self.engine.state
        self._sync_ibkr_account_label()
        self.lbl_cycles.value = str(s.cycles)
        self.lbl_equity.value = f"${s.equity:,.0f}"
        self.lbl_pnl.value = f"${s.pnl:+.2f}"
        self.lbl_pnl.color = GREEN if s.pnl >= 0 else RED
        unprot = int(getattr(s, "unprotected_count", 0) or 0)
        self.lbl_unprotected.value = str(unprot)
        self.lbl_unprotected.color = RED if unprot else GREEN
        self.lbl_risk.value = f"Risk: {s.risk}" if s.risk else "Risk: —"
        self.lbl_status.value = s.status
        running = bool(s.running) and getattr(s, "autonomous", False)
        self.lbl_status.color = GREEN if running else (AMBER if getattr(s, "paused", False) else MUTED)
        self._refresh_run_btn()
        self._refresh_connect_btn()
        self._refresh_halt_btn()
        self._refresh_service_status()
        self._sync_think_stream()
        strat = str(getattr(s, "brain_strat", "") or "").strip()
        stage_err = str(getattr(s, "stage_error", "") or "").strip()
        if stage_err:
            self.lbl_last_send.value = f"Block: {stage_err[:240]}"
            self.lbl_last_send.color = AMBER
        elif strat and strat not in ("—",):
            self.lbl_last_send.value = f"Last send: {strat}"
            self.lbl_last_send.color = TEXT
        else:
            self.lbl_last_send.value = "Last send: —"
            self.lbl_last_send.color = MUTED
        result = s.last_result or {}
        status = self._format_result_status(result)
        self.lbl_result.value = f"Result: {status}"
        blocked = status.lower().startswith(("blocked", "rejected", "fail", "error"))
        self.lbl_result.color = RED if blocked else TEXT
        rationale = str(s.brain_rationale or "").strip()
        self.lbl_why.value = f"Why: {rationale[:240]}" if rationale and rationale != "—" else "Why: —"
        self.lbl_why.color = TEXT if rationale and rationale != "—" else MUTED
        market_read = str(getattr(s, "market_read", "") or "").strip()
        self.lbl_focus.value = f"Focus: {market_read[:220]}" if market_read else "Focus: —"
        self.lbl_focus.color = TEXT if market_read else MUTED
        pace = getattr(s, "pace", None) or {}
        if isinstance(pace, dict) and pace:
            sleep_s = pace.get("sleep_s") or pace.get("wait_s")
            reason = pace.get("reason") or pace.get("tier") or ""
            self.lbl_pace.value = f"Pace: {sleep_s}s {reason}".strip()
        else:
            self.lbl_pace.value = "Pace: —"
        trace = list(getattr(s, "tool_trace", None) or [])
        self.lbl_tools.value = f"Tools: {' '.join(trace[-12:])}" if trace else "Tools: —"
        self.lbl_tools.color = TEXT if trace else MUTED
        skip = str(getattr(s, "skip_reason", "") or getattr(s, "stage_error", "") or "")
        if getattr(s, "book_unreliable", False) and "unreliable" not in skip:
            skip = skip or "book_unreliable"
        if skip:
            self.lbl_banner.value = skip
            self.lbl_banner.visible = True
            self.lbl_banner.color = RED if "error" in skip.lower() else AMBER
        elif blocked:
            self.lbl_banner.value = f"{strat}: {status}" if strat else status
            self.lbl_banner.visible = True
            self.lbl_banner.color = RED
        else:
            self.lbl_banner.value = ""
            self.lbl_banner.visible = False
        now = time.monotonic()
        try:
            eq_k = round(float(getattr(s, "equity", 0) or 0), 2)
        except (TypeError, ValueError):
            eq_k = 0.0
        if (
            now - float(getattr(self, "_score_last", 0) or 0) >= 3.0
            or eq_k != getattr(self, "_score_eq", None)
        ):
            self._score_last = now
            self._score_eq = eq_k
            self._refresh_score_line()
        try:
            from abcxauto.lab_playbook import load_lab, load_live, is_paper

            pb = load_lab() if is_paper() else load_live()
            pb = pb if isinstance(pb, dict) else {}
            inst = str(pb.get("instructions") or "").strip()
            if is_paper():
                tag = "promoted" if pb.get("promoted") else (
                    "ready" if pb.get("ready_to_promote") else "lab"
                )
            else:
                tag = "live" if inst else "no promote"
            self.lbl_playbook.value = (
                f"Playbook [{tag}]: {inst[:140]}" if inst else f"Playbook [{tag}]: none"
            )
            self.lbl_playbook.color = TEXT if inst else MUTED
        except Exception:
            self.lbl_playbook.value = "Playbook: —"
        try:
            unprot_n = int(getattr(s, "unprotected_count", 0) or 0)
            self.page.title = (
                f"ABCXAUTO · c{s.cycles} · {s.status} · unprot={unprot_n}"
            )
        except Exception:
            pass
        self.lbl_positions.value = self._positions_summary(s.positions)
        self.lbl_positions.color = TEXT if s.positions else MUTED
        self.lbl_working_orders.value = self._format_working_orders(s.open_orders or [])
        self.lbl_working_orders.color = TEXT if s.open_orders else MUTED
        fills = getattr(s, "recent_fills", None) or []
        self.lbl_recent_fills.value = self._format_recent_fills(fills)
        self.lbl_recent_fills.color = TEXT if fills else MUTED
        self.lbl_activity.value = self._cycle_log_text(s.records)
        try:
            pulse = s.reality_pulse or {}
            if pulse:
                self._apply_clock(pulse)
        except Exception:
            pass

    @staticmethod
    def _format_result_status(res: object) -> str:
        if not isinstance(res, dict) or not res:
            return "—"
        if res.get("success") is True:
            if res.get("filled") is True:
                ep = res.get("entry_price")
                return f"filled @{ep}" if ep not in (None, "") else "filled"
            return "ok"
        status = str(res.get("status") or "").strip()
        note = str(res.get("note") or res.get("reason_code") or res.get("error") or "").strip()
        if status and note and note.lower() not in status.lower():
            combo = f"{status}: {note}"
            return combo if len(combo) <= 120 else combo[:117] + "…"
        return status or note or ("fail" if res.get("success") is False else "ok")

    def _positions_summary(self, positions: list) -> str:
        if not positions:
            return "No open positions"
        lines = []
        for p in positions[:12]:
            con = p.get("conId") or p.get("con_id") or "?"
            sym = p.get("symbol") or "?"
            sec = str(p.get("secType") or p.get("sec_type") or "STK")
            qty = p.get("quantity", p.get("position", 0))
            pnl = p.get("unrealized_pnl") or p.get("unrealizedPnl") or 0
            try:
                pnl_s = f"{float(pnl):+.2f}"
            except (TypeError, ValueError):
                pnl_s = str(pnl)
            lines.append(f"{con}  {sym} {sec}  qty={qty}  uPnL={pnl_s}")
        return "\n".join(lines)

    def _format_working_orders(self, orders: list) -> str:
        if not orders:
            return "No working orders"
        lines = []
        for o in orders[:12]:
            oid = o.get("order_id") or o.get("orderId") or "?"
            sym = o.get("symbol") or "?"
            sec = str(o.get("sec_type") or o.get("secType") or "STK")
            otype = o.get("order_type") or o.get("orderType") or "?"
            qty = o.get("quantity") or o.get("totalQuantity") or "?"
            stop = o.get("aux_price") or o.get("stop_price") or o.get("auxPrice") or ""
            bit = f" stop={stop}" if stop not in (None, "", 0, 0.0) else ""
            lmt = o.get("lmt_price") or o.get("lmtPrice") or o.get("limit_price") or ""
            if lmt not in (None, "", 0, 0.0) and "lmt" not in bit:
                bit += f" lmt={lmt}"
            leg = ""
            if sec.upper().startswith("OPT"):
                right = o.get("right") or ""
                strike = o.get("strike")
                exp = o.get("expiration") or ""
                leg = f" {right}{strike} {exp}".rstrip()
            lines.append(f"{oid}  {sym} {sec} {otype} x{qty}{leg}{bit}")
        return "\n".join(lines)

    def _format_recent_fills(self, fills: list) -> str:
        if not fills:
            return "No fills this session"
        lines = []
        for f in fills[:8]:
            sym = f.get("symbol") or "?"
            side = f.get("side") or f.get("action") or ""
            qty = f.get("quantity") or f.get("shares") or ""
            px = f.get("price") or f.get("avg_price") or ""
            lines.append(f"{sym} {side} x{qty} @{px}")
        return "\n".join(lines)

    def _cycle_log_text(self, records: list[dict]) -> str:
        if not records:
            return "Connect IBKR, then Start."
        lines: list[str] = []
        for r in reversed(list(records or [])[-20:]):
            kind = str(r.get("type") or "cycle").lower()
            ts = str(r.get("ts") or "")
            if "T" in ts:
                ts = ts.split("T", 1)[-1][:8]
            else:
                ts = ts[-8:]
            if kind in ("connect", "start", "pause", "disconnect", "open_risk"):
                lines.append(f"{ts}  {kind.upper()}  {r.get('msg') or '—'}")
                continue
            strat = r.get("strat") or (r.get("action_obj") or {}).get("strategy") or "—"
            status = self._format_result_status(r.get("result") or {})
            lines.append(f"{ts}  {strat}  {status}")
        return "\n".join(lines) or "Connect IBKR, then Start."

    def _apply_clock(self, pulse: dict) -> None:
        view = pulse_clock_view(pulse)
        self.lbl_clock.value = view.get("clock") or "—"
        status = (view.get("session_status") or "closed").lower()
        self.lbl_session_badge.value = view.get("session") or "—"
        self.lbl_session_badge.color = (
            GREEN if status == "regular"
            else (AMBER if status in ("premarket", "postmarket") else MUTED)
        )

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
                if getattr(self, "_think_need_scroll", False):
                    self._think_need_scroll = False
                    try:
                        await self.think_scroll.scroll_to(offset=-1, duration=0)
                    except Exception:
                        pass
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
