"""Operator click handlers for the Pro cockpit. Same engine seams."""
from __future__ import annotations

import json
import logging

import flet as ft

from abcxauto.broker.connection import LIVE_CONFIRM_PHRASE
from abcxauto.desktop.bind import get_config
from abcxauto.desktop.tokens import (
    AMBER,
    AGENT_FIELD_KEYS,
    BG,
    BLUE,
    BORDER,
    GREEN,
    MUTED,
    RED,
    RISK_FIELD_KEYS,
    SURFACE,
    TEXT,
)
from abcxauto.reality_pulse import build_reality_pulse

logger = logging.getLogger("abcxauto.pro_desktop")


class ActionsMixin:

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


    def _open_flatten_confirm_dialog(self, _=None) -> None:
        """Confirm first — Flatten All is the broker nuclear option."""

        def _cancel(_e=None) -> None:
            dlg.open = False
            self._safe_update()

        def _confirm(_e=None) -> None:
            dlg.open = False
            self._flatten_all()
            self._safe_update()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            title=ft.Text("Flatten All?", color=TEXT),
            content=ft.Text(
                "Cancels working orders and closes lots at the broker. "
                "This is not Halt — Halt only blocks new risk.",
                size=13,
                color=MUTED,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_cancel),
                ft.TextButton(
                    "Flatten All",
                    on_click=_confirm,
                    style=ft.ButtonStyle(color=RED),
                ),
            ],
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()


    def _flatten_all(self) -> None:
        """Marshalled engine.panic() once. Never asyncio.run."""
        self._flatten_waiting = True
        self.engine.panic()
        self._toast("Flattening…", color=AMBER)
        self._sync_widgets()


    @staticmethod
    def _flatten_failed_row(failed: object) -> dict | None:
        """Unprotected leftover first — that is the line Noah needs."""
        rows = [row for row in (failed or []) if isinstance(row, dict)]
        if not rows:
            return None
        rank = {"none": 0, "still_working": 1, "last_stop": 2}
        return min(
            rows,
            key=lambda row: rank.get(str(row.get("protection") or ""), 3),
        )

    @staticmethod
    def _flatten_leftover_bit(row: dict) -> str:
        symbol = str(row.get("symbol") or "").strip() or "lot"
        reason = str(row.get("reason") or "").strip()
        prot = str(row.get("protection") or "").strip()
        if prot == "none":
            bit = f"{symbol} unprotected"
            if reason:
                bit = f"{bit} · {reason}"
            return bit[:96]
        if prot == "still_working":
            return f"{symbol} last-stop still working"
        if prot == "last_stop":
            bit = f"{symbol} last-stop restored"
            if reason:
                bit = f"{bit} · {reason}"
            return bit[:96]
        if reason:
            return f"{symbol} {reason}"[:96]
        return symbol

    @staticmethod
    def _flatten_outcome(result: object) -> tuple[str, str]:
        """Format flatten_all success / remaining / failed. Green only when flat."""
        if not isinstance(result, dict) or not result:
            return "Flatten failed", RED
        if result.get("success") is True:
            return "Flattened — book is flat", GREEN
        err = str(result.get("error") or "").strip()
        status = str(result.get("status") or "").strip()
        if status == "disconnected" or err == "Not connected":
            return f"Flatten failed: {err or 'Not connected'}", RED
        failed = result.get("failed") if isinstance(result.get("failed"), list) else []
        remaining = (
            result.get("remaining") if isinstance(result.get("remaining"), list) else []
        )
        n_left = len(failed) if failed else len(remaining)
        prefix = "Partial flatten" if status == "partial" or n_left else "Flatten failed"
        bits: list[str] = []
        if n_left == 1:
            bits.append("1 still open")
        elif n_left > 1:
            bits.append(f"{n_left} still open")
        row = ActionsMixin._flatten_failed_row(failed)
        if row is None and remaining and isinstance(remaining[0], dict):
            row = remaining[0]
        leftover = ActionsMixin._flatten_leftover_bit(row) if row else ""
        if leftover:
            bits.append(leftover)
        if not bits and err:
            return f"Flatten failed: {err}", RED
        color = AMBER if n_left or status == "partial" else RED
        return prefix + (" — " + " · ".join(bits) if bits else ""), color


    def _maybe_finish_flatten_report(self) -> None:
        if not getattr(self, "_flatten_waiting", False):
            return
        for rec in reversed(list(self.engine.state.records or [])):
            kind = str(rec.get("type") or "")
            msg = rec.get("msg")
            if kind == "panic":
                payload = msg
                if isinstance(msg, str):
                    try:
                        payload = json.loads(msg)
                    except json.JSONDecodeError:
                        payload = {}
                line, color = self._flatten_outcome(payload)
                self._flatten_waiting = False
                self._toast(line, color=color)
                return
            if kind == "error" and "PANIC ERROR" in str(msg or ""):
                self._flatten_waiting = False
                self._toast(str(msg), color=RED)
                return


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

        if key not in RISK_FIELD_KEYS:
            self._note_setting(f"{key} is not a floor knob", color=AMBER)
            self._safe_update()
            return
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
