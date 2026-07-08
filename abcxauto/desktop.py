"""ABCXAUTO Desktop v0.1 — Tkinter trading cockpit."""

import asyncio, json, logging, os, queue, sys, threading, tkinter as tk
from pathlib import Path
from tkinter import scrolledtext

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.llm import GrokClient
from abcxauto.rocket import TWEAKS, apply_tweak, run_cycle

TITLE = "ABCXAUTO Rocket – Autonomous Portfolio"
class RocketApp:
    def __init__(self, root: tk.Tk | None = None):
        self.root = root or tk.Tk()
        self.root.title(TITLE)
        self.root.geometry("900x620")
        self.root.configure(bg="#1a1a2e")
        self.stop, self.ui = threading.Event(), queue.Queue()
        self.cycles, self.pnl, self.equity = 0, 0.0, 0.0
        self.status, self.last_tweak, self.conn = "Safe", {}, None
        self.worker: threading.Thread | None = None
        self._worker_gen = 0
        self._poll_after_id: int | None = None
        self.status_var = tk.StringVar(value=self._status_line())
        self.port_var = tk.StringVar(value="Portfolio: —")
        self.risk_var = tk.StringVar(value="Risk: —")
        self._build()
        self._schedule_poll()

    def _status_line(self) -> str:
        return f"Cycles: {self.cycles} | PnL: ${self.pnl:+.2f} | Equity: ${self.equity:,.2f} | Status: {self.status}"

    def _build(self) -> None:
        bf = tk.Frame(self.root, bg="#1a1a2e", pady=8)
        bf.pack(fill="x")
        for txt, cmd, bg in (
            ("START AUTONOMOUS", self.start, "#2d6a4f"),
            ("STOP", self.stop_loop, "#9b2226"),
            ("PANIC FLATTEN ALL", self.panic, "#9b2226"),
        ):
            tk.Button(bf, text=txt, command=cmd, bg=bg, fg="white", font=("Segoe UI", 11, "bold"),
                      padx=12, pady=6).pack(side="left", padx=6)
        tk.Label(self.root, textvariable=self.status_var, bg="#16213e", fg="#a8dadc",
                 font=("Consolas", 11), anchor="w", padx=10, pady=6).pack(fill="x")
        self.log = scrolledtext.ScrolledText(self.root, height=18, bg="#0f0f23", fg="#e0e0e0",
                                             font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=4)
        imp = tk.LabelFrame(self.root, text="Last Improvement", bg="#1a1a2e", fg="#a8dadc")
        imp.pack(fill="x", padx=8, pady=4)
        self.imp_txt = tk.Text(imp, height=3, bg="#0f0f23", fg="#ffd166", font=("Consolas", 9))
        self.imp_txt.pack(fill="x", padx=6, pady=4)
        tk.Button(imp, text="Apply Now", command=self.apply_now, bg="#457b9d", fg="white").pack(pady=4)
        lf = tk.Frame(self.root, bg="#1a1a2e")
        lf.pack(fill="x", padx=8, pady=6)
        tk.Label(lf, textvariable=self.port_var, bg="#1a1a2e", fg="#8ecae6").pack(anchor="w")
        tk.Label(lf, textvariable=self.risk_var, bg="#1a1a2e", fg="#8ecae6").pack(anchor="w")

    def shutdown_ui(self) -> None:
        if self._poll_after_id is not None:
            try: self.root.after_cancel(self._poll_after_id)
            except Exception: pass
            self._poll_after_id = None

    def _log(self, msg: str) -> None:
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _invalidate_worker(self) -> None:
        self.stop.set(); self._worker_gen += 1

    def start(self) -> None:
        if not get_config().xai_api_key:
            self._log("ERROR: XAI_API_KEY missing")
            return
        self._worker_gen += 1
        gen = self._worker_gen
        self.stop.clear()
        self.status = "Running"
        self.status_var.set(self._status_line())
        self._log("=== START AUTONOMOUS ===")
        self.worker = threading.Thread(target=lambda: self._worker(gen), daemon=True)
        self.worker.start()

    def stop_loop(self) -> None:
        self._invalidate_worker()
        self.status = "Safe"
        self.status_var.set(self._status_line())
        self._log("=== STOPPED ===")
        self.worker = None

    def panic(self) -> None:
        self._invalidate_worker()
        self.status = "Safe"
        self.status_var.set(self._status_line())
        self._log("=== PANIC FLATTEN ALL ===")
        self.worker = None
        threading.Thread(target=lambda: asyncio.run(self._panic()), daemon=True).start()

    async def _panic(self) -> None:
        try:
            conn = self.conn or get_ibkr_connector()
            if not getattr(conn, "connected", False):
                await conn.connect()
            res = await conn.flatten_all() if hasattr(conn, "flatten_all") else {"status": "logged"}
            self.ui.put(("log", f"PANIC result: {json.dumps(res, default=str)}"))
        except Exception as e:
            self.ui.put(("log", f"PANIC ERROR: {e}"))

    def apply_now(self) -> None:
        raw = self.imp_txt.get("1.0", "end").strip()
        if raw.startswith("{"):
            try:
                tw = json.loads(raw)
            except json.JSONDecodeError:
                tw = dict(self.last_tweak)
        else:
            tw = dict(self.last_tweak) if self.last_tweak else {"type": "none", "summary": raw}
        msg = apply_tweak(tw)
        self.imp_txt.delete("1.0", "end")
        self.imp_txt.insert("end", f"applied: {msg}")
        self._log(f"Apply Now: {msg}")

    def _apply_ui_message(self, kind: str, data) -> None:
        if kind == "cycle":
            self.cycles, self.pnl, self.equity = data["cycle"], data["pnl"], data["equity"]
            self.port_var.set(f"Portfolio: {data['portfolio']}")
            self.risk_var.set(f"Risk: {data['risk']}")
            self.status_var.set(self._status_line())
            self._log(f"cycle={data['cycle']} action={data['strat']} pnl_chg={data['pnl_chg']:+.2f} "
                      f"result={json.dumps(data['result'], default=str)[:120]}")
            if data.get("tweak") and data["tweak"] != "none":
                self.last_tweak = data.get("tweak_obj") or {}
                self.imp_txt.delete("1.0", "end")
                self.imp_txt.insert("end", data["tweak"])
                self._log(f"improvement: {data['tweak']}")
        elif kind == "status_safe":
            self.status = "Safe"
            self.status_var.set(self._status_line())
        elif kind == "log":
            self._log(str(data))

    def _schedule_poll(self) -> None:
        self._poll_after_id = self.root.after(100, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                try:
                    kind, data = self.ui.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._apply_ui_message(kind, data)
                except Exception as e:
                    self._log(f"UI ERROR: {e}")
        finally:
            self._schedule_poll()

    def _worker(self, gen: int) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(self._async_worker(gen))

    async def _async_worker(self, gen: int) -> None:
        if gen != self._worker_gen:
            return
        try:
            self.conn = get_ibkr_connector()
            await self.conn.connect()
            g = GrokClient()
        except Exception as e:
            self.ui.put(("log", f"WORKER INIT ERROR: {e}"))
            self.ui.put(("status_safe", None))
            return
        hist, prev, n = [], 0.0, 0
        while gen == self._worker_gen and not self.stop.is_set():
            n += 1
            try:
                out = await run_cycle(n, self.conn, g, hist, prev)
                prev = out["pnl"]
                self.ui.put(("cycle", out))
            except Exception as e:
                self.ui.put(("log", f"ERROR: {e}"))
            await asyncio.sleep(float(TWEAKS.get("cycle_sleep_s", 8)))
def run_app() -> None:
    logging.basicConfig(level=logging.WARNING)
    app = RocketApp()
    probe = os.environ.get("ABCXAUTO_LAUNCH_PROBE")
    if probe:
        t = f"title={app.root.title()}\nexpected={TITLE}\nstatus={app.status}\nmainloop_ready=True\n"
        Path(probe).write_text(t, encoding="utf-8")
        print(f"ABCXAUTO title={app.root.title()} mainloop_ready=True status={app.status}", flush=True)
        app.shutdown_ui()
        try:
            app.root.destroy()
        except Exception:
            pass
        return
    app.root.mainloop()