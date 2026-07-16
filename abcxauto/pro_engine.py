"""Pro engine — START/pause/panic, cycle loop, view state (stdlib only, no Flet)."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.llm import GrokClient
from abcxauto.cycle import (
    TWEAKS,
    apply_tweak,
    format_position_inventory,
    grok,
    run_cycle,
)

logger = logging.getLogger(__name__)


class _MonitorStubSession:
    """Minimal session for PortfolioMonitor on the Pro path.

    Forwards snapshots to the UI queue and logs injects. Does NOT call
    AgentSession / Grok — ``supports_agent_review=False`` skips review nudges.
    """

    supports_agent_review = False

    def __init__(self, engine: "ProEngine") -> None:
        self._engine = engine

    def emit(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if event.get("type") == "snapshot" and event.get("snapshot") is not None:
            try:
                self._engine.ui.put(("monitor_snapshot", event["snapshot"]))
            except Exception:
                pass

    async def inject(self, text: str, *, source: str = "monitor") -> None:
        line = f"[{source}] {text}"
        logger.info(line[:500])
        try:
            self._engine.ui.put(("log", line[:800]))
        except Exception:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


_HOLD_LIKE = frozenset({"hold", "skipped", "blocked", "set_risk", "none", "", "—"})


def _decision_kind(strat: str) -> str:
    """Map strategy name to hold vs trade for the book strip / scorecard."""
    s = (strat or "").strip().lower()
    if s in _HOLD_LIKE:
        return "hold"
    return "trade"


def _unprotected_list(d: dict) -> list:
    unprotected = d.get("unprotected")
    if unprotected is None:
        prot = d.get("protection") or {}
        unprotected = prot.get("unprotected_symbols")
    if unprotected is None:
        pulse = d.get("reality_pulse") or {}
        prot = (pulse.get("protection") or {}) if isinstance(pulse, dict) else {}
        unprotected = prot.get("unprotected_symbols")
    return list(unprotected or [])


def _halted_now() -> bool:
    try:
        from abcxauto.risk_gates import get_risk_gate

        return bool(get_risk_gate().is_halted)
    except Exception:
        return False


def _build_portfolio_view(
    *,
    equity: float,
    pnl: float,
    positions: list,
    unprotected: list,
    strat: str,
    halted: bool,
    portfolio_raw: Any = None,
) -> dict:
    """Book view for the Pro UI from cycle ``portfolio_state`` or engine fields."""
    if isinstance(portfolio_raw, dict) and portfolio_raw:
        view = dict(portfolio_raw)
        view.setdefault("net_liquidation", equity)
        view.setdefault("daily_pnl", pnl)
        view.setdefault("unprotected_count", len(unprotected))
        view.setdefault("unprotected_symbols", list(unprotected))
        view.setdefault("last_decision", _decision_kind(strat))
        view.setdefault("halted", halted)
        view.setdefault("n_positions", len(positions or []))
        return view
    return {
        "net_liquidation": equity,
        "daily_pnl": pnl,
        "unprotected_count": len(unprotected),
        "unprotected_symbols": list(unprotected),
        "last_decision": _decision_kind(strat),
        "halted": halted,
        "n_positions": len(positions or []),
        "summary": (
            portfolio_raw
            if isinstance(portfolio_raw, str)
            else f"{len(positions or [])} positions"
        ),
    }


def compute_mandate_health(
    *,
    unprotected_count: int,
    halted: bool,
    equity: float,
    daily_pnl: float,
    gate_blocks: int = 0,
) -> tuple[str, str]:
    """Return (level, label) where level is green|amber|red."""
    if halted or unprotected_count > 0:
        why = []
        if halted:
            why.append("halt")
        if unprotected_count > 0:
            why.append(f"{unprotected_count} unprotected")
        return "red", " · ".join(why) or "breach"
    # Amber: daily loss > 50% of limit, or many gate blocks (best-effort).
    try:
        limit_pct = float(getattr(get_config(), "daily_loss_limit_pct", 0.0) or 0)
    except Exception:
        limit_pct = 2.0
    if limit_pct > 0 and equity > 0 and daily_pnl < 0:
        limit_abs = (limit_pct / 100.0) * equity
        if abs(daily_pnl) > 0.5 * limit_abs:
            return "amber", f"daily loss {daily_pnl:+.0f} > 50% limit"
    if gate_blocks >= 3:
        return "amber", f"{gate_blocks} gate blocks"
    return "green", "protected"


@dataclass
class ViewState:
    cycles: int = 0
    pnl: float = 0.0
    equity: float = 0.0
    pnl_chg: float = 0.0
    equity_hist: list[float] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    open_orders: list[dict] = field(default_factory=list)
    recent_fills: list[dict] = field(default_factory=list)
    inventory: str = ""
    records: list[dict] = field(default_factory=list)
    tweaks: list[dict] = field(default_factory=list)
    connected: bool = False
    ibkr_account_id: str = ""
    ibkr_account_name: str = ""
    running: bool = False
    paused: bool = False
    autonomous: bool = False
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
    retest: dict = field(default_factory=dict)
    order_suite: dict = field(default_factory=dict)
    order_suite_summary: str = ""
    lab_pass_rate: float = 0.0
    brain_strat: str = "—"
    brain_rationale: str = (
        "Start autonomous — Grok decides every RTH cycle; risk gates constrain."
    )
    opportunities: list[dict] = field(default_factory=list)
    news_items: list[dict] = field(default_factory=list)
    market_read: str = ""
    risk_posture: str = ""
    last_params: dict = field(default_factory=dict)
    world_state: dict = field(default_factory=dict)
    judgment: dict = field(default_factory=dict)
    stance: str = ""
    thesis: str = ""
    dismissed: str = ""
    intent: dict = field(default_factory=dict)
    stage_error: str = ""
    trade_plan: dict | None = None
    regime: dict = field(default_factory=dict)
    portfolio_risk: dict = field(default_factory=dict)
    structure_grade: str = ""
    structure_lessons: list = field(default_factory=list)
    risk: str = "—"
    close_attempts: int = 0
    close_ok: int = 0
    mismatches: int = 0
    # Book strip / mandate (Phase 4 Pro UI)
    portfolio: dict = field(default_factory=dict)
    mandate_health: str = "green"
    mandate_health_label: str = "protected"
    last_decision: str = "—"
    halted: bool = False
    unprotected_count: int = 0
    hold_count: int = 0
    trade_count: int = 0
    gate_blocks: int = 0
    last_error: str | None = None


class ProEngine:
    """Autonomous cycle loop + thread-safe UI queue. Wired to START via ProTerminal."""

    def __init__(self) -> None:
        self.ui: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self.pause = threading.Event()  # set = paused
        self._gen = 0
        self.worker: threading.Thread | None = None
        self.conn: Any = None
        self.state = ViewState()
        self.monitor: Any = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._suite_lock = threading.Lock()

    def connect_broker(self) -> str | None:
        """Connect IBKR + start monitor without running agent cycles.

        Does not require an xAI key. Idempotent if the worker is already up.
        """
        if self.worker and self.worker.is_alive():
            if self.state.connected and not self.state.autonomous:
                self.state.running = False
                self.state.paused = False
                self.state.status = "Connected"
            return None
        self._gen += 1
        gen = self._gen
        self.stop.clear()
        self.pause.clear()
        self.state.autonomous = False
        self.state.running = False
        self.state.paused = False
        self.state.status = "Connecting"
        self._note("CONNECT", "Connecting to IBKR…")
        self.worker = threading.Thread(target=lambda: self._worker(gen), daemon=True)
        self.worker.start()
        return None

    def start(self) -> str | None:
        """Start autonomous agent cycles (requires xAI; connects IBKR if needed)."""
        if not get_config().xai_api_key:
            return "XAI_API_KEY missing"
        if self.worker and self.worker.is_alive():
            self.pause.clear()
            self.state.autonomous = True
            self.state.paused = False
            self.state.running = True
            self.state.status = "Running"
            self._note("START", "Autonomous agent running")
            return None
        self._gen += 1
        gen = self._gen
        self.stop.clear()
        self.pause.clear()
        self.state.autonomous = True
        self.state.running = True
        self.state.paused = False
        self.state.status = "Running"
        self._note("START", "Starting autonomous agent…")
        self.worker = threading.Thread(target=lambda: self._worker(gen), daemon=True)
        self.worker.start()
        return None

    def _note(self, kind: str, msg: str) -> None:
        """Append a short lifecycle line to the activity log."""
        self.state.records.append(
            {"cycle": 0, "type": str(kind).lower(), "msg": msg, "ts": _now()}
        )
        if len(self.state.records) > 200:
            self.state.records = self.state.records[-200:]

    def run_startup_suite(self) -> None:
        """Run order suite once at startup (always dry-run; never a second asyncio loop)."""
        self._schedule_suite("startup", allow_paper_place=False)

    def run_manual_suite(self) -> None:
        """Start worker if needed, then paper place→cancel the full suite."""
        from abcxauto.config import get_config
        if not get_config().is_paper:
            self.ui.put(("error", "Test Suite is paper-only — switch profile to Paper"))
            return
        err = self.ensure_broker_ready()
        if err:
            self.ui.put(("error", f"Test Suite: {err}"))
            return
        self._schedule_suite("manual", allow_paper_place=True)

    def run_strategy_test(self, strategy: str) -> dict:
        """Start worker if needed, then paper place→cancel one order type."""
        from abcxauto.config import get_config
        if not get_config().is_paper:
            self.ui.put(("error", "Test Suite is paper-only — switch profile to Paper"))
            return {"strategy": strategy, "pass": False, "detail": "live mode blocked"}
        err = self.ensure_broker_ready()
        if err:
            self.ui.put(("error", f"Test Suite: {err}"))
            return {"strategy": strategy, "pass": False, "detail": err}
        loop = self._worker_loop
        assert loop is not None and loop.is_running()
        asyncio.run_coroutine_threadsafe(
            self._run_strategy_broker_once(strategy), loop
        )
        return {"strategy": strategy, "mode": "paper_pending", "pass": True, "detail": "running on paper…"}

    def ensure_broker_ready(self, timeout: float = 45.0) -> str | None:
        """Connect IBKR (no agent cycles) and wait until the broker link is up."""
        err = self.connect_broker()
        if err:
            return err
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            loop = self._worker_loop
            conn = self.conn
            if (
                loop is not None
                and loop.is_running()
                and conn is not None
                and bool(getattr(conn, "connected", False))
            ):
                return None
            if self.worker is not None and not self.worker.is_alive():
                return "IBKR connect failed — start TWS/Gateway on the paper port"
            time.sleep(0.1)
        return "Timed out waiting for paper IBKR connection"

    def _merge_strategy_row(self, strategy: str, row: dict, *, mode: str) -> None:
        from abcxauto.order_suite import (
            format_order_suite_summary,
            get_cached_suite,
            set_cached_suite,
        )

        cached = get_cached_suite() or {}
        results = [r for r in (cached.get("results") or []) if r.get("strategy") != strategy]
        results.append(row)
        passed = sum(1 for r in results if r.get("pass"))
        failed = sum(1 for r in results if not r.get("pass"))
        report = {
            **cached,
            "taken_at": row.get("taken_at") or cached.get("taken_at"),
            "source": f"single:{strategy}",
            "paper_only": True,
            "mode": mode,
            "results": results,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / max(1, passed + failed), 3),
            "strategies_tested": len(results),
            "summary": (
                f"order suite [single:{strategy}] "
                f"{'PASS' if row.get('pass') else 'FAIL'} — "
                f"{passed} pass / {failed} fail mode={mode}"
            ),
            "idle_prevented": True,
        }
        set_cached_suite(report)
        self.state.order_suite = report
        self.state.order_suite_summary = format_order_suite_summary(report)
        self.state.lab_summary = self.state.order_suite_summary
        self.ui.put(("order_suite", report))

    async def _run_strategy_broker_once(self, strategy: str) -> None:
        from abcxauto.order_suite import run_strategy_broker_test
        from abcxauto.reality_pulse import build_reality_pulse

        try:
            with self._suite_lock:
                conn = self.conn
                if conn is None or not getattr(conn, "connected", False):
                    conn = get_ibkr_connector()
                    if not getattr(conn, "connected", False):
                        await conn.connect()
                    self.conn = conn
                if not getattr(conn, "connected", False):
                    raise RuntimeError("IBKR not connected")
                pulse = build_reality_pulse(
                    ibkr_connected=True,
                    positions=self.state.positions,
                )
                row = await run_strategy_broker_test(
                    strategy,
                    connector=conn,
                    pulse=pulse,
                    positions=self.state.positions,
                )
            self._merge_strategy_row(
                strategy, row, mode=str(row.get("mode") or "paper")
            )
            self.ui.put(
                (
                    "log",
                    f"STRATEGY TEST {strategy}: "
                    f"{'PASS' if row.get('pass') else 'FAIL'} mode={row.get('mode')}",
                )
            )
        except Exception as e:
            fail = {
                "strategy": strategy,
                "pass": False,
                "mode": "broker_fail",
                "detail": str(e)[:300],
            }
            self._merge_strategy_row(strategy, fail, mode="broker_fail")
            self.ui.put(("error", f"STRATEGY TEST ERROR ({strategy}): {e}"))

    def switch_trading_mode(self, mode: str, *, live_confirm: str = "") -> None:
        """Apply session paper/live mode and reconnect broker when the worker is up."""
        from abcxauto.config import set_trading_mode

        cfg = set_trading_mode(mode, live_confirm=live_confirm)
        self.ui.put(("log", f"TRADING MODE → {cfg.trading_mode} port={cfg.ibkr_port}"))
        self.ui.put(("trading_mode", cfg.trading_mode))
        loop = self._worker_loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._reconnect_after_mode_switch(), loop
                )
            except Exception as e:
                self.ui.put(("error", f"Mode reconnect schedule failed: {e}"))

    async def _reconnect_after_mode_switch(self) -> None:
        try:
            if self.conn is not None and getattr(self.conn, "connected", False):
                await self.conn.disconnect()
            self.conn = get_ibkr_connector()
            await self.conn.connect()
            self.ui.put(("conn", True))
            self._publish_ibkr_account()
            self.ui.put(("log", "IBKR reconnected after mode switch"))
        except Exception as e:
            self.ui.put(("conn", False))
            self.ui.put(("error", f"Mode switch reconnect failed: {e}"))

    def _schedule_suite(self, source: str, *, allow_paper_place: bool) -> None:
        """Schedule suite on the worker loop. Manual paper tests never dry-run."""
        if allow_paper_place:
            err = self.ensure_broker_ready()
            if err:
                self.ui.put(("error", f"Test Suite: {err}"))
                return
        loop = self._worker_loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._run_suite_once(source, allow_paper_place=allow_paper_place),
                    loop,
                )
                return
            except Exception as e:
                if allow_paper_place:
                    self.ui.put(("error", f"Test Suite schedule failed: {e}"))
                    return
                logger.warning(f"suite schedule on worker failed ({e}); startup dry-run")
        if allow_paper_place:
            self.ui.put(("error", "Test Suite: worker loop not ready"))
            return
        # Startup-only: schema dry-run on a private loop (no broker).
        threading.Thread(
            target=lambda: asyncio.run(
                self._run_suite_once(source, allow_paper_place=False)
            ),
            daemon=True,
        ).start()

    async def _run_suite_once(
        self, source: str, *, allow_paper_place: bool = False
    ) -> None:
        from abcxauto.order_suite import (
            format_order_suite_summary,
            paper_place_enabled,
            run_order_suite,
        )
        from abcxauto.reality_pulse import build_reality_pulse

        label = source.upper().replace("_", " ")
        # Paper place→cancel on manual suite when paper mode (suite_paper_place default on).
        # Startup / no-worker fallback always dry-run.
        force_dry = not (allow_paper_place and paper_place_enabled())
        try:
            with self._suite_lock:
                conn = self.conn
                if not force_dry:
                    if conn is None or not getattr(conn, "connected", False):
                        conn = get_ibkr_connector()
                        if not getattr(conn, "connected", False):
                            await conn.connect()
                        self.conn = conn
                    if not getattr(conn, "connected", False):
                        raise RuntimeError("IBKR not connected for paper suite")
                pulse = build_reality_pulse(
                    ibkr_connected=bool(getattr(conn, "connected", False))
                    if conn
                    else False,
                    positions=self.state.positions,
                )
                report = await run_order_suite(
                    connector=conn if not force_dry else None,
                    pulse=pulse,
                    positions=self.state.positions,
                    source=source,
                    force_dry=force_dry,
                )
            self.state.order_suite = report
            self.state.order_suite_summary = format_order_suite_summary(report)
            self.state.order_lab = {
                "pass_rate": report.get("pass_rate"),
                "passed": report.get("passed"),
                "failed": report.get("failed"),
                "results": report.get("results") or [],
                "taken_at": report.get("taken_at"),
                "summary": report.get("summary"),
            }
            self.state.lab_pass_rate = float(report.get("pass_rate") or 0)
            self.state.lab_summary = self.state.order_suite_summary
            self.state.retest = {
                "after_suite": True,
                "reason": source,
                "summary": f"{source}: pass_rate={report.get('pass_rate')}",
                "order_suite": report,
            }
            self.ui.put(
                (
                    "log",
                    f"{label} ORDER SUITE: {report.get('summary')} idle_prevented=True",
                )
            )
            self.ui.put(("order_suite", report))
        except Exception as e:
            self.ui.put(("error", f"{label} SUITE ERROR: {e}"))

    def _apply_open_risk(
        self,
        positions: list | None = None,
        open_orders: list | None = None,
        *,
        note: bool = False,
        allow_flat_close: bool = True,
    ) -> None:
        """Reconcile ActiveTradePlan from broker book. Never clears on pause/stop."""
        from abcxauto.trade_plan import format_open_risk_line, sync_open_risk

        pos = list(positions if positions is not None else (self.state.positions or []))
        orders = list(
            open_orders if open_orders is not None else (self.state.open_orders or [])
        )
        thesis = str(self.state.thesis or "")
        try:
            from abcxauto.memory import get_journal

            thesis = thesis or (get_journal().get_working_thesis() or "")
        except Exception:
            pass
        try:
            plan = sync_open_risk(
                pos,
                orders,
                thesis=thesis,
                bump=False,
                allow_flat_close=allow_flat_close,
            )
        except Exception as e:
            logger.warning("open risk sync failed: %s", e)
            return
        self.state.trade_plan = plan.to_dict() if plan else None
        if note and plan:
            self._note("OPEN_RISK", format_open_risk_line(plan))

    def pause_engine(self) -> None:
        """Pause agent cycles without tearing down IBKR / monitor."""
        if not self.worker or not self.worker.is_alive():
            return
        was_auto = bool(self.state.autonomous)
        self.pause.set()
        self.state.autonomous = False
        self.state.paused = True
        self.state.running = False
        self.state.status = "Connected" if self.state.connected else "Paused"
        # Decisions-only: do not clear active_trade_plan or flatten.
        self._note("PAUSE", "Agent paused — IBKR still connected; open risk kept")
        self._apply_open_risk(note=True, allow_flat_close=False)
        if was_auto:
            try:
                from abcxauto.agent_loop import run_session_review_on_stop

                run_session_review_on_stop(
                    {
                        "thesis": self.state.thesis,
                        "what_worked": self.state.market_read,
                        "next_change": f"last={self.state.brain_strat}",
                    }
                )
            except Exception:
                pass

    def stop_engine(self) -> None:
        was_linked = bool(self.state.connected) or (
            self.worker is not None and self.worker.is_alive()
        )
        was_auto = bool(self.state.autonomous or self.state.running)
        self.stop.set()
        self.pause.clear()
        self._gen += 1
        self.state.running = False
        self.state.paused = False
        self.state.autonomous = False
        self.state.connected = False
        self.state.status = "Safe"
        self.state.ibkr_account_id = ""
        self.state.ibkr_account_name = ""
        self._stop_monitor()
        self.worker = None
        self._worker_loop = None
        self.conn = None
        # Keep active_trade_plan.json — Stop does not flatten or forget open risk.
        self._apply_open_risk(note=True, allow_flat_close=False)
        if was_auto:
            try:
                from abcxauto.agent_loop import run_session_review_on_stop

                run_session_review_on_stop(
                    {
                        "thesis": self.state.thesis,
                        "what_worked": self.state.market_read,
                        "next_change": f"last={self.state.brain_strat}",
                    }
                )
            except Exception:
                pass
        if was_linked:
            self._note(
                "DISCONNECT",
                "Disconnected from IBKR — open risk plan preserved on disk",
            )

    def request_snapshot(self) -> str | None:
        """Force one monitor snapshot (orders/fills/positions) on the worker loop."""
        loop = self._worker_loop
        mon = self.monitor
        if loop is None or not loop.is_running():
            return "Not connected — Connect IBKR first"
        if mon is None or not getattr(mon, "running", False):
            return "Monitor not running — Connect IBKR first"

        async def _once() -> None:
            try:
                snap = await mon.take_snapshot()
                if snap:
                    self.ui.put(("monitor_snapshot", snap))
            except Exception as e:
                self.ui.put(("error", f"Refresh failed: {e}"))

        asyncio.run_coroutine_threadsafe(_once(), loop)
        return None

    def _stop_monitor(self) -> None:
        mon = self.monitor
        if mon is not None:
            try:
                mon.stop()
            except Exception:
                pass
        self.monitor = None

    def _start_monitor(self) -> None:
        """Start PortfolioMonitor on the current worker asyncio loop (mirror web.py)."""
        self._stop_monitor()
        try:
            cfg = get_config()
            if not getattr(cfg, "monitor_enabled", True):
                return
            if self.conn is None or not getattr(self.conn, "connected", False):
                return
            from abcxauto.monitor import PortfolioMonitor

            stub = _MonitorStubSession(self)
            self.monitor = PortfolioMonitor(stub, self.conn)
            self.monitor.start()
            self.ui.put(("log", "Portfolio monitor started (pro path)"))
        except Exception as e:
            logger.warning(f"Portfolio monitor start failed: {e}")
            self.ui.put(("log", f"Monitor start skipped: {e}"))

    def panic(self) -> None:
        self.stop_engine()
        threading.Thread(target=lambda: asyncio.run(self._do_panic()), daemon=True).start()


    def _publish_ibkr_account(self) -> None:
        """Push IBKR account id/name into ViewState + UI queue."""
        conn = self.conn
        aid = str(getattr(conn, "account_id", "") or "") if conn else ""
        aname = str(getattr(conn, "account_name", "") or "") if conn else ""
        self.state.ibkr_account_id = aid
        self.state.ibkr_account_name = aname or ("IBKR" if aid else "")
        self.ui.put(("ibkr_account", {"id": aid, "name": self.state.ibkr_account_name}))

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
        self.state.hold_count = 0
        self.state.trade_count = 0
        self.state.gate_blocks = 0

    def apply_tweak_manual(self, tw: dict) -> str:
        return apply_tweak(tw)

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
        elif kind == "ibkr_account":
            payload = data or {}
            s.ibkr_account_id = str(payload.get("id") or "")
            s.ibkr_account_name = str(payload.get("name") or "")
        elif kind == "cycle":
            self._on_cycle(data)
        elif kind == "order_suite":
            s.order_suite = data or {}
            s.order_suite_summary = str((data or {}).get("summary") or "")
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
                    "type": "order_suite",
                    "ts": _now(),
                    "order_suite": data,
                    "lab_summary": s.order_suite_summary,
                    "msg": s.order_suite_summary,
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
        elif kind == "monitor_snapshot":
            # Keep equity / positions fresh from monitor polls between cycles.
            snap = data or {}
            acct = snap.get("account") or {}
            try:
                nl = acct.get("netliquidation") or acct.get("NetLiquidation")
                if nl is not None:
                    s.equity = float(nl)
            except (TypeError, ValueError):
                pass
            try:
                pnl = acct.get("dailypnl") or acct.get("DailyPnL")
                if pnl is not None:
                    s.pnl = float(pnl)
            except (TypeError, ValueError):
                pass
            if snap.get("positions") is not None:
                s.positions = snap.get("positions") or []
            if snap.get("open_orders") is not None:
                s.open_orders = snap.get("open_orders") or []
            if snap.get("fills") is not None:
                s.recent_fills = list(snap.get("fills") or [])[-20:]
            had_plan = bool(s.trade_plan)
            # Never confirmed-flat-close while paused / not autonomous — monitor
            # empty snaps must not wipe durable open risk.
            allow_close = bool(s.autonomous) and not bool(s.paused)
            self._apply_open_risk(
                s.positions,
                s.open_orders,
                note=not had_plan,
                allow_flat_close=allow_close,
            )
            prot = snap.get("protection") or {}
            unprotected = list(prot.get("unprotected_symbols") or [])
            s.unprotected_count = len(unprotected)
            s.halted = _halted_now()
            s.mandate_health, s.mandate_health_label = compute_mandate_health(
                unprotected_count=s.unprotected_count,
                halted=s.halted,
                equity=s.equity,
                daily_pnl=s.pnl,
                gate_blocks=s.gate_blocks,
            )
            s.portfolio = _build_portfolio_view(
                equity=s.equity,
                pnl=s.pnl,
                positions=s.positions,
                unprotected=unprotected,
                strat=s.brain_strat,
                halted=s.halted,
                portfolio_raw=s.portfolio if isinstance(s.portfolio, dict) else None,
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
        s.retest = d.get("retest") or {}
        s.order_suite = d.get("order_suite") or s.order_suite
        s.order_suite_summary = str(
            (d.get("lab_summary") or (s.order_suite or {}).get("summary") or "")
        )
        s.lab_pass_rate = float((s.order_lab or {}).get("pass_rate") or 0)
        s.brain_strat = d.get("strat", "hold")
        s.brain_rationale = d.get("rationale") or "—"
        s.market_read = str(d.get("market_read") or "").strip()
        s.opportunities = list(d.get("opportunities") or [])
        s.news_items = list(d.get("news_items") or [])
        s.risk_posture = str(d.get("risk_posture") or "")
        s.last_params = dict(d.get("params") or (d.get("action_obj") or {}).get("params") or {})
        s.world_state = dict(d.get("world_state") or {})
        s.judgment = dict(d.get("judgment") or {})
        s.stance = str(d.get("stance") or s.judgment.get("stance") or "")
        s.thesis = str(d.get("thesis") or s.judgment.get("thesis") or "")
        s.dismissed = str(d.get("dismissed") or s.judgment.get("dismissed") or "")
        s.intent = dict(d.get("intent") or s.judgment.get("intent") or {})
        s.stage_error = str(d.get("stage_error") or "")
        s.trade_plan = d.get("trade_plan")
        s.regime = dict(d.get("regime") or (s.world_state or {}).get("regime") or {})
        s.portfolio_risk = dict(
            d.get("portfolio_risk") or (s.world_state or {}).get("portfolio_risk") or {}
        )
        s.structure_grade = str(d.get("structure_grade") or "")
        s.structure_lessons = list(
            d.get("structure_lessons")
            or (s.world_state or {}).get("structure_lessons")
            or []
        )
        s.positions = d.get("positions") or []
        s.open_orders = d.get("open_orders") or []
        s.inventory = d.get("inventory") or format_position_inventory(s.positions)
        # Book strip / mandate health
        unprotected = _unprotected_list(d)
        s.unprotected_count = len(unprotected)
        s.halted = bool(d.get("halted")) if "halted" in d else _halted_now()
        s.last_decision = _decision_kind(str(d.get("strat") or ""))
        if s.last_decision == "hold":
            s.hold_count += 1
        else:
            s.trade_count += 1
        # Close / mismatch stats for Logs summary strip
        strat = str(d.get("strat") or "").lower()
        val = str(d.get("validation") or "")
        res = d.get("result") or {}
        if any(k in strat for k in ("close", "market_order", "flatten")) or "close" in val:
            s.close_attempts += 1
            if res.get("success") or res.get("status") in ("executed", "filled", "Submitted"):
                s.close_ok += 1
        if "rejected" in val.lower() or "mismatch" in val.lower():
            s.mismatches += 1
        status = str(res.get("status") or "").lower()
        if (
            status in ("blocked", "rejected", "gate_blocked")
            or "gate" in status
            or strat == "blocked"
        ):
            s.gate_blocks += 1
        s.mandate_health, s.mandate_health_label = compute_mandate_health(
            unprotected_count=s.unprotected_count,
            halted=s.halted,
            equity=s.equity,
            daily_pnl=s.pnl,
            gate_blocks=s.gate_blocks,
        )
        portfolio_raw = d.get("portfolio_state") or d.get("portfolio")
        s.portfolio = _build_portfolio_view(
            equity=s.equity,
            pnl=s.pnl,
            positions=s.positions,
            unprotected=unprotected,
            strat=str(d.get("strat") or ""),
            halted=s.halted,
            portfolio_raw=portfolio_raw,
        )
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
        self._worker_loop = asyncio.get_running_loop()
        try:
            self.conn = get_ibkr_connector()
            await self.conn.connect()
            self.ui.put(("conn", True))
            self.state.connected = True
            self._publish_ibkr_account()
            if not self.state.autonomous:
                self.state.running = False
                self.state.status = "Connected"
            self._note("CONNECT", "IBKR linked")
            self._start_monitor()
        except Exception as e:
            msg = f"IBKR connect failed: {e}"
            self.ui.put(("error", msg))
            self.ui.put(("conn", False))
            self.state.connected = False
            self.state.running = False
            self.state.autonomous = False
            self.state.status = "Safe"
            self.state.last_error = msg
            self._note("ERR", msg)
            self._worker_loop = None
            self.worker = None
            self.conn = None
            return
        g = None
        hist, prev, n = [], 0.0, 0
        try:
            while gen == self._gen and not self.stop.is_set():
                # Keep monitor alive whenever the broker link is up.
                if self.monitor is None or not getattr(self.monitor, "running", False):
                    if getattr(get_config(), "monitor_enabled", True):
                        self._start_monitor()
                if not self.state.autonomous or self.pause.is_set():
                    await asyncio.sleep(0.25)
                    continue
                if g is None:
                    g = GrokClient()
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
                    last_pulse = {}
                else:
                    last_pulse = out.get("reality_pulse") or {}
                cfg = get_config()
                sleep_s = float(getattr(cfg, "cycle_sleep_s", 300) or 300)
                # When RTH is closed, stretch idle sleep (skip stretch in fast-test sleeps).
                sess = str(
                    (last_pulse.get("session") or {}).get("status") or ""
                ).lower()
                if sess and sess != "regular" and sleep_s >= 60:
                    sleep_s = max(sleep_s, 900.0)
                await asyncio.sleep(sleep_s)
        finally:
            self._stop_monitor()
            self._worker_loop = None
