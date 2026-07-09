"""Pro engine — START/pause/panic, rocket loop, view state (stdlib only, no Flet)."""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.executor import safe_execute
from abcxauto.llm import GrokClient
from abcxauto.rocket import (
    TWEAKS,
    apply_tweak,
    format_position_inventory,
    grok,
    run_cycle,
    simulate_close_impact,
    validate_action_against_inventory,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


@dataclass
class ViewState:
    cycles: int = 0
    pnl: float = 0.0
    equity: float = 0.0
    pnl_chg: float = 0.0
    equity_hist: list[float] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    open_orders: list[dict] = field(default_factory=list)
    inventory: str = ""
    records: list[dict] = field(default_factory=list)
    tweaks: list[dict] = field(default_factory=list)
    connected: bool = False
    running: bool = False
    paused: bool = False
    status: str = "Safe"
    last_action: dict = field(default_factory=dict)
    last_result: dict = field(default_factory=dict)
    last_impact: dict = field(default_factory=dict)
    reality_pulse: dict = field(default_factory=dict)
    kahneman: dict = field(default_factory=dict)
    kahneman_trace: str = ""
    order_lab: dict = field(default_factory=dict)
    lab_summary: str = ""
    reconfig: dict = field(default_factory=dict)
    simplify: dict = field(default_factory=dict)
    retest: dict = field(default_factory=dict)
    brutal_suite: dict = field(default_factory=dict)
    brutal_summary: str = ""
    lab_pass_rate: float = 0.0
    simplify_count: int = 0
    brain_strat: str = "—"
    brain_rationale: str = "Start autonomous mode to see Grok decisions."
    risk: str = "—"
    close_attempts: int = 0
    close_ok: int = 0
    mismatches: int = 0


class ProEngine:
    """Autonomous rocket loop + thread-safe UI queue. Wired to START via ProTerminal."""

    def __init__(self) -> None:
        self.ui: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self.pause = threading.Event()  # set = paused
        self._gen = 0
        self.worker: threading.Thread | None = None
        self.conn: Any = None
        self.state = ViewState()

    def start(self) -> str | None:
        if not get_config().xai_api_key:
            return "XAI_API_KEY missing"
        if self.worker and self.worker.is_alive():
            # Resume if paused
            if self.pause.is_set():
                self.pause.clear()
                self.state.paused = False
                self.state.running = True
                self.state.status = "Running"
                return None
            return None
        self._gen += 1
        gen = self._gen
        self.stop.clear()
        self.pause.clear()
        self.state.running = True
        self.state.paused = False
        self.state.status = "Running"
        self.worker = threading.Thread(target=lambda: self._worker(gen), daemon=True)
        self.worker.start()
        return None

    def run_startup_suite(self) -> None:
        """Force brutal order suite on startup so the bot never idles."""
        threading.Thread(target=lambda: asyncio.run(self._startup_suite()), daemon=True).start()

    async def _startup_suite(self) -> None:
        from abcxauto.brutal_suite import format_brutal_summary, run_brutal_suite
        from abcxauto.reality_pulse import build_reality_pulse

        try:
            conn = self.conn
            if conn is None:
                try:
                    conn = get_ibkr_connector()
                    if not getattr(conn, "connected", False):
                        await conn.connect()
                except Exception:
                    conn = None
            pulse = build_reality_pulse(
                ibkr_connected=bool(getattr(conn, "connected", False)) if conn else False,
                positions=self.state.positions,
            )
            report = await run_brutal_suite(
                connector=conn, pulse=pulse, positions=self.state.positions, source="startup"
            )
            self.state.brutal_suite = report
            self.state.brutal_summary = format_brutal_summary(report)
            self.state.order_lab = {
                "pass_rate": report.get("pass_rate"),
                "passed": report.get("passed"),
                "failed": report.get("failed"),
                "results": report.get("results") or [],
            }
            self.state.lab_pass_rate = float(report.get("pass_rate") or 0)
            self.state.lab_summary = self.state.brutal_summary
            self.ui.put(
                (
                    "log",
                    f"STARTUP BRUTAL SUITE: {report.get('summary')} idle_prevented=True",
                )
            )
            self.ui.put(("brutal", report))
        except Exception as e:
            self.ui.put(("error", f"STARTUP SUITE ERROR: {e}"))

    def pause_engine(self) -> None:
        """Pause cycles without tearing down the worker / connection."""
        if not self.worker or not self.worker.is_alive():
            return
        self.pause.set()
        self.state.paused = True
        self.state.running = False
        self.state.status = "Paused"

    def stop_engine(self) -> None:
        self.stop.set()
        self.pause.clear()
        self._gen += 1
        self.state.running = False
        self.state.paused = False
        self.state.status = "Safe"
        self.worker = None

    def panic(self) -> None:
        self.stop_engine()
        threading.Thread(target=lambda: asyncio.run(self._do_panic()), daemon=True).start()

    def drain_apply(self) -> ViewState:
        while not self.ui.empty():
            kind, data = self.ui.get_nowait()
            self._apply(kind, data)
        return self.state

    def clear_logs(self) -> None:
        self.state.records.clear()
        self.state.tweaks.clear()
        self.state.close_attempts = 0
        self.state.close_ok = 0
        self.state.mismatches = 0

    def apply_tweak_manual(self, tw: dict) -> str:
        return apply_tweak(tw)

    def validate_last_impact(self) -> dict:
        """Simulate last proposal against live ledger (no broker call)."""
        act = self.state.last_action or {}
        impact = simulate_close_impact(act, self.state.positions)
        self.state.last_impact = impact
        self.ui.put(
            (
                "log",
                f"Validate Order Impact: {impact.get('gate')} "
                f"untouched={impact.get('untouched_conIds')}",
            )
        )
        return impact

    def execute_last_proposal(self) -> None:
        """Manual override: validate then safe_execute last Grok proposal."""
        threading.Thread(
            target=lambda: asyncio.run(self._do_manual_execute()), daemon=True
        ).start()

    async def _do_manual_execute(self) -> None:
        act = dict(self.state.last_action or {})
        if not act:
            self.ui.put(("error", "No proposal to execute"))
            return
        ok, msg = validate_action_against_inventory(act, self.state.positions)
        if not ok:
            self.state.mismatches += 1
            self.ui.put(("error", f"Validate & Execute blocked: {msg}"))
            return
        try:
            conn = self.conn or get_ibkr_connector()
            if not getattr(conn, "connected", False):
                await conn.connect()
            res = await safe_execute(act, conn)
            self.state.last_result = res
            self.ui.put(
                (
                    "log",
                    f"Validate & Execute result: {json.dumps(res, default=str)[:500]}",
                )
            )
        except Exception as e:
            self.ui.put(("error", f"Validate & Execute failed: {e}"))

    async def grok_analyze_tweak(self, tw: dict) -> None:
        try:
            g = GrokClient()
            txt = await grok(
                g,
                f"Analyze this portfolio tweak and suggest ONE follow-up:\n"
                f"{json.dumps(tw)}\nJSON response.",
            )
            self.ui.put(("log", f"Grok analysis: {txt[:500]}"))
        except Exception as e:
            self.ui.put(("error", f"Analyze failed: {e}"))

    def _apply(self, kind: str, data: Any) -> None:
        s = self.state
        if kind == "conn":
            s.connected = bool(data)
        elif kind == "cycle":
            self._on_cycle(data)
        elif kind == "brutal":
            s.brutal_suite = data or {}
            s.brutal_summary = str((data or {}).get("summary") or "")
            s.lab_pass_rate = float((data or {}).get("pass_rate") or 0)
            s.order_lab = {
                "pass_rate": (data or {}).get("pass_rate"),
                "passed": (data or {}).get("passed"),
                "failed": (data or {}).get("failed"),
                "results": (data or {}).get("results") or [],
            }
            s.records.append(
                {
                    "cycle": 0,
                    "type": "brutal_suite",
                    "ts": _now(),
                    "brutal_suite": data,
                    "lab_summary": s.brutal_summary,
                    "msg": s.brutal_summary,
                }
            )
        elif kind == "panic":
            s.records.append(
                {
                    "cycle": 0,
                    "type": "panic",
                    "ts": _now(),
                    "position_results": data.get("position_results", []),
                    "before_ledger": data.get("before_ledger", []),
                    "msg": json.dumps(data, default=str)[:2000],
                    "reasoning_chain": "\n".join(
                        r.get("reasoning", "")
                        for r in data.get("position_results", [])
                        if r.get("reasoning")
                    ),
                    "inventory": format_position_inventory(
                        data.get("before_ledger") or []
                    ),
                }
            )
        elif kind in ("log", "error"):
            s.records.append(
                {"cycle": 0, "type": "error", "msg": str(data), "ts": _now()}
            )

    def _on_cycle(self, d: dict) -> None:
        s = self.state
        s.cycles = d["cycle"]
        s.pnl = d["pnl"]
        s.equity = d["equity"]
        s.pnl_chg = d.get("pnl_chg", 0)
        s.equity_hist.append(d["equity"])
        if len(s.equity_hist) > 40:
            s.equity_hist = s.equity_hist[-40:]
        s.risk = d.get("risk", "—")
        s.last_action = d.get("action_obj") or {}
        s.last_result = d.get("result") or {}
        s.last_impact = d.get("impact") or {}
        s.reality_pulse = d.get("reality_pulse") or {}
        s.kahneman = d.get("kahneman") or {}
        s.kahneman_trace = d.get("kahneman_trace") or ""
        s.order_lab = d.get("order_lab") or {}
        s.lab_summary = d.get("lab_summary") or ""
        s.reconfig = d.get("reconfig") or {}
        s.simplify = d.get("simplify") or {}
        s.retest = d.get("retest") or {}
        s.brutal_suite = d.get("brutal_suite") or s.brutal_suite
        s.brutal_summary = str(
            (d.get("lab_summary") or (s.brutal_suite or {}).get("summary") or "")
        )
        s.lab_pass_rate = float((s.order_lab or {}).get("pass_rate") or 0)
        s.simplify_count = int((s.simplify or {}).get("simplification_count") or 0)
        s.brain_strat = d.get("strat", "hold")
        s.brain_rationale = d.get("rationale") or "—"
        s.positions = d.get("positions") or []
        s.open_orders = d.get("open_orders") or []
        s.inventory = d.get("inventory") or format_position_inventory(s.positions)
        # Close / mismatch stats for Logs summary strip
        strat = str(d.get("strat") or "").lower()
        val = str(d.get("validation") or "")
        if any(k in strat for k in ("close", "market_order", "flatten")) or "close" in val:
            s.close_attempts += 1
            res = d.get("result") or {}
            if res.get("success") or res.get("status") in ("executed", "filled", "Submitted"):
                s.close_ok += 1
        if "rejected" in val.lower() or "mismatch" in val.lower():
            s.mismatches += 1
        rec = {**d, "ts": _now(), "type": "cycle"}
        s.records.append(rec)
        if d.get("tweak") and d["tweak"] != "none":
            s.tweaks.append(
                {
                    "cycle": d["cycle"],
                    "summary": d["tweak"],
                    "obj": d.get("tweak_obj", {}),
                    "before": d.get("tweak_before") or {},
                    "after": dict(TWEAKS),
                    "ts": _now(),
                }
            )

    async def _do_panic(self) -> None:
        try:
            conn = self.conn or get_ibkr_connector()
            if not getattr(conn, "connected", False):
                await conn.connect()
            before_ledger = []
            try:
                before_ledger = (
                    await conn.get_positions() if hasattr(conn, "get_positions") else []
                )
            except Exception:
                pass
            res = (
                await conn.flatten_all()
                if hasattr(conn, "flatten_all")
                else {"status": "logged"}
            )
            res = dict(res)
            res["before_ledger"] = before_ledger
            self.ui.put(("panic", res))
        except Exception as e:
            self.ui.put(("error", f"PANIC ERROR: {e}"))

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
            self.state.running = False
            self.state.status = "Safe"
            return
        hist, prev, n = [], 0.0, 0
        while gen == self._gen and not self.stop.is_set():
            if self.pause.is_set():
                await asyncio.sleep(0.25)
                continue
            n += 1
            try:
                out = await run_cycle(n, self.conn, g, hist, prev)
                prev = out["pnl"]
                rec = hist[-1] if hist else {}
                out.setdefault("action_obj", rec.get("action", {}))
                out.setdefault(
                    "rationale", (rec.get("action") or {}).get("rationale", "")
                )
                out.setdefault(
                    "reasoning_chain",
                    rec.get("reasoning_chain") or out.get("rationale", ""),
                )
                out.setdefault("inventory", rec.get("inventory", ""))
                out.setdefault("validation", rec.get("validation", ""))
                out.setdefault("impact", rec.get("impact", {}))
                out.setdefault(
                    "reality_pulse",
                    rec.get("reality_pulse")
                    or (rec.get("snapshot") or {}).get("reality_pulse")
                    or {},
                )
                out.setdefault("kahneman", rec.get("kahneman") or {})
                out.setdefault("kahneman_trace", rec.get("kahneman_trace") or "")
                if not out.get("positions"):
                    snap = rec.get("snapshot") or {}
                    out["positions"] = snap.get("positions") or []
                self.ui.put(("cycle", out))
            except Exception as e:
                self.ui.put(("error", str(e)))
            await asyncio.sleep(float(TWEAKS.get("cycle_sleep_s", 8)))
