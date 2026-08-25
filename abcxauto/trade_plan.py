"""ActiveTradePlan(s) — IBKR open-risk reconciliation, not a second notebook.

Multi-plan book: ``active_trade_plans.json`` holds STK lots vs working exits.
Thesis / lifecycle live on the playbook card tagged at send. Broker book is
source of truth; confirmed-flat (not a single empty snap) closes plans.
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
_DEFAULT_PLANS_PATH = _REPO_ROOT / "active_trade_plans.json"
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
    """Legacy single-plan path (still written as mirror of primary for tools)."""
    import os

    raw = os.environ.get("ABCXAUTO_TRADE_PLAN_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def _plans_path() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_TRADE_PLANS_PATH", "").strip()
    if raw:
        return Path(raw)
    # Derive beside legacy single-plan path when tests set TRADE_PLAN_PATH.
    single = _path()
    if single != _DEFAULT_PATH:
        return single.with_name("active_trade_plans.json")
    return _DEFAULT_PLANS_PATH


def load_trade_plans(path: Path | None = None) -> list[ActiveTradePlan]:
    """All open plans (multi-plan book). Migrates legacy single-file if needed."""
    plans_p = path or _plans_path()
    legacy = _path()
    out: list[ActiveTradePlan] = []
    if plans_p.is_file():
        try:
            raw = json.loads(plans_p.read_text(encoding="utf-8"))
            items = raw.get("plans") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict) or item.get("status") == "closed":
                        continue
                    try:
                        out.append(ActiveTradePlan.from_dict(item))
                    except Exception:
                        continue
        except Exception:
            logger.exception("load_trade_plans failed")
    if not out and legacy.is_file():
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("status") != "closed":
                out = [ActiveTradePlan.from_dict(raw)]
                save_trade_plans(out, plans_p)
        except Exception:
            logger.exception("legacy trade plan migrate failed")
    return out


def save_trade_plans(
    plans: list[ActiveTradePlan], path: Path | None = None
) -> Path:
    plans_p = path or _plans_path()
    open_plans = [p for p in plans if p and str(p.status or "open") != "closed"]
    plans_p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plans": [p.to_dict() for p in open_plans], "ts": _utc_now()}
    plans_p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Mirror primary to legacy path for scripts / older readers.
    legacy = _path()
    if open_plans:
        primary = open_plans[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            json.dumps(primary.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
    else:
        try:
            if legacy.is_file():
                legacy.unlink()
        except OSError:
            pass
    return plans_p


def load_trade_plan(path: Path | None = None) -> Optional[ActiveTradePlan]:
    """Primary plan (first in multi-plan book) — back-compat."""
    plans = load_trade_plans()
    if plans:
        return plans[0]
    # Explicit legacy path override (tests sometimes pass path=)
    if path is not None and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("status") != "closed":
                return ActiveTradePlan.from_dict(raw)
        except Exception:
            logger.exception("load_trade_plan failed")
    return None


def save_trade_plan(plan: ActiveTradePlan, path: Path | None = None) -> Path:
    """Upsert one plan into the multi-plan book by symbol."""
    plans = load_trade_plans()
    sym = str(plan.symbol or "").upper()
    plan.symbol = sym
    replaced = False
    for i, p in enumerate(plans):
        if str(p.symbol or "").upper() == sym:
            plans[i] = plan
            replaced = True
            break
    if not replaced:
        plans.append(plan)
    return save_trade_plans(plans)


def clear_trade_plan(path: Path | None = None) -> None:
    """Clear entire multi-plan book (+ legacy file)."""
    for p in (_plans_path(), _path()):
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            logger.exception("clear_trade_plan failed for %s", p)


def close_trade_plan(
    reason: str = "",
    path: Path | None = None,
    *,
    symbol: str | None = None,
) -> None:
    """Close one symbol (or primary if symbol omitted) and persist remaining."""
    plans = load_trade_plans()
    if not plans:
        clear_trade_plan(path)
        return
    sym = (symbol or "").upper() or str(plans[0].symbol or "").upper()
    closed: ActiveTradePlan | None = None
    kept: list[ActiveTradePlan] = []
    for p in plans:
        if str(p.symbol or "").upper() == sym:
            p.status = "closed"
            p.closed_at = _utc_now()
            p.close_reason = reason or "closed"
            closed = p
        else:
            kept.append(p)
    if closed is not None:
        try:
            archive = _plans_path().with_name("last_closed_trade_plan.json")
            archive.write_text(
                json.dumps(closed.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
    if kept:
        save_trade_plans(kept)
    else:
        clear_trade_plan(path)


def plan_from_bracket_action(act: dict, thesis: str = "") -> Optional[ActiveTradePlan]:
    """Build a plan from a successful new-entry bracket params."""
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
    _ = thesis
    return ActiveTradePlan(
        symbol=symbol,
        direction=direction if direction in ("LONG", "SHORT") else "LONG",
        thesis="",
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


def _order_id(o: dict) -> int | None:
    raw = o.get("order_id") if o.get("order_id") is not None else o.get("orderId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_protective_stop_type(otype: str) -> bool:
    from abcxauto.broker.order_types import is_stop_order

    text = (otype or "").upper()
    if is_stop_order(text):
        return True
    return any(k in text for k in ("STP", "TRAIL", "STOP"))


def iter_working_stops(
    open_orders: list[dict] | None, symbol: str, direction: str
):
    """Yield (order, abs_qty) for working STP/TRAIL on the exit side."""
    sym = (symbol or "").upper()
    want_exit = "SELL" if str(direction or "LONG").upper() == "LONG" else "BUY"
    for o in open_orders or []:
        if _order_symbol(o) != sym:
            continue
        sec = str(o.get("sec_type") or o.get("secType") or "STK").upper()
        if sec and not sec.startswith("STK") and sec != "ETF":
            continue
        action = _order_action(o)
        if action and action != want_exit:
            continue
        if not _is_protective_stop_type(_order_type(o)):
            continue
        q = _f(o.get("quantity") or o.get("totalQuantity") or o.get("total_quantity"))
        if q is None:
            continue
        yield o, abs(q)


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


def working_stop_qty(open_orders: list[dict] | None, symbol: str, direction: str) -> float | None:
    """Total working stop-order quantity for symbol (exit side). None if none."""
    sym = (symbol or "").upper()
    want_exit = "SELL" if str(direction or "LONG").upper() == "LONG" else "BUY"
    total = 0.0
    found = False
    for o in open_orders or []:
        if _order_symbol(o) != sym:
            continue
        action = _order_action(o)
        if action and action != want_exit:
            continue
        otype = _order_type(o)
        if not any(k in otype for k in ("STP", "TRAIL", "STOP")):
            continue
        q = _f(o.get("quantity") or o.get("totalQuantity") or o.get("total_quantity"))
        if q is None:
            continue
        found = True
        total += abs(q)
    return total if found else None


def stacked_stop_cancel_ids(
    positions: list[dict] | None,
    open_orders: list[dict] | None,
) -> list[int]:
    """Per STK symbol, keep the newest covering STP/TRAIL; return extra ids.

    A stop covers when its qty is at least the held lot (0.51 share slack).
    If no single order covers, cancel nothing (do not strip the last stop).
    If only one working stop exists, cancel nothing.
    """
    to_cancel: list[int] = []
    seen: set[str] = set()
    for row in _stk_rows(positions):
        sym = row["symbol"]
        if sym in seen:
            continue
        seen.add(sym)
        held_signed = stk_qty_for_symbol(positions, sym)
        held = abs(held_signed)
        if held < 1e-9:
            continue
        direction = "LONG" if held_signed > 0 else "SHORT"
        stops: list[tuple[int, float]] = []
        for o, q in iter_working_stops(open_orders, sym, direction):
            oid = _order_id(o)
            if oid is None:
                continue
            stops.append((oid, q))
        if len(stops) < 2:
            continue
        covering = [oid for oid, q in stops if q + 1e-9 >= held - 0.51]
        if not covering:
            continue
        keep_id = max(covering)
        for oid, _q in stops:
            if oid != keep_id:
                to_cancel.append(oid)
    return to_cancel


def _per_order_stop_qty(
    open_orders: list[dict] | None, symbol: str, direction: str, held: float
) -> tuple[float | None, bool]:
    """One working STP/TRAIL qty vs held. Stacked crumbs that sum are not a cover."""
    qtys = [q for _o, q in iter_working_stops(open_orders, symbol, direction)]
    if not qtys:
        return None, False
    covering = [q for q in qtys if q + 1e-9 >= held - 0.51]
    if covering:
        return min(covering), True
    return max(qtys), False


def stop_qty_mismatch_fact(
    positions: list[dict] | None,
    open_orders: list[dict] | None,
    plan: ActiveTradePlan | None = None,
) -> dict[str, Any] | None:
    """Fact: one working stop's qty vs STK held (after trim, stop may be oversized).

    Per order — same 0.51 slack as ``stacked_stop_cancel_ids``. Stacked crumbs
    that sum to held are not a match. ``match`` is the wake-line key.
    """
    plans = [plan] if plan is not None else load_trade_plans()
    if plan is not None and not plans:
        plans = [plan]
    if not plans:
        return None
    checked: list[dict[str, Any]] = []
    first_bad: dict[str, Any] | None = None
    for p in plans:
        if p is None or not p.symbol:
            continue
        held = abs(stk_qty_for_symbol(positions, p.symbol))
        if held < 1e-9:
            continue
        stop_q, covers = _per_order_stop_qty(
            open_orders, p.symbol, p.direction, held
        )
        if stop_q is None:
            row = {
                "symbol": p.symbol,
                "held_qty": held,
                "stop_order_qty": None,
                "mismatch": True,
                "match": False,
                "note": "no working stop qty found",
                "heuristic": "stop_qty vs held — heuristic ≠ recommendation",
            }
        else:
            mismatch = (not covers) or abs(stop_q - held) > 0.51
            row = {
                "symbol": p.symbol,
                "held_qty": held,
                "stop_order_qty": stop_q,
                "mismatch": mismatch,
                "match": not mismatch,
                "heuristic": "stop_qty vs held — heuristic ≠ recommendation",
            }
            if mismatch:
                row["note"] = (
                    "no single stop covers held"
                    if not covers
                    else (
                        "stop order qty ≠ held — after trim, resize stop "
                        "(modify/oca/cancel+replace)"
                    )
                )
        checked.append(row)
        if row.get("mismatch") and first_bad is None:
            first_bad = row
    if first_bad is None:
        if not checked:
            return None
        out = dict(checked[0])
        out["all"] = checked
        return out
    out = dict(first_bad)
    out["all"] = checked
    return out


def book_has_risk(positions: list[dict] | None) -> bool:
    """True if any non-zero position exists (STK/ETF/OPT — blocks false-flat new entry)."""
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


def _refresh_plan_from_row(
    plan: ActiveTradePlan,
    row: dict[str, Any],
    open_orders: list[dict] | None,
    thesis: str = "",
) -> ActiveTradePlan:
    qty_signed = float(row["quantity"])
    direction = "LONG" if qty_signed > 0 else "SHORT"
    plan.quantity = abs(qty_signed)
    plan.direction = direction
    stop, target = _exits_from_orders(open_orders, plan.symbol, direction)
    if stop is not None:
        plan.stop_price = stop
    if target is not None:
        plan.target_price = target
    if plan.entry_price is None:
        plan.entry_price = _avg_cost(row["raw"])
    _ = thesis
    if plan.stop_price is not None:
        plan.invalidation = f"stop {plan.stop_price}"
    plan.status = "open"
    return plan


def reconcile_open_risk_all(
    positions: list[dict] | None,
    open_orders: list[dict] | None = None,
    existing_plans: list[ActiveTradePlan] | None = None,
    *,
    thesis: str = "",
) -> list[ActiveTradePlan]:
    """Keep/rebuild plans for every open STK (multi-plan book)."""
    plans = list(existing_plans) if existing_plans is not None else load_trade_plans()
    rows = _stk_rows(positions)
    if not rows:
        return []

    by_sym = {r["symbol"]: r for r in rows}
    by_plan = {str(p.symbol or "").upper(): p for p in plans if p}
    out: list[ActiveTradePlan] = []

    for sym, row in by_sym.items():
        if sym in by_plan:
            out.append(
                _refresh_plan_from_row(by_plan[sym], row, open_orders, thesis=thesis)
            )
        else:
            qty_signed = float(row["quantity"])
            direction = "LONG" if qty_signed > 0 else "SHORT"
            stop, target = _exits_from_orders(open_orders, sym, direction)
            out.append(
                ActiveTradePlan(
                    symbol=sym,
                    direction=direction,
                    thesis="",
                    invalidation=(
                        f"stop {stop}" if stop is not None else "stop hit"
                    ),
                    entry_price=_avg_cost(row["raw"]),
                    stop_price=stop,
                    target_price=target,
                    quantity=abs(qty_signed),
                    cycles_open=0,
                    status="open",
                )
            )

    # Archive plans whose symbols left the STK book
    for sym, plan in by_plan.items():
        if sym not in by_sym:
            try:
                plan.status = "closed"
                plan.closed_at = _utc_now()
                plan.close_reason = "symbol_left_book"
                archive = _plans_path().with_name("last_closed_trade_plan.json")
                archive.write_text(
                    json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8"
                )
            except Exception:
                pass

    # Stable order: largest |qty| first (matches _stk_rows)
    order = {r["symbol"]: i for i, r in enumerate(rows)}
    out.sort(key=lambda p: order.get(str(p.symbol).upper(), 999))
    return out


def reconcile_open_risk(
    positions: list[dict] | None,
    open_orders: list[dict] | None = None,
    existing_plan: ActiveTradePlan | None = None,
    *,
    thesis: str = "",
) -> Optional[ActiveTradePlan]:
    """Primary plan after multi-plan reconcile (back-compat)."""
    existing = [existing_plan] if existing_plan is not None else None
    plans = reconcile_open_risk_all(
        positions, open_orders, existing, thesis=thesis
    )
    return plans[0] if plans else None


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


def reset_flat_streak() -> None:
    st = _flat_streak_state()
    _save_flat_streak_state(0, bool(st.get("ever_held")))


def maybe_close_on_confirmed_flat(
    positions: list[dict] | None,
    *,
    needed: int = _CONFIRMED_FLAT_N,
    path: Path | None = None,
) -> bool:
    """Close book only after held→flat transition + ``needed`` empty STK snaps.

    Ignores empty books that never saw STK (startup / false-empty before first fill).
    Does not close while any option (or other) risk remains in ``positions``.
    """
    rows = _stk_rows(positions)
    st = _flat_streak_state()
    if rows:
        _save_flat_streak_state(0, True)
        return False
    plans = load_trade_plans()
    # STK gone but options/other remain — close equity plans (stock legs flat).
    if book_has_risk(positions):
        if plans:
            clear_trade_plan(path)
            try:
                last = plans[0]
                last.status = "closed"
                last.closed_at = _utc_now()
                last.close_reason = "stk_flat_options_remain"
                archive = _plans_path().with_name("last_closed_trade_plan.json")
                archive.write_text(
                    json.dumps(last.to_dict(), indent=2) + "\n", encoding="utf-8"
                )
            except Exception:
                pass
            _save_flat_streak_state(0, False)
            return True
        return False
    if not st.get("ever_held") and not plans:
        _save_flat_streak_state(0, False)
        return False
    if not st.get("ever_held") and plans:
        st["ever_held"] = True
    n = int(st.get("empty_count") or 0) + 1
    _save_flat_streak_state(n, True)
    if n < max(1, int(needed)):
        return False
    if plans:
        clear_trade_plan(path)
        try:
            last = plans[0]
            last.status = "closed"
            last.closed_at = _utc_now()
            last.close_reason = "confirmed_flat"
            archive = _plans_path().with_name("last_closed_trade_plan.json")
            archive.write_text(
                json.dumps(last.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
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
    """Reconcile + persist all STK plans; confirmed-flat close when book empty.

    ``bump`` is ignored — hold time is the playbook card, not a cycle counter.
    Set ``allow_flat_close=False`` on Pause/Stop so an empty in-memory snap
    cannot wipe a durable plan.
    """
    _ = bump
    if allow_flat_close and maybe_close_on_confirmed_flat(positions, path=path):
        return None
    if _stk_rows(positions):
        reset_flat_streak()
    plans = reconcile_open_risk_all(
        positions, open_orders, load_trade_plans(), thesis=thesis
    )
    if not plans:
        if not allow_flat_close:
            return load_trade_plan(path)
        return None
    if plans:
        save_trade_plans(plans)
        return plans[0]
    clear_trade_plan(path)
    return None


def format_open_risk_line(plan: ActiveTradePlan | None = None) -> str:
    """Primary / multi-plan open-risk line for UI and notes."""
    plans = [plan] if plan is not None else load_trade_plans()
    if plan is not None and not plans:
        plans = [plan]
    return format_open_risk_lines(plans)


def format_open_risk_lines(plans: list[ActiveTradePlan] | None = None) -> str:
    if plans is None:
        plans = load_trade_plans()
    if not plans:
        return "Open risk: (flat / no plan)"
    if len(plans) == 1:
        p = plans[0]
        bits = [f"OPEN RISK  {p.symbol} {p.direction}"]
        if p.quantity is not None:
            bits.append(f"x{p.quantity:g}")
        if p.entry_price is not None:
            bits.append(f"@{p.entry_price}")
        if p.stop_price is not None:
            bits.append(f"stop={p.stop_price}")
        if p.target_price is not None:
            bits.append(f"tgt={p.target_price}")
        return "  ".join(bits)
    parts = []
    for p in plans:
        stop_s = f"{p.stop_price}" if p.stop_price is not None else "?"
        parts.append(f"{p.symbol} {p.direction} x{p.quantity:g} stop={stop_s}")
    return f"OPEN RISK BOOK ({len(plans)}): " + " | ".join(parts)


def open_position_count(positions: list[dict] | None) -> int:
    """Non-zero position rows (STK/OPT/…) — same counting as max_open_positions gate."""
    n = 0
    for p in positions or []:
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if qty != 0:
            n += 1
    return n


def _held_conids(positions: list[dict] | None) -> set[str]:
    out: set[str] = set()
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        cid = str(p.get("conId") or p.get("con_id") or "").strip()
        if cid:
            out.add(cid)
    return out


def _held_stk_symbols(positions: list[dict] | None) -> set[str]:
    out: set[str] = set()
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        if not (sec.startswith("STK") or sec == "ETF"):
            continue
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        sym = str(p.get("symbol") or "").upper()
        if sym:
            out.add(sym)
    return out


def working_entry_slots(
    orders: list[dict] | None,
    positions: list[dict] | None = None,
) -> int:
    """Slots reserved by working new-risk tickets (fill-lag ≠ free capacity).

    Protective stops and exits on held lots do not reserve. BAG reserves one
    slot per combo leg (default 2 when legs are missing).
    """
    held_con = _held_conids(positions)
    held_stk = _held_stk_symbols(positions)
    seen: set[tuple] = set()
    slots = 0
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        if _is_protective_stop_type(_order_type(o)):
            continue
        cid = str(o.get("conId") or o.get("con_id") or "").strip()
        if cid and cid in held_con:
            continue
        sec = str(o.get("sec_type") or o.get("secType") or "STK").upper()
        sym = _order_symbol(o)
        if (sec.startswith("STK") or sec == "ETF") and sym and sym in held_stk:
            continue
        if sec == "BAG":
            oid = _order_id(o)
            parent = o.get("parent_id") if o.get("parent_id") is not None else o.get("parentId")
            try:
                parent_i = int(parent) if parent is not None else None
            except (TypeError, ValueError):
                parent_i = None
            key = ("bag", parent_i if parent_i is not None else oid, sym)
            if key in seen:
                continue
            seen.add(key)
            legs = o.get("combo_legs") or o.get("comboLegs") or []
            # combo_legs only — ignore invented reserved_slots on the ticket.
            n = len(legs) if isinstance(legs, list) and legs else 2
            slots += max(1, n)
            continue
        strike = o.get("strike")
        right = str(o.get("right") or "").upper()[:1]
        exp = str(o.get("expiration") or o.get("lastTradeDateOrContractMonth") or "")
        key = ("lot", sym, sec, strike, right, exp)
        if key in seen:
            continue
        seen.add(key)
        slots += 1
    return slots


def capacity_fact(
    positions: list[dict] | None,
    *,
    max_open_positions: int = 0,
    open_orders: list[dict] | None = None,
) -> dict[str, Any]:
    """Fact: filled lots + working entries vs max_open_positions (0 max = unlimited)."""
    used = open_position_count(positions)
    pending = working_entry_slots(open_orders, positions)
    charged = used + pending
    max_n = int(max_open_positions or 0)
    if max_n <= 0:
        return {
            "open_count": used,
            "pending_entries": pending,
            "max_open_positions": 0,
            "slots_left": None,
            "allows_new_risk": True,
            "note": "max_open_positions disabled (unlimited capacity Fact)",
        }
    left = max(0, max_n - charged)
    note = f"{used}/{max_n} open"
    if pending:
        note += f" + {pending} working-entry"
    note += f"; {left} slot(s) for new risk"
    return {
        "open_count": used,
        "pending_entries": pending,
        "max_open_positions": max_n,
        "slots_left": left,
        "allows_new_risk": left > 0,
        "note": note,
    }
