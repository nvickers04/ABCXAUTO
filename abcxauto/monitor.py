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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from abcxauto.broker.order_types import is_stop_order
from abcxauto.config import get_config
from abcxauto.marketdata.market_hours import get_session_info

logger = logging.getLogger(__name__)


def build_protection_report(
    positions: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Match each position with its working stop/target orders.

    Stock positions without a working stop order are flagged unprotected.
    Option positions are listed for review but not stop-audited (multi-leg
    structures carry defined risk in the structure itself).
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
                if o.get("symbol") == symbol
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
            entry["note"] = "option position - protection audit not applied"

        report.append(entry)

    return {
        "positions": report,
        "unprotected_symbols": unprotected,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class PortfolioMonitor:
    """Owns the background task; exposes the latest snapshot for the UI."""

    def __init__(self, session: Any, connector: Any) -> None:
        self.session = session
        self.connector = connector
        self.cfg = get_config()
        self.latest: Dict[str, Any] = {}
        self._task: Optional[asyncio.Task] = None
        self._last_review_ts: float = 0.0
        self._last_unprotected_nudge_ts: float = 0.0

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

    async def _tick(self) -> None:
        snapshot = await self.take_snapshot()
        if not snapshot:
            return

        now = time.monotonic()
        unprotected = snapshot["protection"]["unprotected_symbols"]
        has_positions = bool(snapshot["protection"]["positions"])

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

        snapshot = {
            "connected": True,
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "positions": positions,
            "open_orders": orders,
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
                "[monitor] URGENT: unprotected position(s) detected: "
                f"{', '.join(protection.get('unprotected_symbols', []))}. "
                "Every position must have a stop loss and take profit. Immediately "
                "propose an 'oca' protective pair (or trailing_stop) for each "
                "unprotected position using sensible levels from current prices/ATR."
            )
        else:
            header = (
                "[monitor] Scheduled portfolio review. Assess P&L and current protection. "
                "If a stop should be tightened (e.g. move to breakeven after a favorable "
                "run) or a target adjusted, propose modify_stop / modify_target with the "
                "order_id from the protection report. If everything is well-placed, reply "
                "briefly with 'No changes needed' and one line of reasoning per position."
            )

        message = (
            f"{header}\n"
            f"Account: {pnl_bits or 'n/a'}\n"
            f"Protection report: {json.dumps(protection, default=str)}"
        )
        try:
            await self.session.inject(message, source="monitor")
        except Exception as e:
            logger.warning(f"Monitor review injection failed: {e}")
