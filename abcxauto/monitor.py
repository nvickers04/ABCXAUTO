"""Background portfolio monitor: P&L snapshots + Grok review injections.

Two loops in one task:
- every ``monitor_poll_s`` seconds: refresh a snapshot (positions, account
  P&L, open orders, protection audit) for the UI and for change detection;
- every ``monitor_review_s`` seconds (market hours, or any time a position is
  unprotected): inject a [monitor] message into the AgentSession so Grok
  reviews P&L and manages stops/targets via propose_order. All proposals
  auto-execute immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from abcxauto.broker.order_types import is_stop_order
from abcxauto.config import get_config
from abcxauto.marketdata.market_hours import get_session_info
from abcxauto.memory import get_journal
from abcxauto.risk_gates import get_risk_gate

logger = logging.getLogger(__name__)

WakeCallback = Callable[[str], None]


def _account_float(account: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in account and account[key] is not None:
            try:
                return float(account[key])
            except (TypeError, ValueError):
                continue
        lower = key.lower()
        if lower in account and account[lower] is not None:
            try:
                return float(account[lower])
            except (TypeError, ValueError):
                continue
    return None


def _days_to_expiry(expiration: Any) -> Optional[int]:
    """Parse YYYYMMDD (or YYYY-MM-DD) expiration into days-to-expiry, or None."""
    if expiration is None:
        return None
    raw = str(expiration).replace("-", "").strip()[:8]
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        exp = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None
    return (exp - date.today()).days


def _con_id(obj: Dict[str, Any]) -> Optional[str]:
    """Normalize conId/con_id to a comparable string, or None if absent/empty."""
    raw = obj.get("conId")
    if raw is None:
        raw = obj.get("con_id")
    if raw is None or raw == "":
        return None
    return str(raw)


def _order_matches_position(position: Dict[str, Any], order: Dict[str, Any]) -> bool:
    """Prefer conId match when both sides expose it; else symbol + secType."""
    pos_con = _con_id(position)
    ord_con = _con_id(order)
    if pos_con is not None and ord_con is not None:
        return pos_con == ord_con
    pos_sec = position.get("sec_type", "STK")
    ord_sec = order.get("sec_type", "STK")
    return (
        order.get("symbol") == position.get("symbol", "")
        and ord_sec == pos_sec
    )


def build_protection_report(
    positions: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Match each position with its working stop/target orders.

    Stock positions without a working stop order are flagged unprotected.
    Matching prefers conId when both the position and order expose one;
    otherwise falls back to symbol + secType (avoids option-leg / stale
    same-symbol stops masquerading as stock protection).
    Option positions get a light advisory audit (market value, DTE, short flag)
    but are not stop-audited — multi-leg structures carry defined risk in the
    structure itself. Short option legs are flagged for Grok review only.
    """
    report = []
    unprotected = []

    for p in positions or []:
        qty = p.get("quantity") or 0
        if not qty:
            continue
        symbol = p.get("symbol", "")
        sec_type = p.get("sec_type", "STK")
        exit_action = "SELL" if qty > 0 else "BUY"

        entry = {
            "symbol": symbol,
            "sec_type": sec_type,
            "quantity": qty,
            "avg_cost": p.get("avg_cost"),
            "market_price": p.get("market_price"),
            "market_value": p.get("market_value"),
            "unrealized_pnl": p.get("unrealized_pnl"),
            "realized_pnl": p.get("realized_pnl"),
        }

        if sec_type == "STK":
            same_side_exits = [
                o for o in (orders or [])
                if _order_matches_position(p, o)
                and o.get("sec_type", "STK") == "STK"
                and o.get("action") == exit_action
            ]
            stops = [o for o in same_side_exits if is_stop_order(o.get("order_type", ""))]
            targets = [o for o in same_side_exits if o.get("order_type") == "LMT"]
            entry["stop_orders"] = stops
            entry["target_orders"] = targets
            entry["protected"] = bool(stops)
            missing = []
            if not stops:
                missing.append("stop_loss")
            if not targets:
                missing.append("take_profit")
            if missing:
                entry["missing"] = missing
            if not stops:
                unprotected.append(symbol)
        else:
            # Light option audit — advisory only (does not block or auto-panic).
            expiration = (
                p.get("expiration")
                or p.get("lastTradeDateOrContractMonth")
                or p.get("expiry")
            )
            dte = _days_to_expiry(expiration)
            if dte is not None:
                entry["days_to_expiry"] = dte
            if expiration is not None:
                entry["expiration"] = expiration
            if p.get("strike") is not None:
                entry["strike"] = p.get("strike")
            if p.get("right") is not None:
                entry["right"] = p.get("right")
            try:
                qty_n = float(qty)
            except (TypeError, ValueError):
                qty_n = 0.0
            if qty_n < 0:
                entry["note"] = "short option — review risk"
                entry["flag"] = "short option — review risk"
            else:
                entry["note"] = "option position — light audit (no stop matching)"

        report.append(entry)

    return {
        "positions": report,
        "unprotected_symbols": unprotected,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class PortfolioMonitor:
    """Owns the background task; exposes the latest snapshot for the UI."""

    def __init__(
        self,
        session: Any,
        connector: Any,
        *,
        on_wake: WakeCallback | None = None,
    ) -> None:
        self.session = session
        self.connector = connector
        self.cfg = get_config()
        self.on_wake = on_wake
        self.latest: Dict[str, Any] = {}
        self._task: Optional[asyncio.Task] = None
        self._last_review_ts: float = 0.0
        self._last_unprotected_nudge_ts: float = 0.0
        self._prev_unprotected: set[str] = set()
        self._prev_fill_keys: set[str] = set()
        self._prev_halted: bool = False
        self._prev_had_plan: bool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"Portfolio monitor started (poll={self.cfg.monitor_poll_s}s, "
            f"review={self.cfg.monitor_review_s}s)"
        )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Monitor tick failed: {e}")
            await asyncio.sleep(max(5, self.cfg.monitor_poll_s))

    def _emit_wake(self, reason: str) -> None:
        cb = self.on_wake
        if cb is None:
            return
        try:
            cb(reason)
        except Exception as e:
            logger.warning("Monitor wake callback failed: %s", e)

    def _detect_pace_wakes(self, snapshot: Dict[str, Any]) -> None:
        """Whitelist wakes for Pro adaptive pacing (no Grok from monitor)."""
        if self.on_wake is None:
            return
        prot = snapshot.get("protection") or {}
        unprot = {str(s).upper() for s in (prot.get("unprotected_symbols") or []) if s}
        if unprot - self._prev_unprotected:
            self._emit_wake("unprotected")
        self._prev_unprotected = unprot

        halted = bool(get_risk_gate().is_halted)
        if halted and not self._prev_halted:
            self._emit_wake("halt")
        self._prev_halted = halted

        fill_keys: set[str] = set()
        for f in snapshot.get("fills") or []:
            if not isinstance(f, dict):
                continue
            key = str(
                f.get("execId")
                or f.get("exec_id")
                or f.get("execution_id")
                or f.get("orderId")
                or f.get("order_id")
                or ""
            )
            if not key:
                key = (
                    f"{f.get('symbol')}|{f.get('side')}|{f.get('shares')}|{f.get('time')}"
                )
            fill_keys.add(key)
        if self._prev_fill_keys and (fill_keys - self._prev_fill_keys):
            self._emit_wake("fill")
        if fill_keys:
            self._prev_fill_keys = fill_keys

        try:
            from abcxauto.trade_plan import load_trade_plan

            has_plan = load_trade_plan() is not None
        except Exception:
            has_plan = False
        positions = snapshot.get("positions") or []
        flat_book = not any(
            abs(float(p.get("quantity") or p.get("position") or 0)) > 1e-9
            for p in positions
            if isinstance(p, dict)
        )
        if self._prev_had_plan is True and not has_plan and flat_book:
            self._emit_wake("flat_confirmed")
        self._prev_had_plan = has_plan

    async def _tick(self) -> None:
        snapshot = await self.take_snapshot()
        if not snapshot:
            return

        await self._maybe_auto_panic(snapshot)
        self._detect_pace_wakes(snapshot)

        now = time.monotonic()
        unprotected = snapshot["protection"]["unprotected_symbols"]
        has_positions = bool(snapshot["protection"]["positions"])

        # Stub / non-agent sessions (ProEngine) skip Grok review entirely;
        # auto-panic, snapshots, fills, and equity tracking still run above.
        if not self._supports_agent_review():
            return

        # Unprotected positions are urgent: nudge Grok at most every 2 minutes,
        # regardless of the normal review interval.
        if unprotected and now - self._last_unprotected_nudge_ts > 120:
            self._last_unprotected_nudge_ts = now
            self._last_review_ts = now
            await self._ask_grok_review(snapshot, urgent=True)
            return

        if not has_positions:
            return
        if now - self._last_review_ts < self.cfg.monitor_review_s:
            return
        if not self._market_active():
            return

        self._last_review_ts = now
        await self._ask_grok_review(snapshot, urgent=False)

    def _supports_agent_review(self) -> bool:
        """True when session can accept Grok review injections (web AgentSession)."""
        flag = getattr(self.session, "supports_agent_review", None)
        if flag is not None:
            return bool(flag)
        # Default True for AgentSession / test fakes without the flag.
        return True

    async def _maybe_auto_panic(self, snapshot: Dict[str, Any]) -> None:
        """On daily-loss breach: halt once, flatten_all, inject a clear message.

        The risk-gate halt latch guards against repeated flatten on every poll.
        """
        cfg = self.cfg
        if not cfg.auto_panic_on_breach or cfg.daily_loss_limit_pct <= 0:
            return

        gate = get_risk_gate()
        if gate.is_halted:
            return

        account = snapshot.get("account") or {}
        net_liq = _account_float(account, "netliquidation", "NetLiquidation")
        daily_pnl = _account_float(account, "dailypnl", "DailyPnL")
        if net_liq is None or net_liq <= 0 or daily_pnl is None:
            return

        limit = -(cfg.daily_loss_limit_pct / 100.0) * net_liq
        if daily_pnl > limit:
            return

        reason = (
            f"AUTO-PANIC: daily PnL {daily_pnl:.2f} breached "
            f"{cfg.daily_loss_limit_pct}% of NL ({limit:.2f} on {net_liq:.2f})"
        )
        logger.critical(reason)
        gate.halt(reason, kind="auto_panic")

        flatten_result: Any = {"skipped": True}
        try:
            if hasattr(self.connector, "flatten_all"):
                flatten_result = await self.connector.flatten_all()
        except Exception as e:
            logger.error(f"AUTO-PANIC flatten_all failed: {e}")
            flatten_result = {"error": str(e)}

        message = (
            f"[monitor] {reason}. Trading halted; flatten_all invoked "
            f"({flatten_result}). New entries blocked until resume or next day."
        )
        try:
            await self.session.inject(message, source="monitor")
        except Exception as e:
            logger.warning(f"AUTO-PANIC inject failed: {e}")

    def _market_active(self) -> bool:
        try:
            session = get_session_info().get("session")
        except Exception:
            return True  # fail open — better to review than to skip
        if session == "regular":
            return True
        if self.cfg.monitor_extended_hours and session in ("premarket", "postmarket"):
            return True
        return False

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    async def take_snapshot(self) -> Dict[str, Any]:
        """Refresh positions/account/orders and cache the combined snapshot."""
        if hasattr(self.connector, "connected") and not self.connector.connected:
            self.latest = {
                "connected": False,
                "taken_at": datetime.now(timezone.utc).isoformat(),
            }
            return {}

        positions = await self.connector.get_positions()
        orders = await self.connector.get_open_orders()
        account = await self.connector.get_account_summary()
        protection = build_protection_report(positions, orders)

        # Feed peak-equity tracker for the self-clearing drawdown gate.
        net_liq = _account_float(account or {}, "netliquidation", "NetLiquidation")
        if net_liq is not None and net_liq > 0:
            get_risk_gate().update_equity(net_liq)

        journal = get_journal()
        journal.record_snapshot(account or {}, positions, orders)

        fills: list = []
        # Cheap idempotent fill ingest (hasattr so fakes without get_fills stay green).
        if hasattr(self.connector, "get_fills"):
            try:
                fills = await self.connector.get_fills() or []
                journal.record_fills(fills)
            except Exception as e:
                logger.warning(f"Monitor fill ingest failed: {e}")
                fills = []

        snapshot = {
            "connected": True,
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "positions": positions,
            "open_orders": orders,
            "fills": list(fills)[-20:],
            "protection": protection,
        }
        self.latest = snapshot
        self.session.emit({"type": "snapshot", "snapshot": snapshot})
        return snapshot

    # ------------------------------------------------------------------
    # Grok review
    # ------------------------------------------------------------------

    async def _ask_grok_review(self, snapshot: Dict[str, Any], urgent: bool) -> None:
        import json

        account = snapshot.get("account", {})
        protection = snapshot.get("protection", {})
        pnl_bits = ", ".join(
            f"{k}={account.get(k)}"
            for k in ("netliquidation", "dailypnl", "unrealizedpnl", "realizedpnl")
            if k in account
        )

        if urgent:
            header = (
                "[monitor] FACT: unprotected: "
                f"{', '.join(protection.get('unprotected_symbols', []))}."
            )
        else:
            header = "[monitor] FACT: scheduled book snapshot."

        message = (
            f"{header}\n"
            f"Account: {pnl_bits or 'n/a'}\n"
            f"Protection report: {json.dumps(protection, default=str)}"
        )
        try:
            await self.session.inject(message, source="monitor")
        except Exception as e:
            logger.warning(f"Monitor review injection failed: {e}")
