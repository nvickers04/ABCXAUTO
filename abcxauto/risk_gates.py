"""Hard pre-trade risk gates — single choke point before broker dispatch.

``record_entry()`` is called by the executor after a successful entry dispatch
(not inside ``pre_trade_check``), so rejected or failed orders do not consume
the daily trade budget.

Peak-drawdown gate (``max_peak_drawdown_pct``) rejects new entries while equity
is below the peak threshold but does **not** trip the permanent halt latch —
it self-clears when NetLiquidation recovers above the floor.

Concentration gate (``max_symbol_concentration_pct``) is the only size gate that
reads the book instead of just the ticket: ``max_position_pct`` sees one order at
a time, so N orders in one name could stack past it. It sums every lot in the
proposed underlying, stock and options together, and adds the new notional.

Arena gate (``max_arena_concentration_pct``) is the cheap complex cap: one
sector/theme/cap bucket from arenas we already scan, as a % of NL. Per-name
cannot see NVDA+SMCI+ARM+AVGO as four names in one bet. Scan sorts are not
buckets. This check lives on send even when paper ``risk_gates_enabled`` is
off — same class as mode_size, not the floors-gated per-name helper.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import date
from typing import Any, Optional, Tuple

from abcxauto.config import get_config
from abcxauto.proposals import MANAGEMENT_STRATEGIES, OrderProposal
from abcxauto.strategy_params import EXIT_ONLY_EXTRA, OPTION_STRATEGIES
from abcxauto.universe import arenas_for_symbol, is_bucket_arena

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
    if getattr(proposal.params, "closing_position", False):
        return True, "closing"
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


def _parse_account_number(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _account_number_state(
    account: dict, *keys: str
) -> Tuple[str, Optional[float]]:
    """Read a USD tag. ``ok`` / ``missing`` / ``unreadable`` (present but not finite)."""
    saw_present = False
    for key in keys:
        for candidate in (key, key.lower()):
            if candidate not in account:
                continue
            raw = account[candidate]
            if raw is None:
                continue
            saw_present = True
            parsed = _parse_account_number(raw)
            if parsed is not None:
                return "ok", parsed
    if saw_present:
        return "unreadable", None
    return "missing", None


def _account_float(account: dict, *keys: str) -> Optional[float]:
    _state, value = _account_number_state(account, *keys)
    return value


def risk_base_usd(net_liq: float, cfg: Any = None) -> float:
    """Dollar base for % gates: full NetLiq. Same % at $1k, $100k, or $1M."""
    try:
        return max(0.0, float(net_liq))
    except (TypeError, ValueError):
        return 0.0


# TWS 7496 / Gateway 4001 — live socket family. Paper is 7497 / 4002.
_LIVE_IBKR_PORTS = frozenset({7496, 4001})


def sizing_floors_active(cfg: Any = None) -> bool:
    """True when % size floors apply. Live always ON; paper follows clerk flag.

    Live is ``trading_mode==live`` or a live-family port (TWS 7496 / Gateway
    4001), or any config that already reports ``is_paper`` as false. A live
    socket with ``TRADING_MODE`` still paper must not skip % floors. This does
    not enable live send — only the size/loss breaker.
    """
    c = cfg if cfg is not None else get_config()
    mode = str(getattr(c, "trading_mode", "paper") or "paper").strip().lower()
    if mode == "live":
        return True
    if getattr(c, "is_paper", None) is False:
        return True
    try:
        port = int(getattr(c, "ibkr_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    if port in _LIVE_IBKR_PORTS:
        return True
    return bool(getattr(c, "sizing_floors", False))


def _pct_of_nl(dollars: float, book: float) -> float:
    if book <= 0:
        return 0.0
    return round(100.0 * float(dollars) / float(book), 4)


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


def _lot_names(position: dict) -> set[str]:
    """Tickers that identify one underlying on a lot.

    Live IBKR options use ``contract.symbol`` = underlying. Some snapshots also
    carry ``underlying`` next to an OCC ``symbol`` — both must count as one name.
    """
    names: set[str] = set()
    for key in ("underlying", "underSymbol", "symbol", "ticker"):
        val = str(position.get(key) or "").strip().upper()
        if val:
            names.add(val)
    return names


def _lot_market_value(position: dict) -> Optional[float]:
    """Priced mark for a lot. None when the field is present but unreadable."""
    raw: Any = None
    if "marketValue" in position and position["marketValue"] is not None:
        raw = position["marketValue"]
    elif "market_value" in position and position["market_value"] is not None:
        raw = position["market_value"]
    else:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return abs(value)


def symbol_exposure_usd(positions: Any, symbol: str) -> Optional[float]:
    """Market value already held in one underlying, summed across lots.

    Stock and its options aggregate on purpose: SPY shares plus SPY calls are
    one bet, not two. Option ``marketValue`` is premium, which understates
    delta — the same basis the portfolio exposure fact already reports, and a
    cap where ``max_position_pct`` alone left none.

    Returns ``None`` when a matching lot cannot be priced (NaN / non-finite
    mark). Callers must fail-closed — ``nan > cap`` is False in Python, which
    would let a second ticket in the same name pass.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0.0
    total = 0.0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if sym not in _lot_names(p):
            continue
        try:
            qty = float(p.get("quantity") or p.get("position") or 0)
        except (TypeError, ValueError):
            continue
        if abs(qty) < 1e-9:
            continue
        marked = _lot_market_value(p)
        if marked is None:
            return None
        total += marked
    return total


def arena_exposure_usd(
    positions: Any,
    arena_id: str,
    *,
    membership: list | None = None,
) -> Optional[float]:
    """Market value already held in one sector/theme/cap arena.

    Every lot whose underlying belongs to that arena counts — different
    tickers, stock and options. Scan-sort membership is ignored. Returns
    ``None`` when a matching lot cannot be priced (fail-closed).
    """
    want = str(arena_id or "").strip()
    if not want or not is_bucket_arena(want):
        return 0.0
    total = 0.0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        names = _lot_names(p)
        if not names:
            continue
        try:
            qty = float(p.get("quantity") or p.get("position") or 0)
        except (TypeError, ValueError):
            continue
        if abs(qty) < 1e-9:
            continue
        in_arena = False
        for name in names:
            if want in arenas_for_symbol(name, membership=membership):
                in_arena = True
                break
        if not in_arena:
            continue
        marked = _lot_market_value(p)
        if marked is None:
            return None
        total += marked
    return total


def arena_concentration_error(
    proposal: OrderProposal,
    positions: Any,
    net_liq: float,
    *,
    membership: list | None = None,
    cap_pct: float | None = None,
) -> str:
    """Refuse when this ticket would push any of its arenas over the bucket %."""
    if is_exit_or_management(proposal):
        return ""
    if cap_pct is None:
        cap_pct = float(
            getattr(get_config(), "max_arena_concentration_pct", 0) or 0
        )
    try:
        cap = float(cap_pct)
    except (TypeError, ValueError):
        cap = 0.0
    if not math.isfinite(cap) or cap <= 0:
        return ""
    symbol = str(getattr(proposal.params, "symbol", "") or "").strip()
    arenas = [
        a
        for a in sorted(arenas_for_symbol(symbol, membership=membership))
        if is_bucket_arena(a)
    ]
    if not arenas:
        return ""
    notional = estimate_notional(proposal)
    if notional is None:
        return "size_unknown_notional"
    book = risk_base_usd(net_liq)
    for arena in arenas:
        held = arena_exposure_usd(
            positions, arena, membership=membership
        )
        if held is None:
            return "size_arena_concentration unknown"
        after = held + float(notional)
        after_pct = _pct_of_nl(after, book)
        if not math.isfinite(after_pct) or after_pct > cap:
            return (
                f"size_arena_concentration {after_pct} > {cap} ({arena})"
            )
    return ""


async def check_arena_concentration(
    proposal: OrderProposal, connector: Any
) -> Tuple[bool, str]:
    """Always-on send check. Exits pass. Cap 0 is off."""
    if is_exit_or_management(proposal):
        return True, "exit"
    cfg = get_config()
    try:
        cap = float(getattr(cfg, "max_arena_concentration_pct", 0) or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if not math.isfinite(cap) or cap <= 0:
        return True, "off"
    try:
        account = await connector.get_account_summary()
    except Exception as e:
        return False, f"Risk gate fail-closed: cannot read account summary ({e})"
    if not isinstance(account, dict) or account.get("error"):
        err = account.get("error") if isinstance(account, dict) else "invalid account"
        return False, f"Risk gate fail-closed: cannot read account summary ({err})"
    nl_state, net_liq = _account_number_state(
        account, "netliquidation", "NetLiquidation"
    )
    if nl_state != "ok" or net_liq is None or net_liq <= 0:
        return False, "Risk gate fail-closed: NetLiquidation unavailable or non-positive"
    try:
        positions = await connector.get_positions()
    except Exception as e:
        return False, f"Risk gate fail-closed: cannot read positions ({e})"
    if isinstance(positions, dict) and positions.get("error"):
        return False, (
            "Risk gate fail-closed: cannot read positions "
            f"({positions.get('error')})"
        )
    if not isinstance(positions, list):
        return False, "Risk gate fail-closed: cannot read positions"
    note = arena_concentration_error(proposal, positions, float(net_liq))
    if note:
        return False, note
    return True, "ok"


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
        self._riskless_combo_202 = False

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
    # IBKR [202] riskless-combo latch (session)
    # ------------------------------------------------------------------

    def note_riskless_combo_202(self) -> None:
        """IBKR cancelled a riskless/guaranteed-loss BAG with [202]."""
        with self._lock:
            self._riskless_combo_202 = True

    @property
    def riskless_combo_202(self) -> bool:
        with self._lock:
            return self._riskless_combo_202

    def sync_riskless_combo_202(self, orders: Any) -> bool:
        """Keep the [202] latch only while a working BAG still occupies the cap."""
        from abcxauto.riskless_combo import working_bag_keeps_202_latch

        with self._lock:
            if self._riskless_combo_202 and not working_bag_keeps_202_latch(orders):
                self._riskless_combo_202 = False
            return self._riskless_combo_202

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

        nl_state, net_liq = _account_number_state(
            account, "netliquidation", "NetLiquidation"
        )
        pnl_state, daily_pnl = _account_number_state(account, "dailypnl", "DailyPnL")
        if nl_state != "ok" or net_liq is None or net_liq <= 0:
            return False, "Risk gate fail-closed: NetLiquidation unavailable or non-positive"
        # Missing DailyPnL is flat (IBKR often omits it early session). A
        # present but non-finite tag is unknown — fail-closed when the
        # breaker is armed, and never treat NaN as "no loss".
        floors_on = sizing_floors_active(cfg)
        if (
            pnl_state == "unreadable"
            and floors_on
            and cfg.daily_loss_limit_pct > 0
        ):
            return False, "Risk gate fail-closed: DailyPnL unreadable"
        if daily_pnl is None:
            daily_pnl = 0.0

        self.update_equity(net_liq)
        book = risk_base_usd(net_liq, cfg)

        # Fail-closed: option tickets must carry a price (no sizing on a lie).
        if proposal.strategy in OPTION_STRATEGIES:
            opt_notional = estimate_notional(proposal)
            if opt_notional is None:
                return False, "size_unknown_notional"

        if floors_on and cfg.daily_loss_limit_pct > 0:
            limit = -(cfg.daily_loss_limit_pct / 100.0) * book
            if daily_pnl <= limit:
                day_pct = _pct_of_nl(daily_pnl, book)
                reason = (
                    f"daily_loss {day_pct} <= -{cfg.daily_loss_limit_pct}"
                )
                self.halt(reason, kind="daily_loss")
                return False, reason

        if floors_on and cfg.max_peak_drawdown_pct > 0:
            peak = self.peak_equity
            if peak is not None and peak > 0:
                floor = peak * (1.0 - cfg.max_peak_drawdown_pct / 100.0)
                if net_liq <= floor:
                    dd_pct = round(100.0 * (1.0 - float(net_liq) / float(peak)), 4)
                    return False, (
                        f"peak_drawdown {dd_pct} > {cfg.max_peak_drawdown_pct}"
                    )

        # cash_only structural: no short stock (always). % cash check only when floors ON.
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
            if floors_on:
                cash = _account_float(
                    account,
                    "TotalCashValue",
                    "totalcashvalue",
                    "AvailableFunds",
                    "availablefunds",
                )
                if cash is None:
                    return False, (
                        "Risk gate fail-closed: cash-only mode requires TotalCashValue "
                        "(or AvailableFunds) in account summary"
                    )
                notional = estimate_notional(proposal)
                if notional is None:
                    return False, "size_unknown_notional"
                if notional > cash:
                    return False, (
                        f"size_cash {_pct_of_nl(notional, book)} > "
                        f"{_pct_of_nl(cash, book)}"
                    )

        if floors_on and cfg.max_position_pct > 0:
            notional = estimate_notional(proposal)
            if notional is None:
                return False, "size_unknown_notional"
            notional_pct = _pct_of_nl(notional, book)
            if notional_pct > cfg.max_position_pct:
                return False, (
                    f"size_max_position {notional_pct} > {cfg.max_position_pct}"
                )

        if floors_on and cfg.max_risk_per_trade_pct > 0:
            if proposal.strategy in ("bracket", "market_bracket"):
                risked = estimate_bracket_risk_dollars(proposal)
                if risked is None:
                    return False, "size_unknown_notional"
                risked_pct = _pct_of_nl(risked, book)
                if risked_pct > cfg.max_risk_per_trade_pct:
                    return False, (
                        f"size_risk_per_trade {risked_pct} > "
                        f"{cfg.max_risk_per_trade_pct}"
                    )
            elif proposal.strategy in OPTION_STRATEGIES:
                notional = estimate_notional(proposal)
                if notional is None:
                    return False, "size_unknown_notional"
                notional_pct = _pct_of_nl(notional, book)
                if notional_pct > cfg.max_risk_per_trade_pct:
                    return False, (
                        f"size_risk_per_trade {notional_pct} > "
                        f"{cfg.max_risk_per_trade_pct}"
                    )

        if floors_on and cfg.max_option_premium_pct > 0 and proposal.strategy in OPTION_STRATEGIES:
            notional = estimate_notional(proposal)
            if notional is None:
                return False, "size_unknown_notional"
            notional_pct = _pct_of_nl(notional, book)
            if notional_pct > cfg.max_option_premium_pct:
                return False, (
                    f"size_option_premium {notional_pct} > "
                    f"{cfg.max_option_premium_pct}"
                )

        concentration_pct = (
            cfg.max_symbol_concentration_pct if floors_on else 0.0
        )
        positions: Any = []
        if cfg.max_open_positions > 0 or concentration_pct > 0:
            try:
                positions = await connector.get_positions()
            except Exception as e:
                return False, f"Risk gate fail-closed: cannot read positions ({e})"
            if isinstance(positions, dict) and positions.get("error"):
                return False, (
                    "Risk gate fail-closed: cannot read positions "
                    f"({positions.get('error')})"
                )
            if not isinstance(positions, list):
                return False, "Risk gate fail-closed: cannot read positions"

        if concentration_pct > 0:
            notional = estimate_notional(proposal)
            if notional is None:
                return False, "size_unknown_notional"
            held = symbol_exposure_usd(
                positions, getattr(proposal.params, "symbol", "")
            )
            if held is None:
                return False, "size_symbol_concentration unknown"
            after = held + float(notional)
            after_pct = _pct_of_nl(after, book)
            if not math.isfinite(after_pct) or after_pct > concentration_pct:
                return False, (
                    f"size_symbol_concentration {after_pct} > {concentration_pct}"
                )

        # Slot refuse: this method already returned when gates are off.
        # Live / gates-on still fire. Working entries reserve in capacity_fact.
        if cfg.max_open_positions > 0:
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
