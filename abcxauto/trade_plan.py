"""ActiveTradePlan — durable open-trade lifecycle across cycles.

Broker book is source of truth: reconcile_open_risk keeps/rebuilds the plan
across Stop/Start; confirmed-flat (not a single empty snap) closes it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO_ROOT / "active_trade_plan.json"
_DEFAULT_FLAT_STREAK_PATH = _REPO_ROOT / "flat_book_streak.json"
_CONFIRMED_FLAT_N = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class ActiveTradePlan:
    symbol: str
    direction: str  # LONG | SHORT
    thesis: str = ""
    invalidation: str = ""
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    quantity: float | None = None
    max_hold_cycles: int = 20
    cycles_open: int = 0
    management: str = "move stop to BE after +0.5R; trail thereafter"
    status: str = "open"  # open | closed
    opened_at: str = field(default_factory=_utc_now)
    closed_at: str | None = None
    close_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ActiveTradePlan":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


def _path() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_TRADE_PLAN_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def load_trade_plan(path: Path | None = None) -> Optional[ActiveTradePlan]:
    p = path or _path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("status") == "closed":
            return None
        return ActiveTradePlan.from_dict(raw)
    except Exception:
        logger.exception("load_trade_plan failed")
        return None


def save_trade_plan(plan: ActiveTradePlan, path: Path | None = None) -> Path:
    p = path or _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    return p


def clear_trade_plan(path: Path | None = None) -> None:
    p = path or _path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        logger.exception("clear_trade_plan failed")


def close_trade_plan(reason: str = "", path: Path | None = None) -> None:
    plan = load_trade_plan(path)
    if not plan:
        clear_trade_plan(path)
        return
    plan.status = "closed"
    plan.closed_at = _utc_now()
    plan.close_reason = reason or "closed"
    # Persist closed snapshot then clear active
    p = path or _path()
    try:
        archive = p.with_name("last_closed_trade_plan.json")
        archive.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    clear_trade_plan(path)


def bump_plan_cycle(path: Path | None = None) -> Optional[ActiveTradePlan]:
    plan = load_trade_plan(path)
    if not plan:
        return None
    plan.cycles_open = int(plan.cycles_open or 0) + 1
    if plan.max_hold_cycles and plan.cycles_open >= int(plan.max_hold_cycles):
        close_trade_plan("time_stop", path)
        return None
    save_trade_plan(plan, path)
    return plan


def plan_from_hunt_action(act: dict, thesis: str = "") -> Optional[ActiveTradePlan]:
    """Build a plan from a successful hunt bracket params."""
    params = (act or {}).get("params") or {}
    strat = str((act or {}).get("strategy") or (act or {}).get("action") or "").lower()
    if strat not in ("bracket", "market_bracket"):
        return None
    symbol = str(params.get("symbol") or "").upper()
    direction = str(params.get("direction") or "LONG").upper()
    if not symbol:
        return None
    try:
        stop = float(params["stop_price"]) if params.get("stop_price") is not None else None
    except (TypeError, ValueError):
        stop = None
    try:
        target = float(params["target_price"]) if params.get("target_price") is not None else None
    except (TypeError, ValueError):
        target = None
    try:
        qty = float(params["quantity"]) if params.get("quantity") is not None else None
    except (TypeError, ValueError):
        qty = None
    try:
        entry = float(params["entry_price"]) if params.get("entry_price") is not None else None
    except (TypeError, ValueError):
        entry = None
    return ActiveTradePlan(
        symbol=symbol,
        direction=direction if direction in ("LONG", "SHORT") else "LONG",
        thesis=(thesis or str((act or {}).get("rationale") or ""))[:500],
        invalidation=f"stop {stop}" if stop else "stop hit",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=qty,
    )


def _flat_streak_path() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_FLAT_STREAK_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_FLAT_STREAK_PATH


def _f(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _stk_rows(positions: list[dict] | None) -> list[dict[str, Any]]:
    """Primary equity risk rows (STK/ETF), largest |qty| first."""
    rows: list[dict[str, Any]] = []
    for p in positions or []:
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        if sec not in ("STK", "ETF", ""):
            continue
        qty = _f(p.get("quantity") if p.get("quantity") is not None else p.get("position"))
        if qty is None or qty == 0:
            continue
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        mv = abs(_f(p.get("marketValue") or p.get("market_value")) or 0.0)
        rows.append({"symbol": sym, "quantity": qty, "market_value": mv, "raw": p})
    rows.sort(key=lambda r: (r["market_value"], abs(r["quantity"])), reverse=True)
    return rows


def _order_symbol(o: dict) -> str:
    return str(o.get("symbol") or "").upper()


def _order_type(o: dict) -> str:
    return str(o.get("order_type") or o.get("orderType") or "").upper()


def _order_action(o: dict) -> str:
    return str(o.get("action") or o.get("side") or "").upper()


def _exits_from_orders(
    open_orders: list[dict] | None, symbol: str, direction: str
) -> tuple[float | None, float | None]:
    """Best-effort stop/target from working protective exits (not entry limits)."""
    stop: float | None = None
    targets: list[float] = []
    want_exit = "SELL" if direction == "LONG" else "BUY"
    for o in open_orders or []:
        if _order_symbol(o) != symbol:
            continue
        action = _order_action(o)
        if action and action != want_exit:
            continue
        otype = _order_type(o)
        aux = _f(o.get("aux_price") or o.get("auxPrice") or o.get("stop_price"))
        lmt = _f(o.get("lmt_price") or o.get("lmtPrice") or o.get("limit_price"))
        if any(k in otype for k in ("STP", "TRAIL", "STOP")):
            if aux is not None:
                stop = aux
            elif lmt is not None and "STP" in otype:
                stop = lmt
        elif otype in ("LMT", "LIMIT", "LOC") or (
            "LMT" in otype and "STP" not in otype
        ):
            if lmt is not None:
                targets.append(lmt)
    target: float | None = None
    if targets:
        # Prefer the take-profit farthest in the profit direction (skip scale crumbs).
        target = max(targets) if direction == "LONG" else min(targets)
    return stop, target


def stk_qty_for_symbol(positions: list[dict] | None, symbol: str) -> float:
    """Signed STK/ETF quantity for symbol (0 if flat / missing)."""
    sym = (symbol or "").upper()
    if not sym:
        return 0.0
    total = 0.0
    for r in _stk_rows(positions):
        if r["symbol"] == sym:
            total += float(r["quantity"])
    return total


def book_has_risk(positions: list[dict] | None) -> bool:
    """True if any non-zero position exists (STK/ETF/OPT — blocks false-flat hunt)."""
    for p in positions or []:
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if qty != 0:
            return True
    return False


def _avg_cost(pos: dict) -> float | None:
    for key in ("avgCost", "avg_cost", "averageCost", "average_cost"):
        v = _f(pos.get(key))
        if v is not None:
            return v
    return None


def reconcile_open_risk(
    positions: list[dict] | None,
    open_orders: list[dict] | None = None,
    existing_plan: ActiveTradePlan | None = None,
    *,
    thesis: str = "",
) -> Optional[ActiveTradePlan]:
    """Keep/rebuild ActiveTradePlan from broker book (never clears on empty).

    Returns an open plan when STK risk exists; None when book has no STK
    (caller decides confirmed-flat close separately).
    """
    plan = existing_plan if existing_plan is not None else load_trade_plan()
    rows = _stk_rows(positions)
    if not rows:
        return None

    by_sym = {r["symbol"]: r for r in rows}
    if plan and plan.symbol in by_sym:
        row = by_sym[plan.symbol]
        qty = abs(float(row["quantity"]))
        direction = "LONG" if float(row["quantity"]) > 0 else "SHORT"
        plan.quantity = qty
        plan.direction = direction
        stop, target = _exits_from_orders(open_orders, plan.symbol, direction)
        if stop is not None:
            plan.stop_price = stop
        if target is not None:
            plan.target_price = target
        if plan.entry_price is None:
            plan.entry_price = _avg_cost(row["raw"])
        if thesis and not (plan.thesis or "").strip():
            plan.thesis = thesis[:500]
        if plan.stop_price is not None:
            plan.invalidation = f"stop {plan.stop_price}"
        plan.status = "open"
        return plan

    # Plan symbol gone but other STK remains — archive then rebuild primary.
    if plan and plan.symbol not in by_sym:
        close_trade_plan("symbol_left_book", path=None)

    # Rebuild from primary STK risk
    row = rows[0]
    symbol = row["symbol"]
    qty_signed = float(row["quantity"])
    direction = "LONG" if qty_signed > 0 else "SHORT"
    stop, target = _exits_from_orders(open_orders, symbol, direction)
    return ActiveTradePlan(
        symbol=symbol,
        direction=direction,
        thesis=(thesis or f"Rehydrated open risk {symbol} {direction}")[:500],
        invalidation=f"stop {stop}" if stop is not None else "stop hit / thesis invalid",
        entry_price=_avg_cost(row["raw"]),
        stop_price=stop,
        target_price=target,
        quantity=abs(qty_signed),
        cycles_open=0,
        status="open",
    )


def _flat_streak_state() -> dict[str, Any]:
    p = _flat_streak_path()
    if not p.is_file():
        return {"empty_count": 0, "ever_held": False}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"empty_count": 0, "ever_held": False}
        return {
            "empty_count": max(0, int(raw.get("empty_count") or 0)),
            "ever_held": bool(raw.get("ever_held")),
        }
    except Exception:
        return {"empty_count": 0, "ever_held": False}


def _save_flat_streak_state(empty_count: int, ever_held: bool) -> None:
    p = _flat_streak_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "empty_count": int(empty_count),
                    "ever_held": bool(ever_held),
                    "ts": _utc_now(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        logger.exception("save flat streak state failed")


def load_flat_streak() -> int:
    return int(_flat_streak_state().get("empty_count") or 0)


def save_flat_streak(count: int) -> None:
    st = _flat_streak_state()
    _save_flat_streak_state(count, bool(st.get("ever_held")))


def reset_flat_streak() -> None:
    st = _flat_streak_state()
    _save_flat_streak_state(0, bool(st.get("ever_held")))


def maybe_close_on_confirmed_flat(
    positions: list[dict] | None,
    *,
    needed: int = _CONFIRMED_FLAT_N,
    path: Path | None = None,
) -> bool:
    """Close plan only after held→flat transition + ``needed`` empty STK snaps.

    Ignores empty books that never saw STK (startup / false-empty before first fill).
    Does not close while any option (or other) risk remains in ``positions``.
    """
    rows = _stk_rows(positions)
    st = _flat_streak_state()
    if rows:
        _save_flat_streak_state(0, True)
        return False
    # STK gone but options/other remain — close equity plan (stock leg flat).
    if book_has_risk(positions):
        if load_trade_plan(path):
            close_trade_plan("stk_flat_options_remain", path)
            _save_flat_streak_state(0, False)
            return True
        return False
    if not st.get("ever_held") and not load_trade_plan(path):
        # Never held in this streak file and no plan — nothing to close.
        _save_flat_streak_state(0, False)
        return False
    if not st.get("ever_held") and load_trade_plan(path):
        # Plan on disk but we never marked held this process — still require
        # consecutive empties, and set ever_held so we don't wipe on first blip.
        st["ever_held"] = True
    n = int(st.get("empty_count") or 0) + 1
    _save_flat_streak_state(n, True)
    if n < max(1, int(needed)):
        return False
    if load_trade_plan(path):
        close_trade_plan("confirmed_flat", path)
        _save_flat_streak_state(0, False)
        return True
    _save_flat_streak_state(0, False)
    return False


def sync_open_risk(
    positions: list[dict] | None,
    open_orders: list[dict] | None = None,
    *,
    thesis: str = "",
    bump: bool = False,
    allow_flat_close: bool = True,
    path: Path | None = None,
) -> Optional[ActiveTradePlan]:
    """Reconcile + persist plan; confirmed-flat close when book empty.

    When ``bump`` and a plan remains for a held symbol, increment cycles_open.
    Set ``allow_flat_close=False`` on Pause/Stop so an empty in-memory snap
    cannot wipe a durable plan.
    """
    if allow_flat_close and maybe_close_on_confirmed_flat(positions, path=path):
        return None
    if _stk_rows(positions):
        reset_flat_streak()
    plan = reconcile_open_risk(
        positions, open_orders, load_trade_plan(path), thesis=thesis
    )
    if not plan:
        # Keep disk plan when we are not allowed to flat-close (pause/stop).
        if not allow_flat_close:
            return load_trade_plan(path)
        return None
    if bump:
        plan.cycles_open = int(plan.cycles_open or 0) + 1
        if plan.max_hold_cycles and plan.cycles_open >= int(plan.max_hold_cycles):
            save_trade_plan(plan, path)  # persist before time_stop archive
            close_trade_plan("time_stop", path)
            return None
    save_trade_plan(plan, path)
    return plan


def format_open_risk_line(plan: ActiveTradePlan | None) -> str:
    if not plan:
        return "Open risk: (flat / no plan)"
    bits = [
        f"OPEN RISK  {plan.symbol} {plan.direction}",
    ]
    if plan.quantity is not None:
        bits.append(f"x{plan.quantity:g}")
    if plan.entry_price is not None:
        bits.append(f"@{plan.entry_price}")
    if plan.stop_price is not None:
        bits.append(f"stop={plan.stop_price}")
    if plan.target_price is not None:
        bits.append(f"tgt={plan.target_price}")
    bits.append(f"cycles={plan.cycles_open}/{plan.max_hold_cycles}")
    return "  ".join(bits)
