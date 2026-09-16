"""Engine-to-widget sync for the Pro cockpit. Paint only — same cadence."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import flet as ft

from abcxauto.desktop.bind import get_config, last_card_send_label
from abcxauto.desktop.stream import (
    grok_sub_color,
    grok_sub_state,
    session_cap_idle_line,
    stream_line_kind,
    stream_view_lines,
    think_tail_in_flight,
    think_tail_last_say,
    think_tail_tool_chips,
)
from abcxauto.desktop.tokens import (
    AMBER,
    AGENT_FIELD_KEYS,
    BG,
    BLUE,
    BORDER,
    BRAIN_FIELDS,
    FLOOR_GATES,
    GREEN,
    HOVER,
    LINK_FIELDS,
    MUTED,
    NOTE_COLOR,
    PACING_FIELDS,
    PAGE_REFRESH_S,
    RED,
    RISK_FIELDS,
    SURFACE,
    TAIL_LIVE_S,
    TEXT,
)
from abcxauto.reality_pulse import format_desk_clock, pulse_clock_view
from abcxauto.think_stream import think_session_text

logger = logging.getLogger("abcxauto.pro_desktop")


class SyncMixin:

    def _sync_surface_tabs(self) -> None:
        for key, parts in self.surface_tabs.items():
            active = key == self.tab
            parts["chip"].bgcolor = HOVER if active else None
            parts["chip"].border = ft.Border.all(1, TEXT if active else BORDER)
            parts["text"].color = TEXT if active else MUTED


    def _refresh_score_line(self) -> None:
        if not self.engine.state.equity:
            # Scoring a zero book returns -100% and a nonsense edge. Say nothing instead.
            self.lbl_score.value = "Score: — no live book"
            self.lbl_score.color = MUTED
            self.lbl_session_score.value = "sess —"
            self.lbl_session_score.color = MUTED
            self._sc_last = {}
            self._sync_edge_stat({})
            return
        try:
            from abcxauto.scorecard import compute_scorecard

            sc = compute_scorecard(equity=self.engine.state.equity)
        except Exception:
            self.lbl_score.value = "Score: —"
            self.lbl_score.color = MUTED
            self.lbl_session_score.value = "sess —"
            self.lbl_session_score.color = MUTED
            self._sc_last = {}
            self._sync_edge_stat({})
            return
        self._sc_last = sc if isinstance(sc, dict) else {}
        self._sync_edge_stat(sc)
        self._sync_session_score(sc)

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
        sess = sc.get("session") if isinstance(sc.get("session"), dict) else {}
        sret = sess.get("book_return_pct")
        if sret is None:
            s_nl, spnl = sess.get("startup_nl"), sess.get("book_pnl")
            if isinstance(s_nl, (int, float)) and s_nl > 0 and isinstance(spnl, (int, float)):
                sret = (float(spnl) / float(s_nl)) * 100.0
        sedge = sess.get("edge_usd")
        sbeat = None if sedge is None else (float(sedge) > 0)
        date = str(sess.get("session_date") or "RTH")
        all_bit = _bit(date, sret, sedge, sbeat)
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
            beat = sbeat
        if beat is True or sbeat is True:
            self.lbl_score.color = GREEN
        elif beat is False or sbeat is False:
            self.lbl_score.color = AMBER
        else:
            self.lbl_score.color = MUTED


    def _brief(self) -> dict:
        """Last completed look. Shown only until the live book arrives."""
        now = time.monotonic()
        if now - float(getattr(self, "_brief_last", 0) or 0) >= 5.0 or not self._brief_row:
            self._brief_last = now
            try:
                from abcxauto.think_stream import load_desk_brief

                row = load_desk_brief()
                self._brief_row = row if isinstance(row, dict) else {}
            except Exception:
                self._brief_row = {}
        return self._brief_row


    @staticmethod
    def _brief_age(row: dict) -> str:
        ts = str((row or {}).get("ts") or "")
        if not ts:
            return "last look"
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            return "last look"
        try:
            return f"last look {when.astimezone().strftime('%H:%M')}"
        except (OSError, OverflowError, ValueError):
            return "last look"


    @staticmethod
    def _brief_lot_rows(labels: list | None) -> list[dict]:
        """``IWM 260821C306.0 long 1 -26%`` from the brief → the same row shape as live lots."""
        rows: list[dict] = []
        for raw in labels or []:
            text = str(raw or "").strip()
            if not text:
                continue
            ident, pct = text, None
            head, _, tail = text.rpartition(" ")
            if head and tail.endswith("%"):
                try:
                    pct = float(tail.rstrip("%"))
                    ident = head
                except ValueError:
                    ident, pct = text, None
            rows.append({
                "ident": ident,
                "symbol": ident.split(" ")[0].upper(),
                "sec": "OPT",
                "qty": 0.0,
                "avg": None,
                "mkt": None,
                "mtm_pct": pct,
                "unprotected": False,
            })
        return rows


    def _sync_session_score(self, scorecard: dict) -> None:
        sess = scorecard.get("session") if isinstance(scorecard, dict) else None
        if not isinstance(sess, dict) or not sess:
            self.lbl_session_score.value = "sess —"
            self.lbl_session_score.color = MUTED
            return
        date = str(sess.get("session_date") or "")
        date_bit = f"{date} " if date else ""
        pnl = sess.get("book_pnl")
        cost = sess.get("model_cost_usd")
        edge = sess.get("edge_usd")
        fills = sess.get("fills")
        wins = sess.get("wins")
        comm = sess.get("commissions_usd")
        dd = sess.get("max_dd_usd")
        spy = sess.get("spy_return_pct")
        pnl_s = f"{pnl:+.0f}" if isinstance(pnl, (int, float)) else "—"
        cost_s = f"{cost:.2f}" if isinstance(cost, (int, float)) else "—"
        edge_s = f"{edge:+.0f}" if isinstance(edge, (int, float)) else "—"
        fill_s = f"{wins}/{fills}" if fills not in (None, 0) else f"{fills or 0}"
        comm_s = f"{comm:.2f}" if isinstance(comm, (int, float)) else "—"
        dd_s = f"{dd:.0f}" if isinstance(dd, (int, float)) else "—"
        spy_s = f"{spy:+.2f}%" if isinstance(spy, (int, float)) else "—"
        self.lbl_session_score.value = (
            f"sess {date_bit}ΔNL={pnl_s} model$={cost_s} edge={edge_s} "
            f"comm$={comm_s} maxDD={dd_s} vsSPY={spy_s} fills={fill_s}"
        )
        if isinstance(edge, (int, float)) and edge > 0:
            self.lbl_session_score.color = GREEN
        elif isinstance(edge, (int, float)):
            self.lbl_session_score.color = AMBER
        else:
            self.lbl_session_score.color = MUTED


    def _sync_edge_stat(self, scorecard: dict) -> None:
        sc = scorecard if isinstance(scorecard, dict) else {}
        sess = sc.get("session") if isinstance(sc.get("session"), dict) else {}
        # Hero is this RTH. Inception edge stays on the window row / promote floor.
        edge = sess.get("edge_usd")
        if isinstance(edge, (int, float)):
            beat = edge > 0
            self.lbl_edge.value = f"${edge:+,.0f}"
            self.lbl_edge.color = GREEN if beat else AMBER
        else:
            self.lbl_edge.value = "—"
            self.lbl_edge.color = MUTED
        date = str(sess.get("session_date") or "")
        cost = sess.get("model_cost_usd")
        spy = sess.get("spy_return_pct")
        bits = []
        if date:
            bits.append(f"RTH {date}")
        bits.append(
            f"model ${cost:,.2f}" if isinstance(cost, (int, float)) else "vs model"
        )
        if isinstance(spy, (int, float)):
            bits.append(f"vs SPY {spy:+.2f}%")
        elif sess:
            bits.append("vs SPY —")
        self.lbl_edge_sub.value = " · ".join(bits) if bits else "vs model"


    @staticmethod
    def _lot_view(positions: list | None, unprotected: list | None = None) -> list[dict]:
        """One row per lot: identity, MTM %, protection. Attention first."""
        from abcxauto.world_state import compact_position, lot_ident

        naked = {str(x).strip().upper() for x in (unprotected or []) if str(x).strip()}
        rows: list[dict] = []
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            row = compact_position(p, extra=True)
            try:
                qty = float(row.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if abs(qty) < 1e-9:
                continue
            sec = str(row.get("sec") or "STK").upper()
            sym = str(row.get("symbol") or "").upper()
            ident = lot_ident(p)
            ident_u = ident.upper()
            rows.append({
                "ident": ident,
                "symbol": sym,
                "sec": sec,
                "qty": qty,
                "avg": row.get("avg"),
                "mkt": row.get("mkt"),
                "mtm_pct": row.get("mtm_pct"),
                "unprotected": ident_u in naked or (sec.startswith("STK") and sym in naked),
            })
        rows.sort(
            key=lambda r: (
                not r["unprotected"],
                r["mtm_pct"] if isinstance(r["mtm_pct"], (int, float)) else 0.0,
            )
        )
        return rows


    @staticmethod
    def _lot_mark_pct(row: dict) -> float | None:
        """MTM % from live avg → mkt. Do not use a pre-rounded 0% suffix."""
        avg, mkt = row.get("avg"), row.get("mkt")
        try:
            avg_f = float(avg)
            mkt_f = float(mkt)
        except (TypeError, ValueError):
            mtm = row.get("mtm_pct")
            return float(mtm) if isinstance(mtm, (int, float)) else None
        if abs(avg_f) < 1e-12:
            return None
        try:
            qty_f = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty_f = 0.0
        if qty_f < 0:
            return (avg_f - mkt_f) / abs(avg_f) * 100.0
        return (mkt_f - avg_f) / abs(avg_f) * 100.0


    def _sync_lots(self) -> None:
        s = self.engine.state
        book = getattr(s, "portfolio", None) or {}
        naked = book.get("unprotected_symbols") if isinstance(book, dict) else None
        rows = self._lot_view(s.positions, naked)
        stale = ""
        if not rows and not s.equity:
            brief = self._brief()
            rows = self._brief_lot_rows(brief.get("open_lots"))
            stale = f"{self._brief_age(brief)} — not live" if rows else ""
        key = json.dumps([rows, stale], sort_keys=True, default=str)
        if key == self._lots_key:
            return
        self._lots_key = key
        if not rows:
            self.lbl_positions.value = "No open positions"
            self.col_lots.controls = [self.lbl_positions]
            return
        controls: list[ft.Control] = [self._lot_control(r) for r in rows]
        if stale:
            controls.insert(0, ft.Text(stale, size=11, color=AMBER))
        self.col_lots.controls = controls


    def _sync_orders(self) -> None:
        """One row per working order — role/covers is why it exists."""
        from abcxauto.world_state import compact_working_orders

        s = self.engine.state
        try:
            rows = compact_working_orders(s.open_orders or [], positions=s.positions)
        except Exception:
            rows = []
        key = json.dumps(rows, sort_keys=True, default=str)
        if key == self._orders_key:
            return
        self._orders_key = key
        if not rows:
            self.col_orders.controls = [self.lbl_working_orders]
            return
        controls: list[ft.Control] = []
        for o in rows:
            sec = str(o.get("sec") or "STK").upper()
            name = str(o.get("symbol") or "?")
            if sec.startswith("OPT"):
                bits = [str(o.get("right") or ""), str(o.get("strike") or "")]
                exp = str(o.get("expiration") or "")
                name = f"{name} {''.join(bits)} {exp}".strip()
            else:
                name = f"{name} {sec}"
            px = ""
            if o.get("stop") not in (None, ""):
                px = f"stop {o['stop']}"
            if o.get("lmt") not in (None, ""):
                px = f"{px}  lmt {o['lmt']}".strip()
            role = str(o.get("role") or "")
            covers = str(o.get("covers") or "")
            controls.append(
                self._blotter_row([
                    self._cell(str(o.get("order_id") or "?"), width=46, color=MUTED, mono=True),
                    self._cell(name, expand=True, mono=True),
                    self._cell(
                        f"{o.get('action') or ''} {o.get('type') or ''}".strip(),
                        width=104,
                        color=TEXT,
                    ),
                    self._cell(
                        str(o.get("qty") if o.get("qty") is not None else "?"),
                        width=38,
                        right=True,
                    ),
                    self._cell(px or "—", width=104, color=MUTED, right=True),
                    self._cell(
                        role or "—",
                        width=58,
                        color=GREEN if role == "exit" else MUTED,
                        right=True,
                    ),
                ])
            )
            if covers:
                controls.append(
                    ft.Container(
                        padding=ft.Padding.only(left=14, bottom=2),
                        content=ft.Text(f"covers {covers}", size=11, color=MUTED),
                    )
                )
        self.col_orders.controls = controls


    def _sync_fills(self) -> None:
        s = self.engine.state
        fills = list(getattr(s, "recent_fills", None) or [])[:20]
        key = json.dumps(fills, sort_keys=True, default=str)
        if key == self._fills_key:
            return
        self._fills_key = key
        if not fills:
            self.col_fills.controls = [self.lbl_recent_fills]
            return
        controls: list[ft.Control] = []
        for f in reversed(fills):
            side = str(f.get("side") or f.get("action") or "")
            controls.append(
                self._blotter_row([
                    self._cell(str(f.get("symbol") or "?"), expand=True, mono=True),
                    self._cell(
                        side,
                        width=56,
                        color=GREEN if side.upper().startswith("B") else RED,
                        weight=ft.FontWeight.W_600,
                    ),
                    self._cell(
                        str(f.get("quantity") or f.get("shares") or ""), width=44, right=True
                    ),
                    self._cell(
                        str(f.get("price") or f.get("avg_price") or ""),
                        width=76,
                        right=True,
                        color=MUTED,
                    ),
                ])
            )
        self.col_fills.controls = controls


    def _sync_scan_tape(self) -> None:
        """The screen Grok pulled: IBKR rank order plus whatever got a live last."""
        hits = getattr(self.engine.state, "scan_hits", None) or {}
        rows = [r for r in (hits.get("rows") or []) if isinstance(r, dict)]
        key = json.dumps([hits.get("source"), hits.get("quoted"), rows], sort_keys=True, default=str)
        if key == self._scan_key:
            return
        self._scan_key = key
        if not rows:
            self.lbl_scan_head.value = "No screen this session — Grok runs the scanner."
            self.lbl_scan_head.color = MUTED
            self.col_scan.controls = []
            return
        ranked = bool(hits.get("ranked"))
        code = str(hits.get("scan_code") or hits.get("arena") or "").strip()
        bits = [f"{len(rows)} hits", f"{int(hits.get('quoted') or 0)} quoted"]
        if code:
            bits.append(code)
        bits.append(str(hits.get("source") or "?"))
        bits.append(str(hits.get("rank_meaning") or ("ranked" if ranked else "not ranked")))
        self.lbl_scan_head.value = " · ".join(bits)
        self.lbl_scan_head.color = TEXT
        controls: list[ft.Control] = [
            self._head_row([("#", 30), ("symbol", 78), ("last", 84), ("metric", None)])
        ]
        for i, row in enumerate(rows[:12], start=1):
            rank = row.get("rank") if ranked else None
            last = row.get("last")
            metric = (
                row.get("gap%")
                if row.get("gap%") is not None
                else row.get("distance") or row.get("benchmark") or row.get("projection") or ""
            )
            tags = []
            if row.get("on_book"):
                tags.append("on book")
            if row.get("in_turn"):
                tags.append("quoted this look")
            note = " · ".join(str(x) for x in ([metric] if metric else []) + tags)
            controls.append(
                self._blotter_row([
                    self._cell(
                        str(rank if rank not in (None, "") else i),
                        width=30,
                        color=MUTED,
                        mono=True,
                    ),
                    self._cell(
                        str(row.get("symbol") or "?"),
                        width=78,
                        mono=True,
                        weight=ft.FontWeight.W_600,
                        color=GREEN if row.get("on_book") else TEXT,
                    ),
                    self._cell(
                        f"{last:,.2f}" if isinstance(last, (int, float)) else "—",
                        width=84,
                        right=True,
                        color=TEXT if isinstance(last, (int, float)) else MUTED,
                        mono=True,
                    ),
                    self._cell(note or "—", expand=True, color=MUTED),
                ])
            )
        self.col_scan.controls = controls


    def _sync_lessons_line(self) -> None:
        """Structures the clerk refused recently. One line, newest first."""
        lessons = [x for x in (getattr(self.engine.state, "structure_lessons", None) or [])
                   if isinstance(x, dict)]
        if not lessons:
            self.lbl_lessons.visible = False
            self.lbl_lessons.value = ""
            return
        bits: list[str] = []
        for ev in lessons[:3]:
            head = " ".join(
                str(ev.get(k) or "").strip()
                for k in ("strategy", "symbol")
                if str(ev.get(k) or "").strip()
            )
            why = str(ev.get("reason_code") or ev.get("message") or "").strip()
            bits.append(f"{head} {why}".strip() or "—")
        self.lbl_lessons.value = "Lessons: " + " · ".join(bits)
        self.lbl_lessons.visible = True


    @staticmethod
    def _is_note(rec: dict) -> bool:
        """Lifecycle line from ProEngine._note — the message is the whole row.

        Keyed on shape, not a kind whitelist: RETRY / PARK / UNIVERSE notes were
        silently blanked when the list did not name them.
        """
        return bool(rec.get("msg")) and not rec.get("result") and not rec.get("action_obj")


    def _sync_activity(self) -> None:
        s = self.engine.state
        records = list(s.records or [])[-40:]
        key = str(len(s.records or [])) + json.dumps(
            [r.get("ts") for r in records[-3:]], default=str
        )
        if key == self._activity_key:
            return
        self._activity_key = key
        if not records:
            self.col_activity.controls = [self.lbl_activity]
            return
        controls: list[ft.Control] = []
        for r in reversed(records):
            kind = str(r.get("type") or "cycle").lower()
            ts = str(r.get("ts") or "")
            ts = ts.split("T", 1)[-1][:8] if "T" in ts else ts[-8:]
            if self._is_note(r):
                what = kind.upper()
                result = str(r.get("msg") or "—")
                color = NOTE_COLOR.get(kind, MUTED)
            else:
                what = str(
                    r.get("strat") or (r.get("action_obj") or {}).get("strategy") or "—"
                )
                result = self._format_result_status(r.get("result") or {})
                color = (
                    RED
                    if result.lower().startswith(("blocked", "rejected", "fail", "error"))
                    else TEXT
                )
            controls.append(
                self._blotter_row([
                    self._cell(ts, width=60, color=MUTED, mono=True),
                    self._cell(what, width=116, color=color, weight=ft.FontWeight.W_600),
                    self._cell(result, expand=True, color=MUTED),
                ])
            )
        self.col_activity.controls = controls


    def _open_lot_n(self) -> int:
        """Open book lots on the strip — not leftover max_open_positions."""
        s = self.engine.state
        book = getattr(s, "portfolio", None) or {}
        naked = book.get("unprotected_symbols") if isinstance(book, dict) else None
        n = len(self._lot_view(s.positions, naked))
        if not n and not s.equity:
            n = len(self._brief().get("open_lots") or [])
        return n


    def _sync_tabs(self) -> None:
        s = self.engine.state
        lots_n = self._open_lot_n()
        counts = {
            "lots": lots_n,
            "orders": len(s.open_orders or []),
            "fills": len(getattr(s, "recent_fills", None) or []),
            "log": len(s.records or []),
        }
        panes = getattr(self, "tab_panes", None) or {}
        for key, tab in self.tabs.items():
            on = key == self._tab
            n = int(counts.get(key, 0))
            tab["text"].color = TEXT if on else MUTED
            tab["count"].value = str(n) if n else ""
            tab["count"].color = TEXT if on else MUTED
            tab["chip"].bgcolor = SURFACE if on else BG
            tab["chip"].border = ft.Border.all(1, MUTED if on else BORDER)
            body = self.tab_bodies.get(key)
            if body is not None:
                body.visible = on
            pane = panes.get(key)
            if pane is not None:
                pane.visible = on


    def _sync_mix_line(self) -> None:
        from abcxauto.world_state import concentration, format_mix, structure_mix

        s = self.engine.state
        positions = list(s.positions or [])
        if not positions and not s.equity:
            mix = format_mix(self._brief().get("mix"))
            self.lbl_mix.value = f"Mix: {mix}" if mix else "Mix: flat"
            self.lbl_mix.color = MUTED
            return
        bits = []
        mix = format_mix(structure_mix(positions))
        if mix:
            bits.append(mix)
        conc = concentration(positions)
        if conc.get("names"):
            bits.append(f"{conc['names']} names")
        self.lbl_mix.value = f"Mix: {' · '.join(bits)}" if bits else "Mix: flat"
        self.lbl_mix.color = TEXT if bits else MUTED


    def _sync_path_line(self) -> None:
        if not self.engine.state.equity:
            self.lbl_path.value = "Path: —"
            self.lbl_path.color = MUTED
            return
        try:
            from abcxauto.memory import get_journal
            from abcxauto.path_math import path_from_journal

            facts = path_from_journal(
                get_journal(),
                equity=self.engine.state.equity,
                risk_pct=getattr(get_config(), "max_risk_per_trade_pct", None),
            )
        except Exception:
            facts = {}
        self.lbl_path.value = self._path_line(facts)
        f, kelly = facts.get("f"), facts.get("kelly")
        over = (
            isinstance(f, (int, float))
            and isinstance(kelly, (int, float))
            and kelly > 0
            and f > kelly
        )
        self.lbl_path.color = AMBER if over else (TEXT if facts.get("n") else MUTED)


    @staticmethod
    def _path_line(facts: dict | None) -> str:
        row = facts if isinstance(facts, dict) else {}
        n = int(row.get("n") or 0)
        if n < 4:
            return f"Path: {n} closed fills — thin sample"
        bits = [f"n{n}"]
        if isinstance(row.get("E"), (int, float)):
            bits.append(f"E${row['E']:+,.0f}")
        if isinstance(row.get("p"), (int, float)):
            bits.append(f"win {row['p'] * 100:.0f}%")
        kelly = row.get("kelly")
        edge = isinstance(kelly, (int, float)) and kelly > 0
        if edge:
            bits.append(f"kelly {kelly * 100:.1f}%")
        else:
            bits.append("kelly none — no edge yet")
        if isinstance(row.get("f"), (int, float)):
            bits.append(f"f {row['f'] * 100:.1f}%")
        # Ruin only reads as a number once there is an edge to survive.
        if edge and isinstance(row.get("ruin"), (int, float)):
            bits.append(f"ruin {row['ruin'] * 100:.1f}%")
        if n < 20:
            bits.append("thin")
        return "Path: " + " · ".join(bits)


    def _copy_stream_text(self) -> str:
        """What Copy stream puts on the clipboard: the full day, not the 8kb stub."""
        live = str(getattr(self.engine.state, "think_live", "") or "")
        return think_session_text(live=live) or str(self.think_live.value or "")


    def _pane_stream_text(self) -> str:
        """Visible Grok stream: pane view of today's keep-file, not think_live[-24000:]."""
        bits: list[str] = []
        for node in getattr(self.col_stream, "controls", None) or []:
            val = getattr(node, "value", None)
            if isinstance(val, str) and val:
                bits.append(val)
                continue
            content = getattr(node, "content", None)
            cval = getattr(content, "value", None)
            if isinstance(cval, str) and cval:
                bits.append(cval)
        return "\n".join(bits)


    # ------------------------------------------------------------- notebook

    def _sync_notebook_page(self, *, force: bool = False) -> None:
        """Empty notebook surface. Persist is gone."""
        _ = force
        self.lbl_notebook_head.value = "—"
        self.lbl_notebook_head.color = MUTED
        self.lbl_notebook_meta.value = "—"
        self.lbl_notebook_meta.color = MUTED
        self.lbl_notebook_lots.value = "lots at write: none"
        self.col_notebook_cards.controls = [
            ft.Text("No setup cards yet.", size=12, color=MUTED)
        ]
        self.lbl_notebook_body.value = "(empty)"
        self.notebook_raw_panel.visible = True
        self._sync_notebook_types({})
        self.lbl_nb_playbook.value = self.lbl_playbook.value
        self.lbl_nb_playbook.color = self.lbl_playbook.color
        self.lbl_nb_playbook.tooltip = self.lbl_playbook.tooltip


    def _sync_notebook_types(self, lab: dict) -> None:
        _ = lab
        self.lbl_notebook_types.value = ""
        self.col_notebook_types.controls = []


    # ------------------------------------------------------------ scorecard

    def _sync_scorecard_page(self, *, force: bool = False) -> None:
        """Windows, per-card scores, revision ledger. The strategy tracker.

        Throttled by ``_sync_active_page`` — ``force`` is accepted so the page
        builder and the refresh control can paint immediately.
        """
        _ = force
        self.lbl_sc_netliq.value = self.lbl_equity.value
        self.lbl_sc_netliq.color = self.lbl_equity.color
        sc: dict = {}
        if self.engine.state.equity:
            try:
                from abcxauto.scorecard import compute_scorecard

                sc = compute_scorecard(equity=self.engine.state.equity) or {}
            except Exception:
                sc = {}
        self._sync_edge_stat(sc)
        self._sync_session_score(sc)
        self.lbl_sc_session.value = self.lbl_session_score.value
        self.lbl_sc_session.color = self.lbl_session_score.color
        sess = sc.get("session") if isinstance(sc.get("session"), dict) else {}
        sedge = sess.get("edge_usd")
        if isinstance(sedge, (int, float)) and sedge > 0:
            self.lbl_sc_verdict.value = "BEATING the model bill"
            self.lbl_sc_verdict.color = GREEN
        elif isinstance(sedge, (int, float)):
            self.lbl_sc_verdict.value = "behind the model bill"
            self.lbl_sc_verdict.color = AMBER
        elif sess.get("session_date"):
            self.lbl_sc_verdict.value = f"RTH {sess.get('session_date')} — no session NL yet"
            self.lbl_sc_verdict.color = MUTED
        else:
            self.lbl_sc_verdict.value = "no live book — connect IBKR to score"
            self.lbl_sc_verdict.color = MUTED
        start_nl = sess.get("startup_nl")
        end_nl = sess.get("end_nl")
        cost = sess.get("model_cost_usd")
        comm = sess.get("commissions_usd")
        dd = sess.get("max_dd_usd")
        start_s = f"${start_nl:,.2f}" if isinstance(start_nl, (int, float)) else "—"
        end_s = f"${end_nl:,.2f}" if isinstance(end_nl, (int, float)) else "—"
        cost_s = f"${cost:,.2f}" if isinstance(cost, (int, float)) else "—"
        comm_s = f"${comm:,.2f}" if isinstance(comm, (int, float)) else "—"
        dd_s = f"${dd:,.2f}" if isinstance(dd, (int, float)) else "—"
        date = str(sess.get("session_date") or "")
        date_bit = f"RTH {date} · " if date else ""
        self.lbl_sc_score.value = (
            f"{date_bit}start {start_s} → {end_s} · model {cost_s} · "
            f"comm {comm_s} · maxDD {dd_s}"
        )
        self.lbl_sc_score.color = TEXT if sess else MUTED
        self._sync_sc_windows(sc)
        self._sync_sc_cards(sc)
        self._sync_sc_ledger(sc)
        try:
            from abcxauto.memory import get_journal

            div = get_journal().strategy_diversity(limit=40) or {}
        except Exception:
            div = {}
        if div.get("n_distinct"):
            strats = ", ".join(str(x) for x in (div.get("strategies") or []))[:160]
            self.lbl_sc_strats.value = f"Types used: {div['n_distinct']} — {strats}"
            self.lbl_sc_strats.color = TEXT
        else:
            self.lbl_sc_strats.value = "Types used: none yet"
            self.lbl_sc_strats.color = MUTED


    def _sync_sc_windows(self, sc: dict) -> None:
        windows = sc.get("windows") if isinstance(sc, dict) else None
        windows = windows if isinstance(windows, dict) else {}
        rows: list[ft.Control] = [
            self._head_row([
                ("window", 78),
                ("return", 84),
                ("edge", 90),
                ("vs SPY", 72),
                ("verdict", None),
            ])
        ]
        order = ["15m", "1h", "4h", "inception"]
        seen = [k for k in order if k in windows] + [k for k in windows if k not in order]
        if not seen:
            self.col_sc_windows.controls = [
                ft.Text("No journal history yet.", size=12, color=MUTED)
            ]
            return
        for label in seen:
            row = windows.get(label) or {}
            ret = row.get("book_return_pct")
            edge = row.get("edge_usd")
            beat = row.get("beating_model")
            cov = str(row.get("coverage") or "")
            spy = row.get("spy_return_pct")
            if beat is True:
                verdict, color = "BEAT", GREEN
            elif beat is False:
                verdict, color = "behind", AMBER
            else:
                verdict, color = cov or "—", MUTED
            rows.append(
                self._blotter_row([
                    self._cell(label, width=78, weight=ft.FontWeight.W_600),
                    self._cell(
                        f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "—",
                        width=84,
                        right=True,
                        color=MUTED,
                    ),
                    self._cell(
                        f"${edge:+,.2f}" if isinstance(edge, (int, float)) else "—",
                        width=90,
                        right=True,
                        color=color,
                    ),
                    self._cell(
                        f"{spy:+.2f}%" if isinstance(spy, (int, float)) else "—",
                        width=72,
                        right=True,
                        color=MUTED,
                    ),
                    self._cell(verdict, expand=True, color=color, weight=ft.FontWeight.W_600),
                ])
            )
        self.col_sc_windows.controls = rows


    def _sync_sc_cards(self, sc: dict | None = None) -> None:
        _ = sc
        self.col_sc_cards.controls = [
            ft.Text(
                "No card-attributed sends yet. A send records the card that called it.",
                size=12,
                color=MUTED,
            )
        ]


    def _sync_sc_ledger(self, sc: dict) -> None:
        _ = sc
        self.col_sc_ledger.controls = [
            ft.Text("No notebook revisions yet.", size=12, color=MUTED)
        ]


    def _risk_settings_lines(self) -> list[str]:
        """Persisted knobs from get_config / risk_settings.json. Display only."""
        from abcxauto.config import get_config, load_risk_settings, resolve_effective_posture

        try:
            load_risk_settings()
        except Exception:
            pass
        cfg = get_config()
        stored = str(getattr(cfg, "risk_posture", "") or "")
        eff = resolve_effective_posture(stored, getattr(cfg, "trading_mode", "paper"))
        post = f"{stored} → {eff}" if stored and eff and stored != eff else (stored or "—")

        def yn(v: object) -> str:
            return "on" if bool(v) else "off"

        def pct(v: object) -> str:
            return f"{v:g}" if isinstance(v, (int, float)) else "—"

        return [
            f"posture {post}",
            f"trade {pct(getattr(cfg, 'max_risk_per_trade_pct', None))}%",
            f"day {pct(getattr(cfg, 'daily_loss_limit_pct', None))}%",
            f"position {pct(getattr(cfg, 'max_position_pct', None))}%",
            f"drawdown {pct(getattr(cfg, 'max_peak_drawdown_pct', None))}%",
            f"option {pct(getattr(cfg, 'max_option_premium_pct', None))}%",
            f"defined-risk {yn(getattr(cfg, 'defined_risk_only', True))}",
            f"cash-only {yn(getattr(cfg, 'cash_only', True))}",
            f"gates {yn(getattr(cfg, 'risk_gates_enabled', True))}",
        ]


    def _sync_risk_settings_view(self) -> None:
        from abcxauto.config import get_config

        cfg = get_config()
        self.lbl_risk_glance.value = "\n".join(self._risk_settings_lines())
        for key, _label, _hint in RISK_FIELDS:
            self._set_field(key, getattr(cfg, key, None))
        for key, _label in FLOOR_GATES:
            sw = self.gates.get(key)
            if sw is not None:
                sw.value = bool(getattr(cfg, key, True))


    def _sync_risk_page(self, *, force: bool = False) -> None:
        from abcxauto.config import get_config, resolve_effective_posture

        self._sync_risk_settings_view()
        cfg = get_config()
        stored = str(getattr(cfg, "risk_posture", "") or "") or "—"
        eff = resolve_effective_posture(stored, getattr(cfg, "trading_mode", "paper"))
        self.lbl_risk_posture.value = (
            f"{stored} → {eff} (live clamp)" if eff and eff != stored else stored
        )
        self.lbl_risk_posture.color = TEXT
        rows: list[ft.Control] = [
            self._field_row(key, label, hint) for key, label, hint in RISK_FIELDS
        ]
        for key, label in FLOOR_GATES:
            hint = (
                "paper may turn off; live forced on"
                if key == "risk_gates_enabled"
                else "floor — operator may re-arm, never disarm"
            )
            rows.append(
                self._field_row(
                    key,
                    label,
                    hint,
                    control=self.gates[key],
                )
            )
        self.col_risk_knobs.controls = rows
        try:
            from abcxauto.risk_gates import sizing_floors_active

            live = (not cfg.is_paper) or str(cfg.trading_mode or "").lower() == "live"
            on = True if live else sizing_floors_active(cfg)
        except Exception:
            live, on = False, False
        self.sw_size_floors.value = on
        self.sw_size_floors.disabled = live
        self.lbl_risk_floors.value = (
            "Floors ON — forced on live." if live and on
            else "Floors ON — % size floors apply." if on
            else "Floors OFF — Grok sizes freely (paper)."
        )
        self.lbl_risk_floors.color = GREEN if on else AMBER
        nl = float(self.engine.state.equity or 0) or None
        day = float(self.engine.state.pnl or 0) if self.engine.state.equity else None
        try:
            from abcxauto.book import clerk_halt_facts

            halt = clerk_halt_facts(nl, day) or {}
        except Exception:
            halt = {}
        if halt.get("clerk_halted"):
            kind = str(halt.get("halt_kind") or "halt")
            reason = str(halt.get("halt_reason") or "")[:120]
            self.lbl_risk_halt_state.value = f"HALTED ({kind}) — {reason}"
            self.lbl_risk_halt_state.color = RED
        else:
            self.lbl_risk_halt_state.value = "Clear — new risk allowed"
            self.lbl_risk_halt_state.color = GREEN
        trips = halt.get("halt_trips_at_usd")
        room = halt.get("ibkr_day_vs_halt")
        limit = halt.get("daily_loss_limit_pct")
        bits = []
        if isinstance(limit, (int, float)):
            bits.append(f"daily limit {limit:g}% NL")
        if isinstance(trips, (int, float)):
            bits.append(f"trips at ${trips:,.2f}")
        if isinstance(room, (int, float)):
            bits.append(f"room ${room:,.2f}")
        self.lbl_risk_halt_math.value = " · ".join(bits) or "connect IBKR for the halt math"
        self.lbl_risk_halt_math.color = MUTED


    def _sync_settings_page(self, *, force: bool = False) -> None:
        _ = force
        from abcxauto.config import (
            AGENT_DISCONNECTED_ONLY_KEYS,
            broker_link_connected,
            risk_settings_path,
        )

        cfg = get_config()
        for key in AGENT_FIELD_KEYS:
            self._set_field(key, getattr(cfg, key, None))
        link_locked = broker_link_connected()
        for key in AGENT_DISCONNECTED_ONLY_KEYS:
            tf = self.fields.get(key)
            if tf is not None:
                tf.disabled = link_locked
        for key in ("monitor_enabled", "monitor_extended_hours"):
            sw = self.gates.get(key)
            if sw is not None:
                sw.value = bool(getattr(cfg, key, False))
        live = self.engine.state.running and getattr(self.engine.state, "autonomous", False)
        sess_bit = ""
        try:
            from abcxauto.session_caps import usage

            used = usage(session=str(getattr(self.engine, "_last_session", "") or ""))
            sess_bit = (
                f" · session {used['looks']}/{used['look_cap']} looks · "
                f"{used['tokens']}/{used['token_cap']} tok"
            )
        except Exception:
            sess_bit = (
                f" · session cap {getattr(cfg, 'session_look_cap', '—')} looks / "
                f"{getattr(cfg, 'session_token_cap', '—')} tok"
            )
        bits: list[str] = []
        for name in ("model_params", "model_params_rth", "model_params_research"):
            blob = getattr(cfg, name, None) or {}
            if blob:
                bits.append(f"{name} {','.join(sorted(blob))}")
        params_bit = (" · " + " · ".join(bits)) if bits else ""
        self.lbl_settings_brain.value = (
            f"{getattr(cfg, 'model', '—')} · temp {getattr(cfg, 'temperature', '—')} · "
            f"{getattr(cfg, 'max_tokens', '—')} tokens/turn"
            + params_bit
            + sess_bit
            + (" · applies on the next look" if live else "")
        )
        paper = bool(getattr(cfg, "is_paper", True))
        self.lbl_settings_mode.value = "Paper" if paper else "Live"
        self.lbl_settings_mode.color = GREEN if paper else RED
        self.lbl_settings_link.value = (
            f"{getattr(cfg, 'ibkr_host', '')}:{getattr(cfg, 'ibkr_port', '')} "
            f"cid={getattr(cfg, 'ibkr_client_id', '')} · "
            f"{'connected' if self.engine.state.connected else 'not connected'}"
        )
        try:
            self.lbl_settings_path.value = str(risk_settings_path())
        except Exception:
            self.lbl_settings_path.value = "risk_settings.json"


    def _sync_floors_chip(self) -> None:
        from abcxauto.risk_gates import sizing_floors_active

        cfg = get_config()
        live = (not cfg.is_paper) or str(cfg.trading_mode or "").lower() == "live"
        on = True if live else sizing_floors_active(cfg)
        self.lbl_floors.value = "Floors on" if on else "Floors off"
        self.lbl_floors.color = GREEN if on else AMBER
        # A refused or failed toggle has to snap the Risk page switch back now,
        # not on the next repaint.
        self.sw_size_floors.value = on
        self.sw_size_floors.disabled = live
        self.btn_floors.border = ft.Border.all(1, GREEN if on else AMBER)
        self.btn_floors.tooltip = (
            "Live: Floors on (forced)"
            if live
            else "Paper: click to toggle % size floors"
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
        self._sync_floors_chip()
        if s.connected and aid:
            self.lbl_account_name.value = aname or f"IBKR {aid}"
            self.lbl_account_id.value = aid
        elif s.connected:
            self.lbl_account_name.value = aname or "IBKR"
            self.lbl_account_id.value = "Account id pending…"
        else:
            self.lbl_account_name.value = "IBKR"
            self.lbl_account_id.value = "Not connected"


    def _refresh_alert(self, unprot: int) -> None:
        halted = bool(getattr(self.engine.state, "halted", False))
        bits: list[str] = []
        if halted:
            bits.append("HALTED — click Resume to send")
        if unprot:
            bits.append(f"{unprot} stock lot(s) need a last-stop")
        if bits:
            self.lbl_alert.value = " · ".join(bits)
            self.lbl_alert.visible = True
            self.lbl_alert.color = RED
        else:
            self.lbl_alert.value = ""
            self.lbl_alert.visible = False


    def _refresh_run_btn(self) -> None:
        s = self.engine.state
        running = bool(s.running) and getattr(s, "autonomous", False) and not getattr(s, "paused", False)
        if running:
            self._set_btn_text(self.btn_run, "Stop", filled=False)
            self.lbl_run_state.value = "Grok on"
            self.lbl_run_state.color = GREEN
            self.lbl_desk.value = "On"
            self.lbl_desk.color = GREEN
            self.lbl_desk_sub.value = ""
        else:
            self._set_btn_text(self.btn_run, "Start", filled=True)
            paused = bool(getattr(s, "paused", False))
            self.lbl_run_state.value = "Grok paused" if paused else "Grok off"
            self.lbl_run_state.color = AMBER if paused else MUTED
            self.lbl_desk.value = "Paused" if paused else "Off"
            self.lbl_desk.color = AMBER if paused else MUTED
            self.lbl_desk_sub.value = ""


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
        body = think_session_text(live=live)
        n = len(body)
        self.lbl_stream_status.value = f"{n:,} chars" + (f" · {status}" if status else "")
        if body == getattr(self, "_think_sync_key", None):
            return
        self._think_sync_key = body
        shown = body.strip()
        if not shown:
            prev = self._prev_stream()
            self.think_live.value = prev or "Grok stream: waiting for tools..."
            self.think_live.color = MUTED
            self.think_live.visible = True
            self.col_stream.controls = []
            self._stream_lines_key = ""
        else:
            # Empty-state label only. The spine is the day; JSON dumps
            # collapse in the view, not on the keep-file.
            self.think_live.value = ""
            self.think_live.visible = False
            self._sync_stream_lines(body)
            if getattr(self, "_stream_follow", True):
                self.think_scroll.auto_scroll = True


    def _sync_stream_lines(self, body: str) -> None:
        """One control per view line so markers read at a glance.

        Chips, banners, and think/say prose paint verbatim. JSON object dumps
        collapse to a short stub — the pane is a view, not the keep-file.
        The operator scrolls the look, not 40kb of [book]/[scan] JSON.
        """
        lines = stream_view_lines(body)
        key = "\n".join(lines)
        if key == self._stream_lines_key:
            return
        self._stream_lines_key = key
        # Only attach the screen to a scan line whose own counts match it. A
        # stale payload next to this look's hits= would be a quiet lie.
        hits = getattr(self.engine.state, "scan_hits", None) or {}
        n_rows = len([r for r in (hits.get("rows") or []) if isinstance(r, dict)])
        want = f"hits={n_rows} quoted={hits.get('quoted')} " if n_rows else ""
        scan_at = -1
        if want:
            for i, raw in enumerate(lines):
                if stream_line_kind(raw) == "scan" and raw.strip().startswith(want):
                    scan_at = i
        controls: list[ft.Control] = []
        mode = "say"
        for i, raw in enumerate(lines):
            kind = stream_line_kind(raw)
            if kind == "blank":
                continue
            if kind == "think":
                mode = "think"
            elif kind == "clerk":
                mode = "clerk"
            elif kind == "say":
                mode = "say"
            elif kind == "banner":
                mode = "clerk" if "CLERK" in raw else "say"
            controls.append(self._stream_line(raw, kind, mode))
            if i == scan_at:
                controls.append(self._scan_inline())
        self.col_stream.controls = controls


    @staticmethod
    def _age_s(sec: float) -> str:
        if sec < 90:
            return f"{sec:.0f}s"
        if sec < 5400:
            return f"{sec / 60:.0f}m"
        return f"{sec / 3600:.1f}h"


    def _think_tail_moved(self, buf: str) -> bool:
        """True when think_live changed since the last paint. First paint is not motion.

        Length alone lies once the 24k cap is full — the last 512 chars still
        move. Hold TAIL_LIVE_S after the last change so a 4s snap gap stays looking.
        """
        text = buf or ""
        n = len(text)
        fp = text[-512:]
        prev_n = self._tail_len
        prev_fp = self._tail_fp
        self._tail_len = n
        self._tail_fp = fp
        now = time.monotonic()
        if prev_n is None:
            return False
        if n > prev_n or (prev_fp is not None and fp != prev_fp):
            self._tail_moved_mono = now
            return True
        age = now - float(self._tail_moved_mono or 0)
        return bool(self._tail_moved_mono) and age < TAIL_LIVE_S


    def _sync_last_line(self) -> None:
        """Last say in the tail, else last real card send. Never Last send: — after a look."""
        s = self.engine.state
        buf = str(getattr(s, "think_live", "") or "")
        say = think_tail_last_say(buf)
        stage_err = str(getattr(s, "stage_error", "") or "").strip()
        strat = str(getattr(s, "brain_strat", "") or "").strip()
        sends_look = int(getattr(s, "sends_last_look", 0) or 0)
        if stage_err:
            self.lbl_last_send.value = f"Block: {stage_err[:240]}"
            self.lbl_last_send.color = AMBER
            return
        if say:
            self.lbl_last_send.value = say[:240]
            self.lbl_last_send.color = TEXT
            return
        card = last_card_send_label()
        if card:
            self.lbl_last_send.value = card[:240]
            self.lbl_last_send.color = TEXT
            return
        if strat and strat not in ("—",):
            self.lbl_last_send.value = f"Last send: {strat}"
            self.lbl_last_send.color = TEXT
            return
        if sends_look:
            self.lbl_last_send.value = f"Last look: {sends_look} send(s)"
            self.lbl_last_send.color = TEXT
            return
        if not s.equity and self._brief().get("strat"):
            brief = self._brief()
            self.lbl_last_send.value = (
                f"Last send: {brief.get('strat')} · {brief.get('sends') or 0} sends "
                f"({self._brief_age(brief)})"
            )
            self.lbl_last_send.color = MUTED
            return
        self.lbl_last_send.value = "—"
        self.lbl_last_send.color = MUTED


    def _sync_health_strip(self) -> None:
        """Silence, burn, link — the three things that make the operator step in.

        Monotonic math and small dict reads only; this paints every tick.
        """
        s, eng = self.engine.state, self.engine
        paused = bool(getattr(s, "paused", False))
        running = (
            bool(s.running)
            and bool(getattr(s, "autonomous", False))
            and not paused
        )
        streak = int(getattr(eng, "_fail_streak", 0) or 0)
        last = float(getattr(eng, "_last_grok_mono", 0.0) or 0.0)
        status = str(getattr(s, "status", "") or "")
        st = status.lower()
        buf = str(getattr(s, "think_live", "") or "")
        parked = bool(getattr(eng, "_think_parked", False) or st == "parked")
        capped = bool(getattr(eng, "_session_capped", False) or st == "idle")
        tail_moved = self._think_tail_moved(buf)
        say = think_tail_last_say(buf)
        tail_live = think_tail_in_flight(buf) or (bool(say) and tail_moved)
        state = grok_sub_state(
            running=running,
            status=status,
            fail_streak=streak,
            parked=parked,
            tail_moved=tail_moved,
            tail_live=tail_live,
            paused=paused,
            session_capped=capped,
        )
        color = grok_sub_color(state)
        looking = state == "looking"
        self.lbl_hs_state.value = state
        self.lbl_hs_state.color = color
        # Grok tile mirrors the strip so "On" alone never hides a think, a wait, or a cap.
        if running:
            self.lbl_desk_sub.value = state
            self.lbl_desk_sub.color = color
        age = (time.monotonic() - last) if last else None
        if age is None:
            self.lbl_hs_age.value = "no look yet"
            self.lbl_hs_age.color = AMBER if running else MUTED
        else:
            self.lbl_hs_age.value = f"last look {self._age_s(age)} ago"
            self.lbl_hs_age.color = (
                RED if running and age > 1800 else (AMBER if running and age > 900 else MUTED)
            )
        if state == "idle" and capped:
            used: dict = {}
            try:
                from abcxauto.session_caps import usage

                used = usage(session=str(getattr(eng, "_last_session", "") or "")) or {}
            except Exception:
                used = {}
            self.lbl_hs_next.value = session_cap_idle_line(used)
            self.lbl_hs_next.color = AMBER
        elif streak and not looking and state != "paused":
            self.lbl_hs_next.value = f"look failed (x{streak})"
            self.lbl_hs_next.color = AMBER
        else:
            self.lbl_hs_next.value = ""
            self.lbl_hs_next.color = MUTED
        sends = int(getattr(s, "sends_last_look", 0) or 0)
        tools = len(think_tail_tool_chips(buf))
        burning = False
        if sends:
            self.lbl_hs_burn.value = f"{sends} ticket(s) this look"
            self.lbl_hs_burn.color = GREEN
        else:
            self.lbl_hs_burn.value = ""
            self.lbl_hs_burn.color = MUTED
        self.lbl_hs_look.value = f"this look: {tools} tool(s) · {sends} send(s)"
        self.lbl_hs_look.color = TEXT if tools else MUTED
        # Only a burn gets a box — the strip is otherwise a plain status bar.
        self.health_box.border = ft.Border.all(1, RED) if burning else None
        pulse = getattr(s, "reality_pulse", None) or {}
        block = pulse.get("session") if isinstance(pulse, dict) else None
        sess = str(block.get("status") or "") if isinstance(block, dict) else str(block or "")
        link = str(self.lbl_ibkr_status.value or "")
        self.lbl_hs_link.value = f"{link} · {sess}" if sess else link
        self.lbl_hs_link.color = GREEN if bool(getattr(s, "connected", False)) else RED
        self._sync_last_line()


    def _prev_stream(self) -> str:
        """An idle pane is wasted — show the last look Grok took."""
        if self._prev_text is None:
            try:
                from abcxauto.think_stream import THINK_PREV_PATH

                raw = (
                    THINK_PREV_PATH.read_text(encoding="utf-8")
                    if THINK_PREV_PATH.is_file()
                    else ""
                )
            except OSError:
                raw = ""
            tail = raw.strip()[-3000:]
            self._prev_text = f"— previous look —\n\n{tail}" if tail else ""
        return self._prev_text


    def _sync_active_page(self, *, force: bool = False) -> None:
        """Only the visible page reads disk, and at most every PAGE_REFRESH_S."""
        now = time.monotonic()
        if not force and now - float(self._page_last or 0) < PAGE_REFRESH_S:
            return
        self._page_last = now
        try:
            if self.tab == "notebook":
                self._sync_notebook_page(force=force)
            elif self.tab == "scorecard":
                self._sync_scorecard_page(force=force)
            elif self.tab == "risk":
                self._sync_risk_page(force=force)
            elif self.tab == "settings":
                self._sync_settings_page(force=force)
        except Exception:
            logger.debug("page sync failed tab=%s", self.tab, exc_info=True)


    def _sync_widgets(self) -> None:
        s = self.engine.state
        self._sync_ibkr_account_label()
        brief = {} if s.equity else self._brief()
        nl = float(s.equity or 0) or float(brief.get("net_liquidation") or 0)
        self.lbl_equity.value = f"${nl:,.2f}" if nl else "—"
        self.lbl_equity.color = TEXT if s.equity else MUTED
        self.lbl_equity_sub.value = "live" if s.equity else self._brief_age(brief) if nl else ""
        if s.equity:
            self.lbl_pnl.value = f"${s.pnl:+.2f}"
            self.lbl_pnl.color = GREEN if s.pnl >= 0 else RED
            self.lbl_pnl_pct.value = f"{s.pnl / s.equity * 100:+.2f}% vs prior close"
        else:
            self.lbl_pnl.value = "—"
            self.lbl_pnl.color = MUTED
            self.lbl_pnl_pct.value = ""
        try:
            from abcxauto.world_state import open_upnl_of

            upnl = open_upnl_of(s.positions)
        except Exception:
            upnl = None
        if isinstance(upnl, (int, float)):
            self.lbl_open_upnl.value = f"${upnl:+,.2f}"
            self.lbl_open_upnl.color = GREEN if upnl >= 0 else RED
            self.lbl_open_upnl_sub.value = "marks now"
        else:
            self.lbl_open_upnl.value = "—"
            self.lbl_open_upnl.color = MUTED
            self.lbl_open_upnl_sub.value = "no open marks"
        unprot = int(getattr(s, "unprotected_count", 0) or 0)
        self.lbl_unprotected.value = str(unprot)
        self.lbl_unprotected.color = RED if unprot else GREEN
        lots = self._open_lot_n()
        self.lbl_lot_count.value = str(lots)
        self.lbl_lot_count.color = TEXT
        self.lbl_risk.value = f"Risk: {s.risk}" if s.risk else "Risk: —"
        self.lbl_status.value = s.status
        running = bool(s.running) and getattr(s, "autonomous", False)
        self.lbl_status.color = GREEN if running else (AMBER if getattr(s, "paused", False) else MUTED)
        self._refresh_run_btn()
        self._refresh_connect_btn()
        self._refresh_halt_btn()
        self._refresh_alert(unprot)
        self._refresh_service_status()
        self._sync_think_stream()
        result = s.last_result or {}
        status = self._format_result_status(result)
        self.lbl_result.value = f"Result: {status}"
        blocked = status.lower().startswith(("blocked", "rejected", "fail", "error"))
        self.lbl_result.color = RED if blocked else TEXT
        rationale = str(s.brain_rationale or "").strip()
        if not s.equity:
            rationale = str(self._brief().get("rationale") or "").strip() or rationale
        self.lbl_why.value = f"Why: {rationale[:240]}" if rationale and rationale != "—" else "Why: —"
        self.lbl_why.color = TEXT if rationale and rationale != "—" else MUTED
        self.lbl_why.tooltip = rationale[:600] or None
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
        self.lbl_dash_tools.value = self.lbl_tools.value
        self.lbl_dash_tools.color = self.lbl_tools.color
        skip = str(getattr(s, "skip_reason", "") or getattr(s, "stage_error", "") or "")
        if getattr(s, "book_unreliable", False) and "unreliable" not in skip:
            skip = skip or "book_unreliable"
        strat = str(getattr(s, "brain_strat", "") or "").strip()
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
        if now - float(self._path_last or 0) >= 20.0 or not self._path_last:
            self._path_last = now
            self._sync_path_line()
        self._sync_mix_line()
        self._sync_lots()
        self._sync_orders()
        self._sync_fills()
        self._sync_activity()
        self._sync_scan_tape()
        self._sync_health_strip()
        self._sync_lessons_line()
        self._sync_tabs()
        self.lbl_playbook.value = "Playbook: —"
        self.lbl_playbook.tooltip = None
        self.lbl_playbook.color = MUTED
        self.page.title = "ABCXAUTO"
        self.lbl_working_orders.value = self._format_working_orders(
            s.open_orders or [], positions=getattr(s, "positions", None)
        )
        self.lbl_working_orders.color = TEXT if s.open_orders else MUTED
        fills = getattr(s, "recent_fills", None) or []
        self.lbl_recent_fills.value = self._format_recent_fills(fills)
        self.lbl_recent_fills.color = TEXT if fills else MUTED
        self.lbl_activity.value = self._cycle_log_text(s.records)
        health = str(getattr(s, "mandate_health", "") or "green")
        label = str(getattr(s, "mandate_health_label", "") or "protected")
        self.lbl_mandate_health.value = f"{health} — {label}"
        self.lbl_mandate_health.color = (
            RED if health == "red" else (AMBER if health == "amber" else GREEN)
        )
        self._sync_active_page()
        try:
            pulse = s.reality_pulse or {}
            if pulse:
                self._apply_clock(pulse)
        except Exception:
            pass
        self._paint_think_news_if_flat()


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


    def _format_working_orders(self, orders: list, positions: list | None = None) -> str:
        from abcxauto.world_state import compact_working_orders

        rows = compact_working_orders(orders, positions=positions)
        if not rows:
            return "No working orders"
        lines = []
        for o in rows:
            oid = o.get("order_id") or "?"
            sym = o.get("symbol") or "?"
            sec = o.get("sec") or "STK"
            otype = o.get("type") or "?"
            qty = o.get("qty") if o.get("qty") is not None else "?"
            action = o.get("action") or ""
            bit = f" stop={o['stop']}" if o.get("stop") not in (None, "") else ""
            if o.get("lmt") not in (None, "") and "lmt" not in bit:
                bit += f" lmt={o['lmt']}"
            leg = ""
            if str(sec).upper().startswith("OPT"):
                right = o.get("right") or ""
                strike = o.get("strike")
                exp = o.get("expiration") or ""
                leg = f" {right}{strike} {exp}".rstrip()
            role = str(o.get("role") or "").strip()
            covers = str(o.get("covers") or "").strip()
            tag = f"  {role} {covers}".rstrip() if role else ""
            act = f"{action} " if action else ""
            lines.append(f"{oid}  {sym} {sec} {act}{otype} x{qty}{leg}{bit}{tag}")
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
            return "Connect IBKR."
        lines: list[str] = []
        for r in reversed(list(records or [])[-20:]):
            kind = str(r.get("type") or "cycle").lower()
            ts = str(r.get("ts") or "")
            if "T" in ts:
                ts = ts.split("T", 1)[-1][:8]
            else:
                ts = ts[-8:]
            if self._is_note(r):
                lines.append(f"{ts}  {kind.upper()}  {r.get('msg') or '—'}")
                continue
            strat = r.get("strat") or (r.get("action_obj") or {}).get("strategy") or "—"
            status = self._format_result_status(r.get("result") or {})
            lines.append(f"{ts}  {strat}  {status}")
        return "\n".join(lines) or "Connect IBKR."


    def _apply_clock(self, pulse: dict) -> None:
        view = pulse_clock_view(pulse)
        self.lbl_clock.value = format_desk_clock() or view.get("clock") or "—"
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
        narrative = (pulse or {}).get("narrative")
        if narrative:
            self.lbl_pulse_narrative.value = str(narrative)
            self.lbl_pulse_narrative.color = TEXT


    # ------------------------------------------------------------ right rail

    @staticmethod
    def _format_return_pct(value) -> tuple[str, str]:
        if value is None:
            return "—", MUTED
        try:
            pct = float(value) * 100.0
        except (TypeError, ValueError):
            return "—", MUTED
        return f"{pct:+.2f}%", GREEN if pct >= 0 else RED


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
        if not force and self._ret_last_fetch and (now - self._ret_last_fetch) < 120.0:
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


    def _open_lots_empty(self) -> bool:
        if self.engine.state.positions:
            return False
        lots = (self.engine.state.world_state or {}).get("open_lots")
        if lots:
            return False
        return True


    def _flat_book_news_names(self) -> list[str]:
        """Last scan/think names when the book is flat. Not a sandbox pad."""
        order: list[str] = []

        def _add(raw: Any) -> None:
            su = str(raw or "").upper().strip()
            if su and su not in order:
                order.append(su)

        s = self.engine.state
        for name in s.scan_fetched or []:
            _add(name)
        hits = s.scan_hits if isinstance(s.scan_hits, dict) else {}
        for row in hits.get("rows") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        for it in s.news_items or []:
            if isinstance(it, dict):
                _add(it.get("symbol"))
        try:
            from abcxauto.think_stream import last_look_for_hunt

            last = last_look_for_hunt()
            last_hits = last.get("scan_hits") if isinstance(last.get("scan_hits"), dict) else {}
            for row in last_hits.get("rows") or []:
                if isinstance(row, dict):
                    _add(row.get("symbol"))
            for name in last.get("scan_fetched") or []:
                _add(name)
        except Exception:
            pass
        return order[:14]


    def _news_rail_universe(self) -> list[dict]:
        """Positions when the book is open; scan/think names when it is flat."""
        pos = [
            p
            for p in (self.engine.state.positions or [])
            if isinstance(p, dict) and str(p.get("symbol") or "").strip()
        ]
        if pos:
            return pos
        return [{"symbol": s} for s in self._flat_book_news_names()]


    def _think_news_items(self) -> list[dict]:
        """Headlines the think already fetched. Never invented."""
        items: list[dict] = []
        seen: set[str] = set()

        def _take(it: Any) -> None:
            if not isinstance(it, dict):
                return
            hl = str(it.get("headline") or "").strip()
            if not hl or hl in seen:
                return
            seen.add(hl)
            items.append(it)

        for it in self.engine.state.news_items or []:
            _take(it)
        hits = self.engine.state.scan_hits if isinstance(self.engine.state.scan_hits, dict) else {}
        top = hits.get("news")
        if isinstance(top, list):
            for it in top:
                _take(it)
        for row in hits.get("rows") or []:
            if not isinstance(row, dict):
                continue
            mda = row.get("mda") if isinstance(row.get("mda"), dict) else {}
            news = mda.get("news") if isinstance(mda, dict) else None
            if isinstance(news, list):
                for it in news:
                    _take(it)
        return items


    def _paint_think_news_if_flat(self) -> None:
        if not self._open_lots_empty():
            return
        if self._news_cache:
            return
        items = self._think_news_items()
        if not items:
            return
        self._news_cache = items
        self._render_news_list(items)


    def _render_news_list(self, items: list[dict], *, fallback: str = "") -> None:
        rows: list[ft.Control] = []
        for it in (items or [])[:10]:
            hl = str(it.get("headline") or "").strip()
            if not hl:
                continue
            sym = str(it.get("symbol") or "").upper()
            src = str(it.get("publisher") or "")
            feed = str(it.get("source") or "")
            if not src and feed not in ("mda", "ibkr", "marketdata"):
                src = feed
            if src.startswith("http"):
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
            rows = [
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=8),
                    content=ft.Text(
                        fallback or "No headlines yet.", size=13, color=MUTED, selectable=True
                    ),
                )
            ]
        self.news_list.controls = rows


    async def _refresh_news(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._news_last_fetch and (now - self._news_last_fetch) < 60.0:
            return
        self._news_last_fetch = now
        think_items = self._think_news_items()
        from abcxauto.news_feed import (
            coalesce_news,
            fetch_agent_news,
            is_real_headline,
            remember_headlines,
        )

        remember_headlines(think_items)
        try:
            unique = await fetch_agent_news(
                self._news_rail_universe(), force=force, per_symbol=5
            )
        except Exception:
            unique = []
        remember_headlines(unique)
        names = [
            str((p or {}).get("symbol") or "").upper().strip()
            for p in self._news_rail_universe()
            if str((p or {}).get("symbol") or "").strip()
        ]
        painted = coalesce_news(unique, names)
        if not any(is_real_headline(it) for it in painted):
            painted = think_items or unique
        self._news_cache = painted
        remember_headlines(painted)
        self._render_news_list(painted)
