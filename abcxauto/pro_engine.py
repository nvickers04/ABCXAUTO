"""Pro engine — START/stop/panic, rocket loop, view state (stdlib only, no Flet)."""

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
from abcxauto.llm import GrokClient
from abcxauto.rocket import TWEAKS, apply_tweak, grok, run_cycle


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
    records: list[dict] = field(default_factory=list)
    tweaks: list[dict] = field(default_factory=list)
    connected: bool = False
    running: bool = False
    status: str = "Safe"
    last_action: dict = field(default_factory=dict)
    brain_strat: str = "—"
    brain_rationale: str = "Start autonomous mode to see Grok decisions."
    risk: str = "—"


class ProEngine:
    """Autonomous rocket loop + thread-safe UI queue. Wired to START button via ProTerminal."""

    def __init__(self) -> None:
        self.ui: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self._gen = 0
        self.worker: threading.Thread | None = None
        self.conn: Any = None
        self.state = ViewState()

    def start(self) -> str | None:
        if not get_config().xai_api_key:
            return "XAI_API_KEY missing"
        if self.worker and self.worker.is_alive():
            return None
        self._gen += 1
        gen = self._gen
        self.stop.clear()
        self.state.running = True
        self.state.status = "Running"
        self.worker = threading.Thread(target=lambda: self._worker(gen), daemon=True)
        self.worker.start()
        return None

    def stop_engine(self) -> None:
        self.stop.set()
        self._gen += 1
        self.state.running = False
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

    def apply_tweak_manual(self, tw: dict) -> str:
        return apply_tweak(tw)

    async def grok_analyze_tweak(self, tw: dict) -> None:
        try:
            g = GrokClient()
            txt = await grok(g, f"Analyze this portfolio tweak and suggest ONE follow-up:\n{json.dumps(tw)}\nJSON response.")
            self.ui.put(("log", f"Grok analysis: {txt[:500]}"))
        except Exception as e:
            self.ui.put(("error", f"Analyze failed: {e}"))

    def _apply(self, kind: str, data: Any) -> None:
        s = self.state
        if kind == "conn":
            s.connected = bool(data)
        elif kind == "cycle":
            self._on_cycle(data)
        elif kind == "panic":
            s.records.append({
                "cycle": 0, "type": "panic", "ts": _now(),
                "position_results": data.get("position_results", []),
                "msg": json.dumps(data, default=str)[:2000],
                "reasoning_chain": "\n".join(
                    r.get("reasoning", "") for r in data.get("position_results", []) if r.get("reasoning")
                ),
            })
        elif kind in ("log", "error"):
            s.records.append({"cycle": 0, "type": "error", "msg": str(data), "ts": _now()})

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
        s.brain_strat = d.get("strat", "hold")
        s.brain_rationale = d.get("rationale") or "—"
        s.positions = d.get("positions") or []
        rec = {**d, "ts": _now(), "type": "cycle"}
        s.records.append(rec)
        if d.get("tweak") and d["tweak"] != "none":
            s.tweaks.append({
                "cycle": d["cycle"], "summary": d["tweak"], "obj": d.get("tweak_obj", {}),
                "before": d.get("tweak_before") or {}, "after": dict(TWEAKS), "ts": _now(),
            })

    async def _do_panic(self) -> None:
        try:
            conn = self.conn or get_ibkr_connector()
            if not getattr(conn, "connected", False):
                await conn.connect()
            # Capture before ledger for Logs display of before/after
            before_ledger = []
            try:
                before_ledger = await conn.get_positions() if hasattr(conn, 'get_positions') else []
            except Exception:
                pass
            res = await conn.flatten_all() if hasattr(conn, "flatten_all") else {"status": "logged"}
            res = dict(res)  # copy
            res['before_ledger'] = before_ledger
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
            return
        hist, prev, n = [], 0.0, 0
        while gen == self._gen and not self.stop.is_set():
            n += 1
            try:
                out = await run_cycle(n, self.conn, g, hist, prev)
                prev = out["pnl"]
                rec = hist[-1] if hist else {}
                out["action_obj"] = rec.get("action", {})
                out["rationale"] = (rec.get("action") or {}).get("rationale", "")
                out["reasoning_chain"] = rec.get("reasoning_chain") or out["rationale"]
                out["inventory"] = rec.get("inventory", "")
                out["validation"] = rec.get("validation", "")
                snap = rec.get("snapshot") or {}
                out["positions"] = snap.get("positions") or []
                self.ui.put(("cycle", out))
            except Exception as e:
                self.ui.put(("error", str(e)))
            await asyncio.sleep(float(TWEAKS.get("cycle_sleep_s", 8)))