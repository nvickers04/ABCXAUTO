"""Pro engine — START/pause/panic, stay-up think host, view state (stdlib only, no Flet)."""

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
from abcxauto.agent_loop import (
    equity_of,
    format_position_inventory,
    pnl_of,
    risk_label,
    snap,
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
    """Book view for the Pro UI from ``portfolio_state`` or engine fields."""
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
    scan_hits: dict = field(default_factory=dict)
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
    # Burn: looks that cost a model call and produced no ticket.
    sends_last_look: int = 0
    looks_since_send: int = 0
    backoff_wait_s: float = 0.0


class ProEngine:
    """Stay-up Grok host + thread-safe UI queue. Wired to START via ProTerminal."""

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
        self._resume_think = False
        self._cold_next = False
        self._think_parked = False
        self._last_session = ""
        self._fail_streak = 0
        self._session_capped = False
        self._recover_same_chat = False
        self._recover_streak = 0
        self._brain_key: tuple = ()
        self._monitor_key: tuple = ()
        from abcxauto.think_stream import bind_engine

        bind_engine(self)

    def connect_broker(self) -> str | None:
        """Connect IBKR + start monitor without calling the model.

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
        """Host one stay-up Grok think (requires xAI; connects IBKR if needed)."""
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
            self.state.status = "Thinking"
            self._think_parked = False
            self._resume_think = True
            self._cold_next = False
            # Stay-up sits on `_wake_event.wait`. Book pokes set it;
            # operator Start must too or the desk paints "Grok on"
            # while the worker stays mid-wait with no look.
            ev = self._wake_event
            if ev is not None:
                loop = self._worker_loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(ev.set)
                else:
                    ev.set()
            self._note("START", "Grok running")
            return None
        self._gen += 1
        gen = self._gen
        self.stop.clear()
        self.pause.clear()
        self.state.autonomous = True
        self.state.running = True
        self.state.paused = False
        self.state.status = "Thinking"
        self._think_parked = False
        self._resume_think = True
        self._cold_next = False
        try:
            from abcxauto.park_clock import load_alarm, start_looks_now

            alarm = load_alarm()
            if alarm.wake_at and not alarm.due() and not start_looks_now(alarm):
                # Fresh launch: honor Grok's leftover park, except a
                # remaining-to-bell / session-card clock — that is a send
                # gate, not a think shutdown. Operator Start on a live
                # worker still pokes (already=True returned above).
                self._resume_think = False
                self.state.status = "Waiting"
        except Exception:
            pass
        self._note("START", "Starting Grok")
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
        try:
            plan = sync_open_risk(
                pos,
                orders,
                thesis="",
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
        """Pause think without tearing down IBKR / monitor."""
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
        self._monitor_key = ()

    @staticmethod
    def _monitor_fingerprint() -> tuple:
        cfg = get_config()
        return (
            bool(getattr(cfg, "monitor_enabled", True)),
            int(getattr(cfg, "monitor_poll_s", 30) or 30),
            int(getattr(cfg, "monitor_review_s", 300) or 300),
            bool(getattr(cfg, "monitor_extended_hours", False)),
        )

    @staticmethod
    def _brain_fingerprint() -> tuple:
        cfg = get_config()
        return (
            str(getattr(cfg, "model", "") or ""),
            float(getattr(cfg, "temperature", 0.0) or 0.0),
            int(getattr(cfg, "max_tokens", 0) or 0),
        )

    def request_wake(self, reason: str) -> None:
        """Interrupt pulse sleep for a whitelisted pace wake (monitor → engine)."""
        from abcxauto.pacing import WakeGate
        from abcxauto.park_clock import BookEvent, note_interrupt

        if (
            not self.state.autonomous
            or self.pause.is_set()
            or self.stop.is_set()
            or getattr(self, "_think_parked", False)
        ):
            return
        try:
            from abcxauto.session_caps import is_capped

            if is_capped(session=str(getattr(self, "_last_session", "") or "")):
                return
        except Exception:
            pass
        if self._wake_gate is None:
            self._wake_gate = WakeGate()
        if not self._wake_gate.try_wake(reason):
            return
        self._wake_reason = str(reason or "").strip().lower()
        # Mid-turn poke into the live xAI episode (fill / unprotected).
        note_interrupt(BookEvent(self._wake_reason, self._wake_reason))
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
            # PortfolioMonitor snapshots the config, so remember what it was
            # built with — a Settings change has to rebuild it.
            self._monitor_key = self._monitor_fingerprint()
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

    async def _stamp_session_start_nl(self) -> None:
        """First usable account NL of the ET day → journal snapshots.

        Scorecard session ``book_return_pct`` reads snapshots via
        ``nav_at_or_after``, not session_markers. Paper 7497 account only.
        """
        conn = self.conn
        fn = getattr(conn, "get_account_summary", None) if conn is not None else None
        if not callable(fn):
            return
        account = await fn()
        if not isinstance(account, dict):
            return
        nl = None
        for key in ("netliquidation", "NetLiquidation", "net_liquidation"):
            raw = account.get(key)
            if raw is None:
                continue
            try:
                nl = float(raw)
            except (TypeError, ValueError):
                continue
            if nl == nl and nl > 0:
                break
            nl = None
        if nl is None:
            return
        from abcxauto.memory import get_journal

        journal = get_journal()
        journal.ensure_session_start_nl(nl)
        try:
            from abcxauto.config import get_config

            journal.ensure_model_session(
                str(getattr(get_config(), "model", "") or ""),
                net_liquidation=nl,
            )
        except Exception:
            logger.debug("session marker after start NL failed", exc_info=True)

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
            # Keep equity / positions fresh from monitor polls between looks.
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
        s.opportunities = list(
            d.get("opportunities")
            or (d.get("world_state") or {}).get("opportunities")
            or []
        )
        s.scan_hits = dict(d.get("scan_hits") or {})
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
        s.news_items = list(
            d.get("news_items")
            or (d.get("world_state") or {}).get("news_items")
            or []
        )
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
        s.sends_last_look = int(d.get("sends") or 0)
        s.looks_since_send = 0
        if not d.get("_failed"):
            s.backoff_wait_s = 0.0
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
        try:
            from abcxauto.think_stream import write_last_turn

            write_last_turn(d)
        except Exception:
            logger.debug("last_turn persist on cycle failed", exc_info=True)

    @staticmethod
    def _session_of_snap(snap: dict | None) -> str:
        s = snap if isinstance(snap, dict) else {}
        pulse = s.get("reality_pulse") if isinstance(s.get("reality_pulse"), dict) else {}
        sess = pulse.get("session")
        if isinstance(sess, dict):
            status = str(sess.get("status") or "").strip().lower()
            if status:
                return status
        hours = s.get("market_hours") if isinstance(s.get("market_hours"), dict) else {}
        block = hours.get("session")
        if isinstance(block, dict):
            return str(block.get("status") or "").strip().lower()
        return str(block or "").strip().lower()

    @staticmethod
    def _resolve_session(session: str = "") -> str:
        from abcxauto.park_clock import resolve_stay_up_session

        return resolve_stay_up_session(session)

    def _idle_on_session_cap(self, session: str = "", *, note: bool = True) -> bool:
        """True when this session is out of looks or tokens. Idle, keep chat."""
        from abcxauto.session_caps import is_capped

        if not is_capped(session=session):
            self._session_capped = False
            return False
        first = not bool(getattr(self, "_session_capped", False))
        self._session_capped = True
        self._resume_think = False
        self.state.status = "Idle"
        if note and first:
            self._note("CAP", "session look/token cap — idle")
        return True

    def _rearm_after_think(self, out: dict | None, *, session: str) -> float:
        """Stay-up keeps the process. Next model call is a poke or a changed lead fact.

        Paper RTH / premarket stay on this process. A good look writes no
        grok_wake.json. Stay-up may sit — the runner does not self-schedule.
        Duplicate lead-fact looks end with no send and wait. Words with no
        tool_calls already stopped the model; this rearm waits for fill /
        order_change / unprotected / operator poke. Chat is kept. Overnight
        park is park_clock after a closed skip.
        """
        session = self._resolve_session(session)
        self._last_session = session
        payload = out if isinstance(out, dict) else {}
        from abcxauto.brain import _look_text_is_junk
        from abcxauto.park_clock import paper_stay_up

        stay = paper_stay_up(session)
        failed = bool(payload.get("_failed"))
        parked = bool(payload.get("_parked"))
        stream_err = str(payload.get("_stream_error") or "")
        rationale = str(payload.get("rationale") or "")
        try:
            sends = int(payload.get("sends") or 0)
        except (TypeError, ValueError):
            sends = 0
        # A spoken say or a send/fill is a finished look. Do not wipe chat.
        # Duplicate lead fact (_ended) waits for a poke, not a fresh desk.
        ended = bool(payload.get("_ended"))
        finished = ended or sends > 0 or not _look_text_is_junk(rationale)
        if finished:
            failed = False
            stream_err = ""
            self._recover_streak = 0
        if parked and not stay:
            self._fail_streak = 0
            self._cold_next = True
            return 0.0
        if stay:
            self._resume_think = False
            self._cold_next = False
        else:
            self._cold_next = bool(failed or parked or stream_err)
        if not failed:
            self._fail_streak = 0
        else:
            self._fail_streak = int(getattr(self, "_fail_streak", 0) or 0) + 1
            if not stay:
                self._resume_think = False
        if self._idle_on_session_cap(session):
            return 0.0
        return 0.0

    @staticmethod
    def _empty_grok_dead_s() -> float:
        from abcxauto.brain import empty_grok_dead_s

        return empty_grok_dead_s()

    def _same_chat_recover_needed(self, out: dict | None, g: Any) -> bool:
        """Empty GROK after tools, or UNAVAILABLE/IOCP, with a live chat.

        Re-enter think on this chat. Not _cold_next. Not a sit clock.
        """
        if g is None or getattr(g, "chat", None) is None:
            return False
        payload = out if isinstance(out, dict) else {}
        if payload.get("_ended") or payload.get("_parked"):
            return False
        try:
            if int(payload.get("sends") or 0) > 0:
                return False
        except (TypeError, ValueError):
            pass
        from abcxauto.brain import (
            EMPTY_GROK_RECOVER_TRIES,
            _look_text_is_junk,
            is_stream_abort_error,
        )

        streak = int(getattr(self, "_recover_streak", 0) or 0)
        if streak >= EMPTY_GROK_RECOVER_TRIES:
            return False
        rationale = str(payload.get("rationale") or "")
        spoken = not _look_text_is_junk(rationale)
        if spoken:
            return False
        if is_stream_abort_error(payload.get("_stream_error") or ""):
            return True
        tools = list(payload.get("tool_trace") or [])
        if tools and _look_text_is_junk(rationale):
            return True
        live = str(getattr(self.state, "think_live", "") or "")
        if live:
            from abcxauto.think_stream import empty_grok_after_tools

            if empty_grok_after_tools(live):
                return True
        return False

    def _arm_same_chat_recover(self) -> None:
        self._recover_streak = int(getattr(self, "_recover_streak", 0) or 0) + 1
        self._recover_same_chat = True
        self._resume_think = True
        self._cold_next = False

    async def _stay_up_lead_changed(self, g: Any) -> bool:
        """True when a collapsible lead fact moved since the last look."""
        from abcxauto.agent_loop import snap as take_snap
        from abcxauto.world_state import (
            build_world_state,
            day_facts,
            desk_fact_changed,
            worst_wake_fact,
        )

        if self.conn is None:
            return False
        try:
            s = await take_snap(self.conn)
        except Exception:
            logger.debug("stay-up lead snap failed", exc_info=True)
            return False
        if not isinstance(s, dict):
            return False
        world = build_world_state(
            cycle=0, snap=s, opportunities=[], news_items=[]
        )
        try:
            from abcxauto.scorecard import compute_scorecard

            sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
            day = day_facts(world, sc)
        except Exception:
            day = day_facts(world, None)
        fact = worst_wake_fact(
            unprotected=list(getattr(world, "unprotected", None) or []),
            day=day,
            session=str(getattr(world, "session_status", "") or ""),
        )
        prev = ""
        chat = getattr(g, "chat", None) if g is not None else None
        if chat is not None:
            prev = str(getattr(chat, "_abcx_last_desk_fact", "") or "")
        if not prev and g is not None:
            prev = str(getattr(g, "_last_desk_fact", "") or "")
        return desk_fact_changed(prev, fact)

    async def _host_think(
        self, n: int, g: Any, s: dict, *, resume: bool = False
    ) -> dict:
        """Call the model on the open chat. Stay-up resume keeps it; book events poke it."""
        from abcxauto.brain import grok_turn, grok_turn_kwargs
        from abcxauto.world_state import (
            build_world_state,
            day_facts,
            format_wake,
        )

        try:
            from abcxauto.memory import get_journal

            get_journal().ingest_look(s)
        except Exception:
            logger.debug("look journal ingest failed", exc_info=True)
        try:
            from abcxauto.think_stream import seed_snap_from_last_turn

            seed_snap_from_last_turn(s)
        except Exception:
            pass
        world = build_world_state(
            cycle=n, snap=s, opportunities=[], news_items=[],
        )
        day = None
        try:
            from abcxauto.scorecard import compute_scorecard

            sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
            day = day_facts(world, sc)
        except Exception:
            day = None
        wake = format_wake(
            cycle=n,
            session=world.session_status,
            flat=world.flat,
            unprotected=world.unprotected,
            ibkr_up=bool(getattr(self.conn, "connected", False)),
            day=day,
        )
        self.state.status = "Thinking"
        recover = bool(getattr(self, "_recover_same_chat", False))
        self._recover_same_chat = False
        turn = await grok_turn(
            g,
            **grok_turn_kwargs(
                grok_turn,
                connector=self.conn,
                world=world,
                snap=s,
                wake=wake,
                resume=resume or recover,
                recover=recover,
            ),
        )
        parked = bool(getattr(turn, "parked", False))
        failed = False
        look_fn = getattr(turn, "look_failed", None)
        if callable(look_fn):
            failed = bool(look_fn())
        else:
            failed = bool(getattr(turn, "failed", False))
        acct = s.get("account") or {}
        pnl = pnl_of(acct) if isinstance(acct, dict) else 0.0
        eq = equity_of(acct) if isinstance(acct, dict) else 0.0
        act = dict(turn.last_act or {})
        result = dict(turn.last_result or {})
        strat = str(turn.last_strat or act.get("strategy") or "")
        if not getattr(turn, "sends", None) and strat.lower() not in ("blocked",):
            # No send this look is yield, not a hold ticket.
            act = {}
            strat = ""
            result = {}
        return {
            "cycle": n,
            "pnl": pnl,
            "equity": eq,
            "pnl_chg": 0,
            "risk": risk_label(s),
            "action_obj": act,
            "result": result,
            "strat": strat,
            "rationale": act.get("rationale") or (turn.text or "")[:1200],
            "positions": s.get("positions") or [],
            "open_orders": s.get("open_orders") or [],
            "inventory": format_position_inventory(s.get("positions") or []),
            "reality_pulse": s.get("reality_pulse") or {},
            "world_state": world.to_dict() if hasattr(world, "to_dict") else {},
            "tool_trace": list(getattr(turn, "tool_trace", None) or []),
            "scan_hits": dict(s.get("scan_hits") or {}),
            "scan_screens": list(s.get("scan_screens") or []),
            "scan_calls": int(s.get("scan_calls") or 0),
            "scan_at": str(s.get("scan_at") or ""),
            "session_range": dict(s.get("session_range") or {}),
            "scan_fetched": list(getattr(world, "scan_fetched", None) or []),
            "news_items": list(
                getattr(world, "news_items", None) or s.get("news_items") or []
            ),
            "candle_source": (
                getattr(world, "candle_source", None) or s.get("candle_source") or ""
            ),
            "sends": len(getattr(turn, "sends", None) or []),
            "validation": str(result.get("note") or result.get("status") or "ok"),
            "structure_grade": str(act.get("_structure_grade") or ""),
            "pace": {"tier": "stay", "sleep_s": 0, "reason": "yield"},
            "_parked": parked,
            "_failed": failed and not parked,
            "_ended": bool(getattr(turn, "ended", False)),
            "_stream_error": str(getattr(turn, "stream_error", "") or ""),
            "_recover": recover,
        }

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
            try:
                await self._stamp_session_start_nl()
            except Exception:
                logger.debug("session-start NL stamp failed", exc_info=True)
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
        from abcxauto.park_clock import peek_interrupt, take_interrupt

        g = None
        n = 0
        first_think = True
        try:
            while gen == self._gen and not self.stop.is_set():
                mon_key = self._monitor_fingerprint()
                mon_live = self.monitor is not None and getattr(
                    self.monitor, "running", False
                )
                if not mon_key[0]:
                    if self.monitor is not None:
                        self._stop_monitor()
                        self._note("MONITOR", "stopped — monitor_enabled off")
                elif not mon_live:
                    self._start_monitor()
                elif mon_key != self._monitor_key:
                    self._note("MONITOR", f"pacing changed → restart {mon_key[1]}s/{mon_key[2]}s")
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
                if self.pause.is_set() and not getattr(self, "_think_parked", False):
                    await asyncio.sleep(0.25)
                    continue
                if (
                    not self.state.autonomous
                    and not getattr(self, "_think_parked", False)
                    and not getattr(self, "_resume_think", False)
                ):
                    await asyncio.sleep(0.25)
                    continue
                brain_key = self._brain_fingerprint()
                if g is None or brain_key != self._brain_key:
                    if g is not None:
                        self._note("BRAIN", f"model/limits changed → {brain_key[0]}")
                    g = GrokClient()
                    self._brain_key = brain_key

                want_look = bool(getattr(self, "_resume_think", False))
                cold = bool(getattr(self, "_cold_next", False))
                poked = peek_interrupt() is not None
                live_chat = g is not None and getattr(g, "chat", None) is not None
                # Poke / stay-up continue this chat. Do not start a new messages list.
                resume = (not cold) and (want_look or poked or live_chat)
                if getattr(self, "_think_parked", False) and not want_look:
                    # Park is shutdown. Do not wait on pokes. Start is the only resume.
                    self.state.status = "Parked"
                    await asyncio.sleep(0.25)
                    continue
                if getattr(self, "_session_capped", False) and not want_look:
                    # Cap idle: snap so a session roll (premarket → RTH) can
                    # stay-up through the open on a fresh budget. No sit clock.
                    try:
                        s_cap = await snap(self.conn)
                    except Exception as e:
                        self.ui.put(("error", f"snap failed: {e}"))
                        await asyncio.sleep(2.0)
                        continue
                    cap_sess = self._resolve_session(self._session_of_snap(s_cap))
                    self._last_session = cap_sess
                    from abcxauto.park_clock import paper_stay_up

                    if paper_stay_up(cap_sess):
                        if self._idle_on_session_cap(cap_sess):
                            await asyncio.sleep(0.25)
                            continue
                        self._resume_think = True
                        continue
                    self._session_capped = False
                    self._resume_think = True
                    continue
                from abcxauto.park_clock import (
                    PULSE_S,
                    clear_park,
                    honor_park,
                    infer_session_before_open,
                    load_alarm,
                    paper_stay_up,
                    pulse_sleep_s,
                )

                alarm = load_alarm()
                sess = self._resolve_session(
                    str(getattr(self, "_last_session", "") or "")
                )
                inferred, mins_now = infer_session_before_open()
                if inferred:
                    sess = inferred
                honor = honor_park(session=sess, minutes_to_open=mins_now)
                future_park = honor and bool(alarm.wake_at) and not alarm.due()
                if not poked and not want_look and (not first_think or future_park):
                    first_think = False
                    if honor and alarm.due():
                        self._resume_think = True
                        continue
                    if not honor:
                        if paper_stay_up(sess):
                            if self._idle_on_session_cap(sess):
                                await asyncio.sleep(0.25)
                                continue
                            try:
                                clear_park()
                            except Exception:
                                pass
                            # Stay-up may sit. Wait for a poke or a lead
                            # fact that actually changed — do not dump a
                            # fresh go-do-desk developer turn.
                            wait = float(PULSE_S)
                            self.state.status = "On"
                            ev = self._wake_event
                            timed_out = True
                            if ev is not None:
                                try:
                                    await asyncio.wait_for(ev.wait(), timeout=wait)
                                except asyncio.TimeoutError:
                                    timed_out = True
                                else:
                                    timed_out = False
                                    ev.clear()
                            else:
                                await asyncio.sleep(wait)
                            if peek_interrupt() is not None:
                                continue
                            if timed_out:
                                if await self._stay_up_lead_changed(g):
                                    self._resume_think = True
                                continue
                            continue
                        wait = float(PULSE_S)
                        self.state.status = "On"
                    else:
                        wait = pulse_sleep_s(alarm)
                        self.state.status = "Waiting" if alarm.wake_at else "On"
                    ev = self._wake_event
                    if ev is not None:
                        ev.clear()
                        try:
                            await asyncio.wait_for(ev.wait(), timeout=wait)
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(wait)
                    continue

                try:
                    s = await snap(self.conn)
                except Exception as e:
                    self.ui.put(("error", f"snap failed: {e}"))
                    await asyncio.sleep(2.0)
                    continue

                first_think = False
                self._resume_think = False
                self._cold_next = False
                self._wake_reason = ""
                session = self._resolve_session(self._session_of_snap(s))
                self._last_session = session
                from abcxauto.agent_loop import _wake_grok_for_session
                from abcxauto.park_clock import (
                    clear_park,
                    ensure_next_look,
                    honor_park,
                    load_alarm,
                    minutes_to_open_from_snap,
                    paper_stay_up,
                )

                prot = s.get("protection") if isinstance(s.get("protection"), dict) else {}
                needs_prot = bool(prot.get("unprotected_symbols"))
                mins_open = minutes_to_open_from_snap(s)
                flat_book = not (s.get("positions") or [])
                stay = paper_stay_up(session)
                if self._idle_on_session_cap(session):
                    continue
                if not _wake_grok_for_session(session, needs_prot=needs_prot):
                    # Overnight / after-close only. Do not call ensure_next_look
                    # in RTH / premarket — stay-up looks on this process.
                    if not stay:
                        try:
                            ensure_next_look(
                                flat=flat_book,
                                session=session,
                                minutes_to_open=mins_open,
                            )
                        except Exception:
                            self._note("WAKE", "next look seed failed")
                    try:
                        from abcxauto.brain import drop_live_chat

                        drop_live_chat(g)
                    except Exception:
                        pass
                    self.state.status = "Waiting"
                    self._note("SKIP", f"session={session or 'closed'} — no Grok")
                    continue

                n += 1
                from abcxauto.session_caps import billed_tokens_now, note_look

                before_tok = billed_tokens_now()
                try:
                    out = await self._host_think(n, g, s, resume=resume)
                    if not out.get("_recover"):
                        self._recover_same_chat = False
                    if not out.get("_ended") and not out.get("_recover"):
                        note_look(
                            session=session,
                            tokens=max(0, billed_tokens_now() - before_tok),
                        )
                    if stay and self._same_chat_recover_needed(out, g):
                        await asyncio.sleep(self._empty_grok_dead_s())
                        if self._same_chat_recover_needed(out, g):
                            self._arm_same_chat_recover()
                            self._note("LOOK", "empty GROK — same chat")
                            continue
                    if out.get("_ended"):
                        # Duplicate lead fact. A look may end.
                        self._rearm_after_think(out, session=session)
                        continue
                    if out.get("_parked"):
                        self.ui.put(("cycle", out))
                        if stay:
                            try:
                                clear_park()
                            except Exception:
                                pass
                            self._rearm_after_think(out, session=session)
                        elif honor_park(session=session, minutes_to_open=mins_open):
                            alarm = load_alarm()
                            if not (alarm.wake_at and alarm.seconds_until() is not None):
                                try:
                                    ensure_next_look(
                                        flat=flat_book,
                                        session=session,
                                        minutes_to_open=mins_open,
                                    )
                                except Exception:
                                    self._note("WAKE", "next look seed failed")
                                alarm = load_alarm()
                            self.state.status = "Waiting"
                            self._note(
                                "PARK",
                                f"next look {alarm.wake_at} — book events still wake it",
                            )
                        continue
                    self._last_grok_mono = time.monotonic()
                    self._last_cycle_out = out
                    self.state.status = "On"
                    self.ui.put(("cycle", out))
                    # Stay-up writes no sit clock and does not call
                    # ensure_next_look / set_wake. Overnight skip still parks.
                    if stay:
                        try:
                            clear_park()
                        except Exception:
                            pass
                    elif honor_park(session=session, minutes_to_open=mins_open):
                        if not out.get("_failed"):
                            try:
                                just_sent = int(out.get("sends") or 0) > 0
                                pos = list(
                                    out.get("positions") or s.get("positions") or []
                                )
                                ensure_next_look(
                                    flat=(not just_sent) and (not pos),
                                    session=session,
                                    minutes_to_open=mins_open,
                                    replace=True,
                                )
                            except Exception:
                                self._note("WAKE", "next look seed failed")
                    self._rearm_after_think(out, session=session)
                except Exception as e:
                    note_look(
                        session=session,
                        tokens=max(0, billed_tokens_now() - before_tok),
                    )
                    self.ui.put(("error", str(e)))
                    payload = {"_failed": True, "_stream_error": str(e)}
                    self._rearm_after_think(payload, session=session)
        finally:
            self._stop_monitor()
            self._worker_loop = None
            self._wake_event = None
