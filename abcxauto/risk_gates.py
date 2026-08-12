"""Hard pre-trade risk gates — single choke point before broker dispatch.

``record_entry()`` is called by the executor after a successful entry dispatch
(not inside ``pre_trade_check``), so rejected or failed orders do not consume
the daily trade budget.

Peak-drawdown gate (``max_peak_drawdown_pct``) rejects new entries while equity
is below the peak threshold but does **not** trip the permanent halt latch —
it self-clears when NetLiquidation recovers above the floor.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any, Optional, Tuple

from abcxauto.config import get_config
from abcxauto.proposals import MANAGEMENT_STRATEGIES, OrderProposal
from abcxauto.strategy_params import EXIT_ONLY_EXTRA, OPTION_STRATEGIES

logger = logging.getLogger(__name__)

# Always rejected when operator sets defined_risk_only (unlimited / naked risk).
_DEFINED_RISK_FORBIDDEN = frozenset({"ratio_spread", "jade_lizard"})
# Short premium naked — rejected when defined_risk_only unless action=BUY.
_DEFINED_RISK_SHORT_OK_IF_LONG = frozenset({"straddle", "strangle"})


def _journal_halt(reason: str, kind: str) -> None:
    """Record halt/resume in the trade journal (lazy import, never raises)."""
    try:
        from abcxauto.memory import get_journal

        get_journal().record_halt(reason, kind)
    except Exception:
        logger.exception("journal halt record failed")


# Bare stock orders are exit-only (schema requires closing_position=true).
_EXIT_ONLY_STRATEGIES = frozenset({
    "limit_order", "market_order", "stop_order", "stop_limit",
}) | EXIT_ONLY_EXTRA

# Only daily-loss circuit-breaker halts auto-clear at midnight. Disconnect,
# auto_panic, and manual/default "halt" kinds persist until resume().
_AUTO_RESET_HALT_KINDS = frozenset({"daily_loss"})


def is_exit_or_management(proposal: OrderProposal) -> bool:
    """True when capital/sizing gates must not block the proposal.

    Protection-placement strategies (oca) still bypass capital gates here; the
    executor separately requires a matching open position before dispatch.
    """
    if proposal.strategy in MANAGEMENT_STRATEGIES:
        return True
    if proposal.strategy == "close_option":
        return True
    if proposal.strategy in _EXIT_ONLY_STRATEGIES:
        return True
    if getattr(proposal.params, "closing_position", False):
        return True
    return False


def check_defined_risk_only(proposal: OrderProposal) -> Tuple[bool, str]:
    """Gate: when defined_risk_only, reject unlimited-risk option shapes.

    Operator control knob — not strategy taste. Returns (ok, reason).
    """
    cfg = get_config()
    if not getattr(cfg, "defined_risk_only", False):
        return True, "defined_risk_off"
    strat = str(proposal.strategy or "")
    if strat in _DEFINED_RISK_FORBIDDEN:
        return False, (
            f"defined_risk_only: {strat} has unlimited/naked risk side "
            "(operator gate)"
        )
    if strat in _DEFINED_RISK_SHORT_OK_IF_LONG:
        action = str(getattr(proposal.params, "action", "BUY") or "BUY").upper()
        if action == "SELL":
            return False, (
                f"defined_risk_only: short {strat} rejected "
                "(use action=BUY or disable defined_risk_only)"
            )
    return True, "ok"


def _market_bracket_entry_proxy(params: Any) -> Optional[float]:
    """Entry proxy for market_bracket notional / risk sizing.

    Prefer ``price_hint`` when provided. Otherwise use the *conservative*
    (least-favorable) bound of the stop/target range — never the midpoint:

    - Notional / risk both assume fill at the target-side extreme, so risk
      is ``|target - stop|`` (full range) rather than half-range.
    - For notional dollars we use ``max(stop, target)`` so a LONG fill near
      the target (high) and a SHORT fill near the stop (high) both size up.
    """
    hint = getattr(params, "price_hint", None)
    if hint is not None:
        try:
            return float(hint)
        except (TypeError, ValueError):
            return None
    stop = getattr(params, "stop_price", None)
    target = getattr(params, "target_price", None)
    if stop is None or target is None:
        return None
    try:
        return max(float(stop), float(target))
    except (TypeError, ValueError):
        return None


def _market_bracket_risk_entry(params: Any) -> Optional[float]:
    """Entry used for dollars-to-stop on market_bracket.

    With ``price_hint``, risk is ``|hint - stop|``. Without it, assume the
    least-favorable fill at the target-side extreme (``target``), so risk is
    the full ``|target - stop|`` span — never the midpoint (which made R:R
    identically 1.0).
    """
    hint = getattr(params, "price_hint", None)
    if hint is not None:
        try:
            return float(hint)
        except (TypeError, ValueError):
            return None
    target = getattr(params, "target_price", None)
    if target is None:
        return None
    try:
        return float(target)
    except (TypeError, ValueError):
        return None


def _account_float(account: dict, *keys: str) -> Optional[float]:
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


def risk_base_usd(net_liq: float, cfg: Any = None) -> float:
    """Dollar base for % gates: min(NetLiq, trading_budget) when a sleeve is set.

    A $1M paper account with a $1000 budget sizes like $1000, not $1M.
    Budget 0 (tests) falls back to NetLiq.
    """
    if cfg is None:
        cfg = get_config()
    try:
        nl = float(net_liq)
    except (TypeError, ValueError):
        nl = 0.0
    try:
        budget = float(getattr(cfg, "trading_budget_usd", 0) or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if budget > 0 and nl > 0:
        return min(nl, budget)
    if budget > 0:
        return budget
    return nl


def estimate_notional(proposal: OrderProposal) -> Optional[float]:
    """Estimate order notional for position-sizing. None if not estimable."""
    params = proposal.params
    qty = int(getattr(params, "quantity", 0) or 0)
    strategy = proposal.strategy

    entry = getattr(params, "entry_price", None)
    limit = getattr(params, "limit_price", None)
    price_hint = getattr(params, "price_hint", None)

    if strategy == "bracket" and entry is not None and qty > 0:
        return float(entry) * qty

    if strategy == "market_bracket":
        entry = _market_bracket_entry_proxy(params)
        if entry is not None and qty > 0:
            return float(entry) * qty
        return None

    # Cash-secured put: cash reserved ≈ strike × 100 × contracts
    if strategy == "cash_secured_put":
        try:
            strike = float(getattr(params, "strike", 0) or 0)
            contracts = int(
                getattr(params, "contracts", None)
                or getattr(params, "quantity", 0)
                or 0
            )
        except (TypeError, ValueError):
            return None
        if strike > 0 and contracts > 0:
            return strike * 100.0 * contracts
        return None

    # Option premium notional when limit_price present (multiplier 100)
    if strategy in OPTION_STRATEGIES and limit is not None and qty > 0:
        try:
            return abs(float(limit)) * 100.0 * qty
        except (TypeError, ValueError):
            return None

    if limit is not None and qty > 0:
        return float(limit) * qty
    if entry is not None and qty > 0:
        return float(entry) * qty
    if price_hint is not None and qty > 0:
        return float(price_hint) * qty
    return None


def estimate_bracket_risk_dollars(proposal: OrderProposal) -> Optional[float]:
    """Dollars risked to stop for bracket / market_bracket. None if not estimable."""
    if proposal.strategy not in ("bracket", "market_bracket"):
        return None
    params = proposal.params
    qty = int(getattr(params, "quantity", 0) or 0)
    if qty <= 0:
        return None
    stop = float(params.stop_price)
    if proposal.strategy == "bracket":
        entry = float(params.entry_price)
    else:
        entry = _market_bracket_risk_entry(params)
        if entry is None:
            return None
    return qty * abs(float(entry) - stop)


class RiskGate:
    """Thread-safe pre-trade risk checks + kill-switch latch."""

    def __init__(self, *, auto_reset_on_new_day: bool = True) -> None:
        self._lock = threading.Lock()
        self._halted = False
        self._halt_reason = ""
        self._halt_kind: str = ""
        self._halt_date: Optional[date] = None
        self.auto_reset_on_new_day = auto_reset_on_new_day
        self._trade_date: Optional[str] = None
        self._daily_trades = 0
        self._peak_equity: Optional[float] = None

    # ------------------------------------------------------------------
    # Halt latch
    # ------------------------------------------------------------------

    def halt(self, reason: str, *, kind: str = "halt") -> None:
        with self._lock:
            self._halted = True
            self._halt_reason = reason or "halted"
            self._halt_kind = kind or "halt"
            self._halt_date = date.today()
            logger.critical(
                f"RISK GATE HALTED ({self._halt_kind}): {self._halt_reason}"
            )
        _journal_halt(self._halt_reason, self._halt_kind)

    def resume(self) -> None:
        with self._lock:
            self._halted = False
            self._halt_reason = ""
            self._halt_kind = ""
            self._halt_date = None
            logger.warning("RISK GATE RESUMED")
        _journal_halt("manual resume", "resume")

    @property
    def is_halted(self) -> bool:
        with self._lock:
            self._maybe_auto_reset_unlocked()
            return self._halted

    @property
    def halt_reason(self) -> str:
        with self._lock:
            self._maybe_auto_reset_unlocked()
            return self._halt_reason

    @property
    def halt_kind(self) -> str:
        with self._lock:
            self._maybe_auto_reset_unlocked()
            return self._halt_kind

    def _maybe_auto_reset_unlocked(self) -> None:
        if not self._halted or not self.auto_reset_on_new_day:
            return
        if self._halt_date is None or self._halt_date >= date.today():
            return
        if self._halt_kind not in _AUTO_RESET_HALT_KINDS:
            return
        logger.info(
            f"Risk gate auto-reset on new day (kind={self._halt_kind}, "
            f"was halted {self._halt_date}: {self._halt_reason})"
        )
        self._halted = False
        self._halt_reason = ""
        self._halt_kind = ""
        self._halt_date = None

    # ------------------------------------------------------------------
    # Peak equity (drawdown gate — self-clearing, no halt latch)
    # ------------------------------------------------------------------

    def update_equity(self, net_liq: float) -> None:
        """Track peak NetLiquidation for the peak-drawdown gate.

        Called by the monitor each poll and inside ``pre_trade_check``.
        Does not trip the halt latch.
        """
        try:
            value = float(net_liq)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        with self._lock:
            if self._peak_equity is None or value > self._peak_equity:
                self._peak_equity = value

    @property
    def peak_equity(self) -> Optional[float]:
        with self._lock:
            return self._peak_equity

    # ------------------------------------------------------------------
    # Daily trade counter
    # ------------------------------------------------------------------

    def record_entry(self) -> None:
        """Increment the daily entry counter after a successful dispatch."""
        today = date.today().isoformat()
        with self._lock:
            if self._trade_date != today:
                self._trade_date = today
                self._daily_trades = 0
            self._daily_trades += 1

    def daily_trade_count(self) -> int:
        today = date.today().isoformat()
        with self._lock:
            if self._trade_date != today:
                return 0
            return self._daily_trades

    def reset_daily_trades(self) -> None:
        with self._lock:
            self._trade_date = date.today().isoformat()
            self._daily_trades = 0

    # ------------------------------------------------------------------
    # Pre-trade check
    # ------------------------------------------------------------------

    async def pre_trade_check(
        self, proposal: OrderProposal, connector: Any
    ) -> Tuple[bool, str]:
        """Return (ok, reason). Exits/management always pass."""
        if is_exit_or_management(proposal):
            return True, "exit/management bypass"

        cfg = get_config()
        if not cfg.risk_gates_enabled:
            return True, "risk gates disabled"

        if self.is_halted:
            return False, f"Trading halted: {self.halt_reason}"

        ok_dr, why_dr = check_defined_risk_only(proposal)
        if not ok_dr:
            return False, why_dr

        try:
            account = await connector.get_account_summary()
        except Exception as e:
            return False, f"Risk gate fail-closed: cannot read account summary ({e})"

        if not isinstance(account, dict) or account.get("error"):
            err = account.get("error") if isinstance(account, dict) else "invalid account"
            return False, f"Risk gate fail-closed: cannot read account summary ({err})"

        net_liq = _account_float(account, "netliquidation", "NetLiquidation")
        daily_pnl = _account_float(account, "dailypnl", "DailyPnL")
        if net_liq is None or net_liq <= 0:
            return False, "Risk gate fail-closed: NetLiquidation unavailable or non-positive"
        if daily_pnl is None:
            daily_pnl = 0.0

        self.update_equity(net_liq)
        sleeve = risk_base_usd(net_liq, cfg)

        if cfg.daily_loss_limit_pct > 0:
            limit = -(cfg.daily_loss_limit_pct / 100.0) * sleeve
            if daily_pnl <= limit:
                reason = (
                    f"Daily loss circuit breaker: daily PnL {daily_pnl:.2f} <= "
                    f"limit {limit:.2f} ({cfg.daily_loss_limit_pct}% of sleeve "
                    f"{sleeve:.2f}; NL {net_liq:.2f})"
                )
                self.halt(reason, kind="daily_loss")
                return False, reason

        if cfg.max_peak_drawdown_pct > 0:
            peak = self.peak_equity
            if peak is not None and peak > 0:
                try:
                    budget = float(getattr(cfg, "trading_budget_usd", 0) or 0)
                except (TypeError, ValueError):
                    budget = 0.0
                if budget > 0:
                    max_drop = (cfg.max_peak_drawdown_pct / 100.0) * budget
                    drop = peak - net_liq
                    if drop >= max_drop:
                        return False, (
                            f"Peak drawdown gate: drop {drop:.2f} >= "
                            f"{cfg.max_peak_drawdown_pct}% of ${budget:.0f} sleeve "
                            f"(max drop {max_drop:.2f}). Self-clears when equity recovers."
                        )
                else:
                    floor = peak * (1.0 - cfg.max_peak_drawdown_pct / 100.0)
                    if net_liq <= floor:
                        return False, (
                            f"Peak drawdown gate: NetLiq {net_liq:.2f} <= "
                            f"{cfg.max_peak_drawdown_pct}% below peak {peak:.2f} "
                            f"(floor {floor:.2f}). Self-clears when equity recovers."
                        )

        if cfg.cash_only:
            direction = getattr(proposal.params, "direction", None)
            if (
                proposal.strategy in ("bracket", "market_bracket")
                and direction == "SHORT"
            ):
                return False, (
                    "Cash-only mode: SHORT stock brackets are rejected "
                    "(no short selling). Set ABCXAUTO_CASH_ONLY=false to allow."
                )
            cash = _account_float(
                account, "TotalCashValue", "totalcashvalue", "AvailableFunds", "availablefunds"
            )
            if cash is None:
                return False, (
                    "Risk gate fail-closed: cash-only mode requires TotalCashValue "
                    "(or AvailableFunds) in account summary"
                )
            notional = estimate_notional(proposal)
            if notional is None:
                if proposal.strategy in OPTION_STRATEGIES:
                    # Option premium often unknown without limit; broker enforces margin.
                    pass
                else:
                    return False, (
                        "Risk gate: cannot estimate order notional for cash-only sizing "
                        "(provide entry_price, limit_price, or price_hint)"
                    )
            elif notional > cash:
                return False, (
                    f"Cash-only: order notional {notional:.2f} exceeds available cash "
                    f"{cash:.2f}"
                )

        if cfg.max_position_pct > 0:
            notional = estimate_notional(proposal)
            if notional is None:
                if proposal.strategy in OPTION_STRATEGIES:
                    pass
                else:
                    return False, (
                        "Risk gate: cannot estimate order notional for position sizing "
                        "(provide entry_price, limit_price, or price_hint)"
                    )
            else:
                max_notional = (cfg.max_position_pct / 100.0) * sleeve
                if notional > max_notional:
                    return False, (
                        f"Position size {notional:.2f} exceeds max "
                        f"{cfg.max_position_pct}% of sleeve {sleeve:.2f} "
                        f"(NL {net_liq:.2f})"
                    )

        if cfg.max_risk_per_trade_pct > 0 and proposal.strategy in (
            "bracket",
            "market_bracket",
        ):
            risked = estimate_bracket_risk_dollars(proposal)
            if risked is None:
                return False, (
                    "Risk gate: cannot estimate dollars risked to stop for risk-per-trade cap"
                )
            max_risk = (cfg.max_risk_per_trade_pct / 100.0) * sleeve
            if risked > max_risk:
                return False, (
                    f"Risk-per-trade {risked:.2f} exceeds max "
                    f"{cfg.max_risk_per_trade_pct}% of sleeve {sleeve:.2f} "
                    f"(NL {net_liq:.2f})"
                )

        if cfg.max_open_positions > 0:
            try:
                positions = await connector.get_positions()
            except Exception as e:
                return False, f"Risk gate fail-closed: cannot read positions ({e})"
            open_count = 0
            for p in positions or []:
                try:
                    qty = float(p.get("quantity", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0
                if qty != 0:
                    open_count += 1
            if open_count >= cfg.max_open_positions:
                return False, (
                    f"Max open positions reached ({open_count} >= {cfg.max_open_positions})"
                )

        return True, "ok"


_gate: Optional[RiskGate] = None
_gate_lock = threading.Lock()


def get_risk_gate() -> RiskGate:
    """Module-level singleton accessor (thread-safe lazy init)."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = RiskGate()
        return _gate


def reset_risk_gate() -> RiskGate:
    """Replace the singleton (for tests)."""
    global _gate
    with _gate_lock:
        _gate = RiskGate()
        return _gate
