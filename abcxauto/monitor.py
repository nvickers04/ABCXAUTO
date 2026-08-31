"""Background portfolio monitor: P&L snapshots + Grok review injections.

Two clocks in one task:
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


def _sec_type(obj: Dict[str, Any] | None) -> str:
    p = obj if isinstance(obj, dict) else {}
    return str(p.get("sec_type") or p.get("secType") or p.get("sec") or "STK").upper()


def _qty(obj: Dict[str, Any] | None) -> float:
    p = obj if isinstance(obj, dict) else {}
    raw = p.get("quantity")
    if raw is None:
        raw = p.get("position")
    if raw is None:
        raw = p.get("totalQuantity")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


# Same slack as trade_plan stacked-stop cover: a crumb short still covers.
_COVER_QTY_SLACK = 0.51


def _leg_ratio(leg: Dict[str, Any] | None) -> float:
    raw = (leg or {}).get("ratio")
    try:
        ratio = float(raw if raw is not None else 1)
    except (TypeError, ValueError):
        ratio = 1.0
    return ratio if ratio > 0 else 1.0


def _covers_held_qty(
    held: float,
    order: Dict[str, Any],
    *,
    ratio: float = 1.0,
) -> bool:
    """True when order size (× BAG ratio) covers the open lot."""
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        r = 1.0
    if r <= 0:
        r = 1.0
    cover = abs(_qty(order)) * r
    return cover + 1e-9 >= abs(held) - _COVER_QTY_SLACK


def _order_id(order: Dict[str, Any] | None) -> Optional[int]:
    o = order if isinstance(order, dict) else {}
    raw = o.get("order_id")
    if raw is None:
        raw = o.get("orderId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_option_lot(position: Dict[str, Any] | None) -> bool:
    sec = _sec_type(position)
    return sec.startswith("OPT") or sec in ("FOP", "BAG")


def _opt_fp(obj: Dict[str, Any] | None) -> tuple[str, str, str, str]:
    p = obj if isinstance(obj, dict) else {}
    sym = str(p.get("symbol") or "").upper()
    exp = str(p.get("expiration") or p.get("lastTradeDateOrContractMonth") or p.get("expiry") or "")
    exp = exp[-6:] if len(exp) >= 6 else exp
    right = str(p.get("right") or "")[:1].upper()
    strike = p.get("strike")
    try:
        strike_s = f"{float(strike):g}"
    except (TypeError, ValueError):
        strike_s = str(strike or "")
    return (sym, exp, right, strike_s)


def _combo_legs(order: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    o = order if isinstance(order, dict) else {}
    legs = o.get("combo_legs") or o.get("comboLegs") or []
    return [leg for leg in legs if isinstance(leg, dict)]


def lot_audit_label(position: Dict[str, Any] | None) -> str:
    """STK uses ticker; OPT uses lot identity so a stock stop cannot mask a vert."""
    p = position if isinstance(position, dict) else {}
    if _is_option_lot(p):
        from abcxauto.world_state import lot_ident

        return lot_ident(p)
    return str(p.get("symbol") or "").upper()


def _bag_covers_lot(position: Dict[str, Any], order: Dict[str, Any]) -> bool:
    if _sec_type(order) != "BAG":
        return False
    pos_con = _con_id(position)
    if not pos_con:
        return False
    qty = _qty(position)
    if abs(qty) < 1e-9:
        return False
    want = "SELL" if qty > 0 else "BUY"
    parent = str(order.get("action") or "").upper()
    legs = _combo_legs(order)
    if not legs:
        return False
    for leg in legs:
        lid = _con_id(leg)
        if lid != pos_con:
            continue
        act = str(leg.get("action") or "").upper()
        if act == want or (not act and parent == want):
            return _covers_held_qty(qty, order, ratio=_leg_ratio(leg))
    return False


def _contract_matches(position: Dict[str, Any], order: Dict[str, Any]) -> bool:
    pos_sec = _sec_type(position)
    ord_sec = _sec_type(order)
    pos_opt = pos_sec.startswith("OPT") or pos_sec in ("FOP", "BAG")
    ord_opt = ord_sec.startswith("OPT") or ord_sec in ("FOP", "BAG")
    if pos_opt != ord_opt:
        return False
    pos_con = _con_id(position)
    ord_con = _con_id(order)
    if pos_con is not None and ord_con is not None:
        return pos_con == ord_con
    if pos_opt or ord_opt:
        fp = _opt_fp(position)
        return bool(fp[0]) and fp == _opt_fp(order)
    return (
        str(order.get("symbol") or "").upper() == str(position.get("symbol") or "").upper()
        and (pos_sec.startswith("STK") and ord_sec.startswith("STK"))
    )


def _covers_lot(position: Dict[str, Any], order: Dict[str, Any]) -> bool:
    """True when the working order fully closes this lot (incl. BAG)."""
    if _bag_covers_lot(position, order):
        return True
    if _sec_type(order) == "BAG":
        return False
    if not _contract_matches(position, order):
        return False
    qty = _qty(position)
    if abs(qty) < 1e-9:
        return False
    exit_action = "SELL" if qty > 0 else "BUY"
    action = str(order.get("action") or "").upper()
    if action != exit_action:
        return False
    return _covers_held_qty(qty, order)


def covering_exits(
    position: Dict[str, Any],
    orders: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """Working orders that close this lot. Unique by order id."""
    seen: set[int] = set()
    out: List[Dict[str, Any]] = []
    for o in orders or []:
        if not isinstance(o, dict) or not _covers_lot(position, o):
            continue
        oid = _order_id(o)
        if oid is not None:
            if oid in seen:
                continue
            seen.add(oid)
        out.append(o)
    return out


def _order_matches_position(position: Dict[str, Any], order: Dict[str, Any]) -> bool:
    """Prefer conId; OPT uses strike fingerprint; STK uses symbol + secType."""
    return _contract_matches(position, order)


def build_protection_report(
    positions: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Match each lot with covering stop/target orders at IBKR.

    Unprotected is STK without a working last-stop that covers the held qty.
    Option covering exits are facts only — they do not force hold or flatten.
    Combos close as one BAG; a short combo is not a full cover.
    ``orphaned_protection`` is the mirror image: exits with no lot left to
    cover, which the unprotected test cannot see because the book looks clean.
    """
    report = []
    unprotected = []

    for p in positions or []:
        qty = _qty(p)
        if not qty:
            continue
        symbol = p.get("symbol", "")
        sec_type = _sec_type(p)

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

        exits = covering_exits(p, orders)
        stops = [o for o in exits if is_stop_order(str(o.get("order_type") or ""))]
        targets = [
            o for o in exits
            if str(o.get("order_type") or "").upper() in ("LMT", "LOC", "STP LMT", "TRAIL LIMIT")
        ]
        entry["stop_orders"] = stops
        entry["target_orders"] = targets
        entry["covering_exits"] = len(exits)

        if sec_type.startswith("STK"):
            entry["protected"] = bool(stops)
            missing = []
            if not stops:
                missing.append("stop_loss")
            if not targets:
                missing.append("take_profit")
            if missing:
                entry["missing"] = missing
            if not stops:
                unprotected.append(lot_audit_label(p) or str(symbol))
        else:
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
            if qty < 0:
                entry["note"] = "short option — review risk"
                entry["flag"] = "short option — review risk"

        report.append(entry)

    try:
        from abcxauto.protect import orphaned_protection_rows

        orphans = orphaned_protection_rows(positions, orders)
    except Exception:
        logger.exception("orphaned protection scan failed")
        orphans = []

    return {
        "positions": report,
        "unprotected_symbols": unprotected,
        "orphaned_protection": orphans,
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
        self.reconciler: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        self._start_reconciler()
        logger.info(
            f"Portfolio monitor started (poll={self.cfg.monitor_poll_s}s, "
            f"review={self.cfg.monitor_review_s}s)"
        )

    def _start_reconciler(self) -> None:
        """Arm the fill-driven orphan sweep — the poll clock is too slow alone."""
        try:
            from abcxauto.protect_reconciler import ProtectionReconciler

            rec = ProtectionReconciler(self.connector)
            self.reconciler = rec if rec.start() else None
        except Exception as e:
            self.reconciler = None
            logger.warning("Protection reconciler not armed: %s", e)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        rec = self.reconciler
        self.reconciler = None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                logger.debug("reconciler stop failed", exc_info=True)

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
        """Whitelist wakes (no Grok from monitor)."""
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

        await self._sweep_orphaned_protection(snapshot)
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

    async def _sweep_orphaned_protection(self, snapshot: Dict[str, Any]) -> None:
        """Backstop for the fill-driven sweep: clean up whatever the poll finds."""
        orphans = (snapshot.get("protection") or {}).get("orphaned_protection") or []
        if not orphans:
            return
        try:
            from abcxauto.executor import cancel_orphaned_protection

            cancelled = await cancel_orphaned_protection(
                self.connector,
                positions=snapshot.get("positions") or [],
                open_orders=snapshot.get("open_orders") or [],
            )
        except Exception:
            logger.exception("orphan-protection sweep failed")
            return
        if not cancelled:
            return
        logger.warning(
            "orphan-protection: cancelled %s working exit(s) on a flat book: %s",
            len(cancelled), cancelled,
        )
        drop = set(cancelled)
        remaining = [
            o for o in (snapshot.get("open_orders") or [])
            if _order_id(o) not in drop
        ]
        snapshot["open_orders"] = remaining
        snapshot["protection"] = build_protection_report(
            snapshot.get("positions") or [], remaining
        )

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

        fills: list = []
        # Cheap idempotent fill ingest (hasattr so fakes without get_fills stay green).
        if hasattr(self.connector, "get_fills"):
            try:
                fills = await self.connector.get_fills() or []
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
        try:
            get_journal().ingest_look(snapshot)
        except Exception as e:
            logger.warning(f"Monitor look journal ingest failed: {e}")
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
