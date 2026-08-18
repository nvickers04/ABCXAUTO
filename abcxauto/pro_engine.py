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
from abcxauto.agent_loop import run_cycle, snap
from abcxauto.cycle import format_position_inventory

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


_HOLD_LIKE = frozenset({"hold", "skipped", "blocked", "set_risk", "self_tune", "none", "", "—"})


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
    reconfig: dict = field(default_factory=dict)
    brain_strat: str = "—"
    brain_rationale: str = (
        "Start autonomous — Grok decides every RTH cycle; risk gates constrain."
    )
    opportunities: list[dict] = field(default_factory=list)
    ibkr_live_last: float | None = None
    ibkr_live_symbol: str = ""
    scan_fetched: list[str] = field(default_factory=list)
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
    pace: dict = field(default_factory=dict)
    think_live: str = ""
    tool_trace: list[str] = field(default_factory=list)
    book_unreliable: bool = False
    skip_reason: str = ""


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
        self._wake_event: asyncio.Event | None = None
        self._wake_reason: str = ""
        self._wake_gate: Any = None
        self._last_grok_mono: float = 0.0
        self._last_pace: dict = {}
        self._last_cycle_out: dict = {}
        from abcxauto.think_stream import bind_engine

        bind_engine(self)

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
        try:
            from abcxauto.self_tune import ensure_immutable_floor

            ensure_immutable_floor(persist=True)
        except Exception:
            logger.exception("immutable floor seed failed")
        already = bool(self.worker and self.worker.is_alive())
        # Connect path already refreshes universe; only re-scan when START
        # resumes an existing IBKR worker (Connect then START).
        self._universe_refresh_on_start = already
        if already:
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
        self.pause.set()
        self.state.autonomous = False
        self.state.paused = True
        self.state.running = False
        self.state.status = "Connected" if self.state.connected else "Paused"
        # Decisions-only: do not clear active_trade_plan or flatten.
        self._note("PAUSE", "Agent paused — IBKR still connected; open risk kept")
        self._apply_open_risk(note=True, allow_flat_close=False)

    def stop_engine(self) -> None:
        was_linked = bool(self.state.connected) or (
            self.worker is not None and self.worker.is_alive()
        )
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
        try:
            from abcxauto.think_stream import mark_review_stale

            mark_review_stale()
        except Exception:
            logger.debug("mark_review_stale on stop failed", exc_info=True)
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

    def request_wake(self, reason: str) -> None:
        """Interrupt cycle sleep for a whitelisted pace wake (monitor → engine)."""
        from abcxauto.pacing import WakeGate

        if not self.state.autonomous or self.pause.is_set() or self.stop.is_set():
            return
        if self._wake_gate is None:
            self._wake_gate = WakeGate()
        if not self._wake_gate.try_wake(reason):
            return
        self._wake_reason = str(reason or "").strip().lower()
        ev = self._wake_event
        if ev is not None:
            ev.set()
        try:
            self.ui.put(("log", f"PACE WAKE: {self._wake_reason}"))
        except Exception:
            pass

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
            self.monitor = PortfolioMonitor(
                stub, self.conn, on_wake=self.request_wake
            )
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
        self.state.close_attempts = 0
        self.state.close_ok = 0
        self.state.mismatches = 0
        self.state.hold_count = 0
        self.state.trade_count = 0
        self.state.gate_blocks = 0

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
                from abcxauto.world_state import daily_pnl_of

                pnl = daily_pnl_of(acct)
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
        elif kind == "log":
            s.records.append(
                {"cycle": 0, "type": "log", "msg": str(data), "ts": _now()}
            )
        elif kind == "error":
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
        s.reconfig = d.get("reconfig") or {}
        s.brain_strat = d.get("strat", "hold")
        s.brain_rationale = d.get("rationale") or "—"
        s.market_read = str(d.get("market_read") or "").strip()
        s.opportunities = list(d.get("opportunities") or [])
        s.ibkr_live_last = d.get("ibkr_live_last")
        if s.ibkr_live_last is None:
            s.ibkr_live_last = (d.get("world_state") or {}).get("ibkr_live_last")
        s.ibkr_live_symbol = str(
            d.get("ibkr_live_symbol")
            or (d.get("world_state") or {}).get("ibkr_live_symbol")
            or ""
        )
        s.scan_fetched = list(
            d.get("scan_fetched")
            or (d.get("world_state") or {}).get("scan_fetched")
            or []
        )
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
        s.tool_trace = list(d.get("tool_trace") or [])
        s.book_unreliable = bool(
            d.get("book_unreliable")
            or ((d.get("world_state") or {}).get("gates") or {}).get("book_unreliable")
        )
        note = str((d.get("result") or {}).get("note") or d.get("validation") or "")
        s.skip_reason = note if "skipped_grok" in note or "book_unreliable" in note else ""
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
        s.pace = dict(d.get("pace") or {})
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
        from abcxauto.pacing import WakeGate

        self._wake_event = asyncio.Event()
        self._wake_gate = WakeGate()
        self._wake_reason = ""
        try:
            self.conn = get_ibkr_connector()
            while gen == self._gen and not self.stop.is_set():
                try:
                    ok = await self.conn.connect()
                except Exception as e:
                    ok = False
                    msg = f"IBKR connect failed: {e}"
                    self.ui.put(("error", msg))
                    self.state.last_error = msg
                    self._note("ERR", msg)
                if ok and getattr(self.conn, "connected", False):
                    break
                self.ui.put(("conn", False))
                self.state.connected = False
                self.state.status = "Waiting IBKR"
                self._note("CONNECT", "TWS not listening — retry 15s")
                for _ in range(15):
                    if gen != self._gen or self.stop.is_set():
                        self._worker_loop = None
                        self.worker = None
                        return
                    await asyncio.sleep(1)
            if gen != self._gen or self.stop.is_set() or not getattr(self.conn, "connected", False):
                self.ui.put(("conn", False))
                self.state.connected = False
                self._worker_loop = None
                self.worker = None
                return
            self.ui.put(("conn", True))
            self.state.connected = True
            self._publish_ibkr_account()
            if not self.state.autonomous:
                self.state.running = False
                self.state.status = "Connected"
            self._note("CONNECT", "IBKR linked")
            try:
                from abcxauto.universe import refresh_legal_set

                al = await refresh_legal_set(self.conn, persist=True)
                n = len(al.get("legal_symbols") or [])
                self._note("UNIVERSE", f"sandbox refreshed n={n} src={al.get('source')}")
            except Exception as ue:
                self._note("UNIVERSE", f"refresh skipped: {ue}")
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
        from abcxauto.pacing import wait_for_pace
        from abcxauto.wake_bus import (
            BookEvent,
            book_fingerprint,
            ensure_next_look,
            events_from_diff,
            load_alarm,
            note_wake,
            pulse_sleep_s,
            save_alarm,
            should_wake_grok,
        )

        g = None
        hist, prev, n = [], 0.0, 0
        fp: dict | None = None
        first_boot = True
        try:
            while gen == self._gen and not self.stop.is_set():
                # Keep monitor alive whenever the broker link is up.
                if self.monitor is None or not getattr(self.monitor, "running", False):
                    if getattr(get_config(), "monitor_enabled", True):
                        self._start_monitor()
                if getattr(self, "_universe_refresh_on_start", False) and self.conn is not None:
                    self._universe_refresh_on_start = False
                    try:
                        from abcxauto.universe import refresh_legal_set

                        al = await refresh_legal_set(self.conn, persist=True)
                        n_legal = len(al.get("legal_symbols") or [])
                        self._note(
                            "UNIVERSE",
                            f"start refresh n={n_legal} src={al.get('source')}",
                        )
                    except Exception as ue:
                        self._note("UNIVERSE", f"start refresh skipped: {ue}")
                if not self.state.autonomous or self.pause.is_set():
                    await asyncio.sleep(0.25)
                    continue
                if g is None:
                    g = GrokClient()

                cfg = get_config()
                try:
                    s = await snap(self.conn)
                except Exception as e:
                    self.ui.put(("error", f"snap failed: {e}"))
                    s = {}
                cur = book_fingerprint({
                    **(s if isinstance(s, dict) else {}),
                    "ibkr_connected": bool(getattr(self.conn, "connected", False)),
                })
                events = events_from_diff(fp, cur)
                fp = cur
                pending_wake = str(self._wake_reason or "").strip().lower()
                if pending_wake in ("unprotected", "halt"):
                    events.append(BookEvent(pending_wake, pending_wake))
                elif pending_wake == "fill":
                    events.append(BookEvent("fill", "monitor fill"))
                alarm = load_alarm()
                ev = should_wake_grok(
                    events,
                    alarm=alarm,
                    first_boot=first_boot,
                    operator=pending_wake in ("operator", "flat_confirmed"),
                )
                if ev is None:
                    pace = {
                        "tier": "pulse",
                        "sleep_s": pulse_sleep_s(alarm),
                        "bypass_grok_min": True,
                        "reason": "watching",
                        "wake_reason": "",
                    }
                    self._last_pace = pace
                    self.state.pace = dict(pace)
                    self.state.status = "Watching"
                    self._wake_event.clear()
                    await wait_for_pace(float(pace["sleep_s"]), self._wake_event)
                    continue

                first_boot = False
                note_wake(ev)
                if ev.kind == "alarm":
                    alarm.wake_at = None
                    save_alarm(alarm)
                n += 1
                wake_for_cycle = ev.kind
                self._wake_event.clear()
                self._wake_reason = ""
                out: dict = {}
                prior_alarm = load_alarm().set_at
                sess = ""
                hours = s.get("market_hours") if isinstance(s, dict) else None
                block = hours.get("session") if isinstance(hours, dict) else None
                if isinstance(block, dict):
                    sess = str(block.get("status") or "")
                elif isinstance(block, str):
                    sess = block
                pos = (s.get("positions") if isinstance(s, dict) else None) or []
                try:
                    from abcxauto.think_stream import emit as think_emit

                    think_emit(
                        "say",
                        f"Cycle {n}: {ev.kind} {ev.detail} — Grok.\n".strip()
                        + "\n",
                    )
                    out = await run_cycle(n, self.conn, g, hist, prev)
                    skipped_note = str(out.get("validation") or out.get("rationale") or "")
                    skipped = "skipped_grok" in skipped_note
                    if not skipped:
                        self._last_grok_mono = time.monotonic()
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
                    if not out.get("positions"):
                        shot = rec.get("snapshot") or {}
                        out["positions"] = shot.get("positions") or []
                    pace = {
                        "tier": "event",
                        "sleep_s": pulse_sleep_s(load_alarm()),
                        "bypass_grok_min": True,
                        "reason": ev.kind,
                        "wake_reason": ev.kind,
                    }
                    out["pace"] = pace
                    self._last_pace = pace
                    self._last_cycle_out = out
                    self.ui.put(("cycle", out))
                except Exception as e:
                    self.ui.put(("error", str(e)))
                    self._last_pace = {
                        "tier": "pulse",
                        "sleep_s": pulse_sleep_s(),
                        "reason": "grok_error",
                    }
                finally:
                    ensure_next_look(
                        previous_set_at=prior_alarm,
                        flat=not bool(pos),
                        session=sess,
                    )

                self._wake_event.clear()
                await wait_for_pace(
                    float((self._last_pace or {}).get("sleep_s") or pulse_sleep_s()),
                    self._wake_event,
                )
        finally:
            self._stop_monitor()
            self._worker_loop = None
            self._wake_event = None
