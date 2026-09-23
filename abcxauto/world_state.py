"""WorldState — live book facts for Grok tools (no LLM)."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.trade_plan import capacity_fact, load_trade_plans

logger = logging.getLogger(__name__)

FILL_WINDOW_S = 180.0
COMBO_FACT = "IBKR BAG — short legs only as spread legs"
COMBO_STRATS = frozenset({
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "butterfly",
    "straddle",
    "strangle",
    "calendar_spread",
    "diagonal_spread",
    "ratio_spread",
    "jade_lizard",
})


def fill_age_s(fill: dict[str, Any], now: datetime | None = None) -> float | None:
    ts = str(fill.get("ts") or fill.get("time") or "")
    if not ts:
        return None
    try:
        raw = ts.replace("Z", "+00:00")
        clock = now or datetime.now(timezone.utc)
        return (clock - datetime.fromisoformat(raw)).total_seconds()
    except ValueError:
        return None


def fill_in_window(fill: dict[str, Any], *, window_s: float = FILL_WINDOW_S) -> bool:
    age = fill_age_s(fill)
    if age is None:
        return True
    return age <= window_s


def position_avg_facts(pos: dict[str, Any] | None) -> dict[str, Any]:
    """STK avg is per-share. OPT IBKR averageCost is usually contract cash."""
    p = pos if isinstance(pos, dict) else {}
    raw = p.get("avgCost") if p.get("avgCost") is not None else p.get("avg_cost")
    if raw is None:
        raw = p.get("averageCost")
    try:
        raw_f = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw_f = None
    mkt = p.get("market_price") or p.get("marketPrice") or p.get("last")
    try:
        mkt_f = float(mkt) if mkt is not None else None
    except (TypeError, ValueError):
        mkt_f = None
    sec = str(p.get("secType") or p.get("sec_type") or p.get("sec") or "STK").upper()
    out: dict[str, Any] = {"avg": raw_f}
    if not sec.startswith("OPT") or raw_f is None:
        return out
    contract = abs(raw_f) >= 5.0 and (mkt_f is None or abs(raw_f) > abs(mkt_f) * 3)
    if contract:
        out["avg"] = raw_f / 100.0
        out["avg_usd"] = raw_f
    else:
        out["avg"] = raw_f
        out["avg_usd"] = raw_f * 100.0
    return out


def compact_position(
    pos: dict[str, Any],
    *,
    extra: bool = True,
    net_liq: float | None = None,
    stop: float | None = None,
) -> dict[str, Any]:
    p = pos if isinstance(pos, dict) else {}
    avg_row = position_avg_facts(p)
    row: dict[str, Any] = {
        "conId": p.get("conId") or p.get("con_id"),
        "symbol": p.get("symbol"),
        "sec": p.get("secType") or p.get("sec_type"),
        "qty": (
            p.get("quantity")
            if p.get("quantity") is not None
            else (p.get("position") if p.get("position") is not None else p.get("qty"))
        ),
        "avg": avg_row.get("avg"),
        "mkt": p.get("market_price") or p.get("marketPrice") or p.get("mkt") or p.get("last"),
    }
    if avg_row.get("avg_usd") is not None:
        row["avg_usd"] = avg_row["avg_usd"]
    if extra:
        if p.get("expiration") or p.get("lastTradeDateOrContractMonth"):
            row["expiration"] = p.get("expiration") or p.get("lastTradeDateOrContractMonth")
        if p.get("strike") is not None:
            row["strike"] = p.get("strike")
        if p.get("right"):
            row["right"] = p.get("right")
        local = p.get("local_symbol") or p.get("localSymbol")
        if local:
            row["local"] = local
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        suffix = _lot_mtm_suffix(row, qty)
        if suffix:
            try:
                row["mtm_pct"] = float(suffix.strip().rstrip("%"))
            except (TypeError, ValueError):
                pass
        upnl = lot_upnl(p)
        if upnl is not None:
            row["uPnL"] = round(upnl, 2)
        nl = net_liq
        if nl is None:
            try:
                nl = float(p.get("_net_liq")) if p.get("_net_liq") is not None else None
            except (TypeError, ValueError):
                nl = None
        if nl is not None and nl > 0:
            if upnl is not None:
                row["uPnL_pct_nl"] = pct_of_nl(upnl, nl)
            avg_usd = row.get("avg_usd")
            if avg_usd is not None:
                pct = pct_of_nl(avg_usd, nl)
                if pct is not None:
                    row["avg_usd_pct_nl"] = pct
            try:
                mv = abs(float(p.get("marketValue") or p.get("market_value") or 0))
            except (TypeError, ValueError):
                mv = 0.0
            if mv > 0:
                row["mv_pct_nl"] = pct_of_nl(mv, nl)
            stop_px = stop
            if stop_px is None:
                raw_stop = p.get("stop") or p.get("stop_price") or p.get("aux_price")
                try:
                    stop_px = float(raw_stop) if raw_stop is not None else None
                except (TypeError, ValueError):
                    stop_px = None
            if stop_px is not None and qty != 0:
                mkt = row.get("mkt")
                try:
                    mkt_f = float(mkt) if mkt is not None else None
                except (TypeError, ValueError):
                    mkt_f = None
                if mkt_f is not None and mkt_f > 0:
                    sec = str(row.get("sec") or "STK").upper()
                    mult = 100.0 if sec.startswith("OPT") else 1.0
                    risk_usd = abs(float(mkt_f) - float(stop_px)) * abs(qty) * mult
                    row["risk_pct_nl"] = pct_of_nl(risk_usd, nl)
    return row


def pct_of_nl(usd: Any, net_liq: Any, *, digits: int = 4) -> float | None:
    """Percent of NetLiq for a dollar amount.

    Canonical % of NL helper. None when either side cannot be coerced or
    NetLiq is zero (unknown book — not 0%). Zero dollars on a live book is
    ``0.0``. ``risk_gates._pct_of_nl`` still returns ``0.0`` on book<=0;
    migrate those call sites to this helper.
    """
    try:
        dollars = float(usd)
        nl = float(net_liq)
    except (TypeError, ValueError):
        return None
    if nl == 0 or nl != nl or dollars != dollars:
        return None
    return round(100.0 * dollars / nl, digits)


def _fill_order_id(fill: dict[str, Any]) -> str:
    oid = fill.get("order_id") if fill.get("order_id") is not None else fill.get("orderId")
    return str(oid) if oid is not None and str(oid) else ""


def _fill_con_id(fill: dict[str, Any]) -> str:
    cid = fill.get("conId") if fill.get("conId") not in (None, "", 0, "0") else fill.get("con_id")
    return str(cid) if cid not in (None, "", 0, "0") else ""


def _fill_abs_qty(fill: dict[str, Any]) -> float:
    try:
        return abs(
            float(
                fill.get("quantity")
                if fill.get("quantity") is not None
                else fill.get("shares")
                or 0
            )
        )
    except (TypeError, ValueError):
        return 0.0


def _fill_side_u(fill: dict[str, Any]) -> str:
    return str(fill.get("side") or fill.get("action") or "").upper()


def _fill_signed_qty(fill: dict[str, Any]) -> float:
    qty = _fill_abs_qty(fill)
    if qty <= 0:
        return 0.0
    side = _fill_side_u(fill)
    if side in ("SLD", "SELL"):
        return -qty
    if side in ("BOT", "BUY"):
        return qty
    return 0.0


def _fill_is_opt(fill: dict[str, Any]) -> bool:
    sec = str(fill.get("sec_type") or fill.get("secType") or fill.get("sec") or "").upper()
    if sec in ("OPT", "FOP"):
        return True
    if sec in ("BAG", "STK", "CASH", "IND"):
        return False
    # Leg fills sometimes omit sec_type but carry option identity.
    return bool(
        fill.get("strike") is not None
        or fill.get("right")
        or fill.get("expiration")
        or fill.get("lastTradeDateOrContractMonth")
        or fill.get("local_symbol")
        or fill.get("localSymbol")
    )


def _position_from_combo_fill(fill: dict[str, Any], signed_qty: float) -> dict[str, Any]:
    """Desk lot from a BAG/combo leg fill. Qty comes from the fill — never invented."""
    cid = _fill_con_id(fill)
    sec = str(fill.get("sec_type") or fill.get("secType") or "OPT").upper() or "OPT"
    if sec not in ("OPT", "FOP"):
        sec = "OPT"
    row: dict[str, Any] = {
        "symbol": str(fill.get("symbol") or "").upper(),
        "secType": sec,
        "sec_type": sec,
        "quantity": signed_qty,
        "conId": int(cid) if cid.isdigit() else cid,
        "con_id": int(cid) if cid.isdigit() else cid,
        "_from_fill": True,
    }
    if fill.get("strike") is not None:
        row["strike"] = fill.get("strike")
    exp = fill.get("expiration") or fill.get("lastTradeDateOrContractMonth")
    if exp:
        row["expiration"] = exp
    right = fill.get("right")
    if right:
        row["right"] = right
    local = fill.get("local_symbol") or fill.get("localSymbol")
    if local:
        row["local_symbol"] = local
        row["localSymbol"] = local
    px = fill.get("price") or fill.get("avg_price") or fill.get("avgPrice")
    if px is not None:
        try:
            row["market_price"] = float(px)
            row["avg_cost"] = float(px)
        except (TypeError, ValueError):
            pass
    return row


def _attach_missing_combo_legs(
    positions: list[dict],
    fills: list[dict] | None,
    *,
    window_s: float,
) -> bool:
    """Paint missing BAG wings when fills already show the live combo.

    Only completes a wing when a same-order mate lot is already on the book
    (orphan long after debit vertical). Does not re-attach a fully closed
    combo from closing fills alone.
    """
    by_oid: dict[str, list[dict[str, Any]]] = {}
    for f in fills or []:
        if not isinstance(f, dict) or not fill_in_window(f, window_s=window_s):
            continue
        if not _fill_is_opt(f):
            continue
        oid = _fill_order_id(f)
        cid = _fill_con_id(f)
        if not oid or not cid or _fill_signed_qty(f) == 0:
            continue
        by_oid.setdefault(oid, []).append(f)
    held: dict[str, float] = {}
    for p in positions:
        cid = str(p.get("conId") or p.get("con_id") or "")
        if not cid:
            continue
        held[cid] = _row_signed_qty(p)
    attached = False
    for _oid, legs in by_oid.items():
        net: dict[str, float] = {}
        meta: dict[str, dict[str, Any]] = {}
        sides: set[str] = set()
        for f in legs:
            cid = _fill_con_id(f)
            sq = _fill_signed_qty(f)
            net[cid] = net.get(cid, 0.0) + sq
            meta[cid] = f
            side = _fill_side_u(f)
            if side in ("BOT", "BUY"):
                sides.add("BUY")
            elif side in ("SLD", "SELL"):
                sides.add("SELL")
        if len(net) < 2 or sides != {"BUY", "SELL"}:
            continue
        mate_present = any(
            cid in held and abs(held[cid]) > 1e-9 and held[cid] * sq > 0
            for cid, sq in net.items()
        )
        if not mate_present:
            continue
        for cid, sq in net.items():
            if abs(sq) < 1e-9:
                continue
            if cid in held and abs(held[cid]) > 1e-9:
                continue
            positions.append(_position_from_combo_fill(meta[cid], sq))
            held[cid] = sq
            attached = True
    return attached


def reconcile_book_with_fills(
    positions: list[dict] | None,
    orders: list[dict] | None,
    fills: list[dict] | None,
    *,
    window_s: float = FILL_WINDOW_S,
) -> tuple[list[dict], list[dict], bool]:
    """Align desk book with recent fills.

    Closing: SLD reduces longs; BOT reduces shorts. Opening SLD on a short
    wing must not erase the live combo. Missing BAG legs are attached from
    multi-leg fills when a mate lot is already on the book.

    The bool is True only when a fill in the window changed the desk book
    (qty reduced, filled order dropped, or combo leg attached). A qty-matched
    working stop with no such fill leaves it False — that is fill-lag status,
    not a protection mismatch.
    """
    pos_out = [dict(p) for p in (positions or []) if isinstance(p, dict)]
    ord_out = [dict(o) for o in (orders or []) if isinstance(o, dict)]
    close_long: dict[str, float] = {}
    close_short: dict[str, float] = {}
    filled_ids: set[str] = set()
    for f in fills or []:
        if not isinstance(f, dict) or not fill_in_window(f, window_s=window_s):
            continue
        oid = _fill_order_id(f)
        if oid:
            filled_ids.add(oid)
        pid = f.get("permId") if f.get("permId") is not None else f.get("perm_id")
        if pid is not None and str(pid):
            filled_ids.add(str(pid))
        cid = _fill_con_id(f)
        qty = _fill_abs_qty(f)
        if not cid or qty <= 0:
            continue
        side = _fill_side_u(f)
        if side in ("SLD", "SELL"):
            close_long[cid] = close_long.get(cid, 0.0) + qty
        elif side in ("BOT", "BUY"):
            close_short[cid] = close_short.get(cid, 0.0) + qty
    reconciled = False
    kept_pos: list[dict] = []
    for p in pos_out:
        cid = str(p.get("conId") or p.get("con_id") or "")
        try:
            raw_q = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            raw_q = 0.0
        mag = abs(raw_q)
        if raw_q > 0:
            take = close_long.get(cid, 0.0) if cid else 0.0
        elif raw_q < 0:
            take = close_short.get(cid, 0.0) if cid else 0.0
        else:
            take = 0.0
        if take > 0 and mag > 0:
            reconciled = True
            left = mag - take
            if left < 1e-9:
                continue
            sign = 1.0 if raw_q > 0 else -1.0
            if "quantity" in p:
                p["quantity"] = sign * left
            if "position" in p:
                p["position"] = sign * left
        kept_pos.append(p)
    if _attach_missing_combo_legs(kept_pos, fills, window_s=window_s):
        reconciled = True
    kept_ord: list[dict] = []
    for o in ord_out:
        oid = str(o.get("order_id") if o.get("order_id") is not None else o.get("orderId") or "")
        pid = str(o.get("permId") if o.get("permId") is not None else o.get("perm_id") or "")
        if (oid and oid in filled_ids) or (pid and pid in filled_ids):
            reconciled = True
            continue
        kept_ord.append(o)
    return kept_pos, kept_ord, reconciled


def book_is_flat(
    positions: list[dict] | None,
    orders: list[dict] | None,
    fills: list[dict] | None = None,
) -> bool:
    """Empty book only when no lots, no working tickets, and no pending entry fill."""
    from abcxauto.trade_plan import book_has_risk

    if book_has_risk(positions):
        return False
    if any(isinstance(o, dict) for o in (orders or [])):
        return False
    held = {
        str(p.get("conId") or p.get("con_id") or "")
        for p in (positions or [])
        if isinstance(p, dict)
    }
    held.discard("")
    now = datetime.now(timezone.utc)
    for f in fills or []:
        if not isinstance(f, dict):
            continue
        side = str(f.get("side") or f.get("action") or "").upper()
        if side not in ("BOT", "BUY"):
            continue
        cid = str(f.get("conId") or f.get("con_id") or "")
        if cid and cid in held:
            continue
        ts = str(f.get("ts") or f.get("time") or "")
        if ts:
            try:
                raw = ts.replace("Z", "+00:00")
                age = (now - datetime.fromisoformat(raw)).total_seconds()
                if age > 180:
                    continue
            except ValueError:
                pass
        return False
    return True


def _row_con_id(row: dict[str, Any] | None) -> str:
    p = row if isinstance(row, dict) else {}
    for key in ("conId", "con_id"):
        v = p.get(key)
        if v not in (None, "", 0, "0"):
            return str(v)
    return ""


def _row_signed_qty(row: dict[str, Any] | None) -> float:
    p = row if isinstance(row, dict) else {}
    raw = p.get("qty")
    if raw is None:
        raw = p.get("quantity") if p.get("quantity") is not None else p.get("position")
    if raw is None:
        raw = p.get("totalQuantity")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _qty_side(qty: float) -> tuple[str, str]:
    side = "short" if qty < 0 else "long"
    mag = abs(qty)
    qty_s = str(int(mag)) if float(mag).is_integer() else f"{mag:g}"
    return side, qty_s


def _contract_fp(row: dict[str, Any] | None, *, use_id: bool = True) -> tuple[Any, ...]:
    """Match a working ticket to an open lot without requiring conId."""
    p = row if isinstance(row, dict) else {}
    if use_id:
        cid = _row_con_id(p)
        if cid:
            return ("id", cid)
    sym = str(p.get("symbol") or "").upper()
    sec = str(p.get("sec") or p.get("sec_type") or p.get("secType") or "STK").upper()
    if sec.startswith("OPT") or sec == "FOP":
        exp = str(p.get("expiration") or p.get("lastTradeDateOrContractMonth") or "")
        exp = exp[-6:] if len(exp) >= 6 else exp
        right = str(p.get("right") or "")[:1].upper()
        strike = p.get("strike")
        try:
            strike_s = f"{float(strike):g}"
        except (TypeError, ValueError):
            strike_s = str(strike or "")
        return ("opt", sym, exp, right, strike_s)
    return ("stk", sym, sec)


def _sane_trail_value(raw: Any, *, percent: bool) -> float | None:
    """Real trail only — IBKR leaves unset doubles at ~sys.float_info.max."""
    if raw in (None, "", 0, 0.0, "0"):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    # Percent trails are typically under 100; amount trails are price-sized.
    if percent:
        return v if v < 100.0 else None
    return v if v < 1e6 else None


def compact_trail(order: dict[str, Any] | None) -> float | None:
    """First sane trail percent or amount; omit IBKR unset sentinels."""
    o = order if isinstance(order, dict) else {}
    for key, is_pct in (
        ("trail_percent", True),
        ("trailingPercent", True),
        ("trail_amount", False),
    ):
        got = _sane_trail_value(o.get(key), percent=is_pct)
        if got is not None:
            return got
    return None


def compact_working_orders(
    orders: list[dict] | None,
    *,
    positions: list[dict] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Book facts: working order id, type, qty, stop/trail, exit vs entry."""
    by_id: dict[str, dict[str, Any]] = {}
    by_fp: dict[tuple[Any, ...], dict[str, Any]] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        qty = _row_signed_qty(p)
        if abs(qty) < 1e-9:
            continue
        rec = {"ident": lot_ident(p), "qty": qty}
        cid = _row_con_id(p)
        if cid:
            by_id[cid] = rec
        by_fp[_contract_fp(p, use_id=False)] = rec
    rows: list[dict[str, Any]] = []
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        oid = o.get("order_id")
        if oid is None:
            oid = o.get("orderId")
        otype = o.get("order_type") or o.get("orderType")
        sec = str(o.get("sec_type") or o.get("secType") or "STK").upper()
        action = str(o.get("action") or o.get("side") or "").upper()
        row: dict[str, Any] = {
            "order_id": oid,
            "symbol": o.get("symbol"),
            "sec": sec,
            "type": otype,
            "action": o.get("action") or o.get("side"),
            "qty": o.get("quantity") if o.get("quantity") is not None else o.get("totalQuantity"),
        }
        cid_raw = o.get("conId") if o.get("conId") not in (None, "", 0, "0") else o.get("con_id")
        if cid_raw not in (None, "", 0, "0"):
            row["conId"] = cid_raw
        cid = _row_con_id(o)
        if sec.startswith("OPT"):
            if o.get("strike") is not None:
                row["strike"] = o.get("strike")
            if o.get("right"):
                row["right"] = o.get("right")
            if o.get("expiration"):
                row["expiration"] = o.get("expiration")
            local = o.get("local_symbol") or o.get("localSymbol")
            if local:
                row["local"] = local
        stop = (
            o.get("aux_price")
            or o.get("auxPrice")
            or o.get("stop_price")
            or o.get("stopPrice")
        )
        if stop not in (None, 0, 0.0, "0"):
            row["stop"] = stop
        lmt = o.get("lmt_price") or o.get("lmtPrice") or o.get("limit_price")
        if lmt not in (None, 0, 0.0, "0"):
            row["lmt"] = lmt
        trail = compact_trail(o)
        if trail is not None:
            row["trail"] = trail
        if sec == "BAG":
            legs = o.get("combo_legs") or o.get("comboLegs")
            if isinstance(legs, list) and legs:
                row["legs"] = len(legs)
            reserved = o.get("reserved_slots")
            if reserved not in (None, 0, 0.0, "0"):
                row["reserved_slots"] = reserved
        lot = by_id.get(cid) if cid else None
        if lot is None:
            lot = by_fp.get(_contract_fp(o, use_id=False))
        if lot:
            row["covers"] = lot["ident"]
            lot_qty = float(lot.get("qty") or 0)
            closing = (action in {"SELL", "SLD"} and lot_qty > 0) or (
                action in {"BUY", "BOT"} and lot_qty < 0
            )
            otype_u = str(otype or "").upper()
            if not closing and not action and (
                "STP" in otype_u or otype_u.startswith("TRAIL")
            ):
                closing = True
            row["role"] = "exit" if closing else "add"
        else:
            row["role"] = "entry"
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def format_working_exits(
    orders: list[dict] | None,
    positions: list[dict] | None = None,
    *,
    limit: int = 6,
) -> str:
    """Compact STP/LMT exits for the wake line."""
    bits: list[str] = []
    for row in compact_working_orders(orders, positions=positions, limit=12):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        typ = str(row.get("type") or "").upper()
        if role == "entry" and "STP" not in typ and "LMT" not in typ and "LIMIT" not in typ:
            continue
        if role not in ("exit", "") and "STP" not in typ and "LMT" not in typ:
            if role == "add":
                continue
        px = row.get("stop") if row.get("stop") is not None else row.get("lmt")
        oid = row.get("order_id")
        sym = row.get("symbol") or "?"
        qty = row.get("qty")
        bit = f"{sym} {typ or '?'} {px} oid {oid}"
        if qty not in (None, ""):
            bit += f" qty={qty}"
        bits.append(bit)
        if len(bits) >= limit:
            break
    return " / ".join(bits)


def format_lot_lasts(world: Any, *, limit: int = 6) -> str:
    qmap = getattr(world, "ibkr_live_quotes", None) or {}
    if not isinstance(qmap, dict):
        return ""
    bits: list[str] = []
    seen: set[str] = set()
    for p in getattr(world, "positions", None) or []:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        if sec in ("OPT", "FOP", "BAG"):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        last = qmap.get(sym)
        if last is None:
            continue
        bits.append(f"{sym} last={last}")
        if len(bits) >= limit:
            break
    return " ".join(bits)


def concentration(positions: list[dict] | None) -> dict[str, Any]:
    """Lots vs names. cloned = extra same-side lots, not vertical legs."""
    by_name: dict[str, dict[str, Any]] = {}
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        qty = _row_signed_qty(p)
        if abs(qty) < 1e-9:
            continue
        rec = by_name.setdefault(
            sym,
            {
                "lots": 0,
                "qty": 0.0,
                "long": 0,
                "short": 0,
                "vert": 0,
                "extra": 0,
                "structures": 0,
            },
        )
        rec["lots"] += 1
        rec["qty"] += abs(qty)
        if qty > 0:
            rec["long"] += 1
        else:
            rec["short"] += 1
        sec = str(p.get("secType") or p.get("sec_type") or p.get("sec") or "STK").upper()
        exp = ""
        if sec in ("OPT", "FOP"):
            exp = str(p.get("expiration") or p.get("lastTradeDateOrContractMonth") or "")
        bucket = buckets.setdefault((sym, exp), {"long": 0, "short": 0})
        if qty > 0:
            bucket["long"] += 1
        else:
            bucket["short"] += 1
    for (sym, _exp), bucket in buckets.items():
        rec = by_name[sym]
        rec["vert"] += min(bucket["long"], bucket["short"])
        rec["extra"] += abs(bucket["long"] - bucket["short"])
    for rec in by_name.values():
        rec["structures"] = int(rec["vert"]) + int(rec["extra"])
    lots = int(sum(int(v["lots"]) for v in by_name.values()))
    structures = int(sum(int(v["structures"]) for v in by_name.values()))
    return {
        "names": len(by_name),
        "lots": lots,
        "structures": structures,
        "by_name": by_name,
        "cloned": sorted(s for s, v in by_name.items() if int(v["extra"]) > 1),
    }


def structure_mix(positions: list[dict] | None) -> dict[str, int]:
    """Clerk count of book geometry. Not a rank or a strategy menu."""
    long_c = short_c = long_p = short_p = stk = 0
    paired: dict[tuple[str, str], dict[str, int]] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        row = compact_position(p, extra=True)
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if abs(qty) < 1e-9:
            continue
        sec = str(row.get("sec") or "STK").upper()
        if sec == "STK":
            stk += 1
            continue
        if sec not in ("OPT", "FOP"):
            continue
        right = str(row.get("right") or "").upper()[:1]
        sym = str(row.get("symbol") or "").upper()
        exp = str(row.get("expiration") or "")
        key = (sym, exp)
        bucket = paired.setdefault(key, {"long": 0, "short": 0})
        if qty > 0:
            bucket["long"] += 1
            if right == "P":
                long_p += 1
            else:
                long_c += 1
        else:
            bucket["short"] += 1
            if right == "P":
                short_p += 1
            else:
                short_c += 1
    vert = int(sum(min(v["long"], v["short"]) for v in paired.values()))
    return {
        "long_c": long_c,
        "short_c": short_c,
        "long_p": long_p,
        "short_p": short_p,
        "stk": stk,
        "vert": vert,
    }


def format_mix(mix: dict[str, Any] | None) -> str:
    m = mix if isinstance(mix, dict) else {}
    bits = []
    for key, label in (
        ("long_c", "longC"),
        ("short_c", "shortC"),
        ("long_p", "longP"),
        ("short_p", "shortP"),
        ("vert", "vert"),
        ("stk", "stk"),
    ):
        try:
            n = int(m.get(key) or 0)
        except (TypeError, ValueError):
            n = 0
        if n:
            bits.append(f"{label}:{n}")
    return ",".join(bits)


def _lot_mtm_suffix(row: dict[str, Any], qty: float) -> str:
    try:
        avg = float(row.get("avg"))
        mkt = float(row.get("mkt"))
    except (TypeError, ValueError):
        return ""
    if avg == 0 or mkt is None:
        return ""
    if qty < 0:
        pct = (avg - mkt) / abs(avg) * 100.0
    else:
        pct = (mkt - avg) / abs(avg) * 100.0
    return f" {pct:+.0f}%"


def _opt_exp_key(pos: dict[str, Any] | None) -> str:
    p = pos if isinstance(pos, dict) else {}
    raw = str(p.get("expiration") or p.get("lastTradeDateOrContractMonth") or "").replace("-", "")
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    if len(raw) == 6 and raw.isdigit():
        return "20" + raw
    return raw


def _ticket_param(params: Any, *keys: str) -> Any:
    if isinstance(params, dict):
        for key in keys:
            if params.get(key) is not None:
                return params.get(key)
        return None
    for key in keys:
        val = getattr(params, key, None)
        if val is not None:
            return val
    return None


def vertical_partner(
    position: dict[str, Any] | None,
    positions: list[dict] | None,
) -> dict[str, Any] | None:
    """Opposite-side OPT, same name/expiry/right, different strike. Closest wing."""
    if not isinstance(position, dict):
        return None
    sec = str(position.get("secType") or position.get("sec_type") or position.get("sec") or "").upper()
    if not (sec.startswith("OPT") or sec == "FOP"):
        return None
    qty = _row_signed_qty(position)
    if abs(qty) < 1e-9:
        return None
    sym = str(position.get("symbol") or "").upper()
    exp = _opt_exp_key(position)
    right = str(position.get("right") or "")[:1].upper()
    try:
        strike = float(position.get("strike"))
    except (TypeError, ValueError):
        return None
    if not sym or not exp or not right:
        return None
    want = -1.0 if qty > 0 else 1.0
    best: dict[str, Any] | None = None
    best_dist: float | None = None
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        psec = str(p.get("secType") or p.get("sec_type") or p.get("sec") or "").upper()
        if not (psec.startswith("OPT") or psec == "FOP"):
            continue
        pq = _row_signed_qty(p)
        if pq * want <= 0:
            continue
        if str(p.get("symbol") or "").upper() != sym:
            continue
        if _opt_exp_key(p) != exp or str(p.get("right") or "")[:1].upper() != right:
            continue
        try:
            ps = float(p.get("strike"))
        except (TypeError, ValueError):
            continue
        if abs(ps - strike) < 1e-9:
            continue
        dist = abs(ps - strike)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = p
    return best


def ticket_option_lot(params: Any, positions: list[dict] | None) -> dict[str, Any] | None:
    """OPT lot a single-leg ticket aims at. conId first; else strike fingerprint."""
    target = _ticket_param(params, "conId", "con_id")
    if target not in (None, "", 0, "0"):
        want = str(target).strip()
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            if str(p.get("conId") or p.get("con_id") or "") == want:
                sec = str(p.get("secType") or p.get("sec_type") or "").upper()
                if sec.startswith("OPT") or sec == "FOP":
                    return p
                return None
        return None
    symbol = str(_ticket_param(params, "symbol") or "").upper()
    right = str(_ticket_param(params, "right") or "")[:1].upper()
    strike = _ticket_param(params, "strike")
    exp = _opt_exp_key({
        "expiration": _ticket_param(params, "expiration"),
    })
    if not symbol or not right or strike is None:
        return None
    try:
        strike_f = float(strike)
    except (TypeError, ValueError):
        return None
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "").upper()
        if not (sec.startswith("OPT") or sec == "FOP"):
            continue
        if str(p.get("symbol") or "").upper() != symbol:
            continue
        if str(p.get("right") or "")[:1].upper() != right:
            continue
        if exp and _opt_exp_key(p) != exp:
            continue
        try:
            if abs(float(p.get("strike")) - strike_f) < 1e-9:
                return p
        except (TypeError, ValueError):
            continue
    return None


_SINGLE_LEG_EXIT = frozenset({
    "limit_order",
    "market_order",
    "stop_order",
    "stop_limit",
    "close_option",
    "oca",
    "trailing_stop",
    "trailing_stop_limit",
})


def combo_partner(
    position: dict[str, Any] | None,
    positions: list[dict] | None,
) -> dict[str, Any] | None:
    """Hedge lot that a one-contract ticket would strand.

    Vertical first, then calendar/diagonal (same right, other expiry), then
    opposite-right same expiry when a fill would leave a short. Two longs of
    opposite rights (long straddle/strangle) are not a combo for this gate.
    """
    mate = vertical_partner(position, positions)
    if mate is not None:
        return mate
    if not isinstance(position, dict):
        return None
    sec = str(position.get("secType") or position.get("sec_type") or position.get("sec") or "").upper()
    if not (sec.startswith("OPT") or sec == "FOP"):
        return None
    qty = _row_signed_qty(position)
    if abs(qty) < 1e-9:
        return None
    sym = str(position.get("symbol") or "").upper()
    exp = _opt_exp_key(position)
    right = str(position.get("right") or "")[:1].upper()
    if not sym or not exp or not right:
        return None
    try:
        strike = float(position.get("strike"))
    except (TypeError, ValueError):
        strike = None
    want = -1.0 if qty > 0 else 1.0
    cal_best: dict[str, Any] | None = None
    cal_dist: float | None = None
    opp_best: dict[str, Any] | None = None
    opp_dist: float | None = None
    want_right = "P" if right == "C" else "C" if right == "P" else ""
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        psec = str(p.get("secType") or p.get("sec_type") or p.get("sec") or "").upper()
        if not (psec.startswith("OPT") or psec == "FOP"):
            continue
        if str(p.get("symbol") or "").upper() != sym:
            continue
        pq = _row_signed_qty(p)
        if abs(pq) < 1e-9:
            continue
        p_right = str(p.get("right") or "")[:1].upper()
        p_exp = _opt_exp_key(p)
        try:
            ps = float(p.get("strike"))
        except (TypeError, ValueError):
            ps = None
        dist = abs(ps - strike) if strike is not None and ps is not None else 0.0
        if p_right == right and p_exp != exp and pq * want > 0:
            if cal_dist is None or dist < cal_dist:
                cal_dist = dist
                cal_best = p
            continue
        if p_right != want_right or p_exp != exp:
            continue
        both_short = qty < 0 and pq < 0
        opposite_signs = qty * pq < 0
        if not (both_short or opposite_signs):
            continue
        if opp_dist is None or dist < opp_dist:
            opp_dist = dist
            opp_best = p
    return cal_best or opp_best


def single_leg_vertical_block(
    strategy: str,
    params: Any,
    positions: list[dict] | None,
) -> str | None:
    """Error when a one-contract ticket would break a live combo."""
    st = str(strategy or "").lower()
    if st in COMBO_STRATS or st not in _SINGLE_LEG_EXIT:
        return None
    lot = ticket_option_lot(params, positions)
    if lot is None:
        return None
    if combo_partner(lot, positions) is None:
        return None
    return (
        "defined_risk_only: single-leg ticket on a live combo — "
        "use the matching combo send with closing_position (BAG)"
    )


def lot_ident(pos: dict[str, Any] | None) -> str:
    """Contract identity for one lot. No MTM, no card tag — safe to call per paint."""
    row = compact_position(pos if isinstance(pos, dict) else {}, extra=True)
    qty = _row_signed_qty(row)
    side, qty_s = _qty_side(qty)
    sym = str(row.get("symbol") or "?").upper()
    sec = str(row.get("sec") or "STK").upper()
    if sec in ("OPT", "FOP"):
        exp = str(row.get("expiration") or "")
        exp = exp[-6:] if len(exp) >= 6 else exp
        right = str(row.get("right") or "")[:1]
        return f"{sym} {exp}{right}{row.get('strike')} {side} {qty_s}"
    return f"{sym} {sec} {side} {qty_s}"


def lot_dte(pos: dict[str, Any] | None) -> int | None:
    """Days to expiration. None for STK or an unparseable expiry."""
    if not isinstance(pos, dict):
        return None
    sec = str(pos.get("secType") or pos.get("sec_type") or pos.get("sec") or "STK").upper()
    if sec not in ("OPT", "FOP"):
        return None
    raw = _opt_exp_key(pos)
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        exp = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None
    return (exp - date.today()).days


def lot_structure_name(
    pos: dict[str, Any] | None,
    positions: list[dict] | None = None,
) -> str:
    """Book geometry for one lot: STK / vert / cal / combo / call / put."""
    if not isinstance(pos, dict):
        return ""
    sec = str(pos.get("secType") or pos.get("sec_type") or pos.get("sec") or "STK").upper()
    if sec == "STK":
        return "STK"
    if sec not in ("OPT", "FOP"):
        return sec or "opt"
    book = list(positions or [])
    if vertical_partner(pos, book) is not None:
        return "vert"
    mate = combo_partner(pos, book)
    if mate is not None:
        if _opt_exp_key(mate) != _opt_exp_key(pos):
            return "cal"
        return "combo"
    right = str(pos.get("right") or "")[:1].upper()
    if right == "C":
        return "call"
    if right == "P":
        return "put"
    return "opt"


def _iso_days_held(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    if age < 0:
        return 0
    return int(age // 86400)


def _plan_symbol(plan: Any) -> str:
    if isinstance(plan, dict):
        return str(plan.get("symbol") or "").upper()
    return str(getattr(plan, "symbol", "") or "").upper()


def _plan_opened_at(plan: Any) -> Any:
    if isinstance(plan, dict):
        return plan.get("opened_at")
    return getattr(plan, "opened_at", None)


def lot_days_held(
    pos: dict[str, Any] | None,
    *,
    fills: list[dict] | None = None,
    plans: list[Any] | None = None,
) -> int | None:
    """Days since open when a timestamp is known. Context only — not a flatten."""
    if not isinstance(pos, dict):
        return None
    for key in ("opened_at", "open_time", "open_ts", "time"):
        held = _iso_days_held(pos.get(key))
        if held is not None:
            return held
    cid = _row_con_id(pos)
    oldest: int | None = None
    if cid:
        for f in fills or []:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("conId") or f.get("con_id") or "")
            if fid != cid:
                continue
            held = _iso_days_held(f.get("ts") or f.get("time") or f.get("opened_at"))
            if held is None:
                continue
            oldest = held if oldest is None else max(oldest, held)
    if oldest is not None:
        return oldest
    sym = str(pos.get("symbol") or "").upper()
    if not sym:
        return None
    for plan in plans or []:
        if _plan_symbol(plan) != sym:
            continue
        held = _iso_days_held(_plan_opened_at(plan))
        if held is not None:
            return held
    return None


def _fmt_lot_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def lot_labels(
    positions: list[dict] | None,
    *,
    limit: int = 32,
    fills: list[dict] | None = None,
    plans: list[Any] | None = None,
) -> list[str]:
    """Wake / last_turn lot facts: identity + structure / DTE / uPnL / days_held.

    Age and marks are context. This does not flatten or rank.
    """
    book = [p for p in (positions or []) if isinstance(p, dict)]
    labels: list[str] = []
    for p in book:
        row = compact_position(p, extra=True)
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if abs(qty) < 1e-9:
            continue
        ident = lot_ident(p)
        extra = _lot_mtm_suffix(row, qty)
        if extra:
            ident = f"{ident}{extra}"
        bits = [ident]
        struct = lot_structure_name(p, book)
        tokens = {t.upper() for t in ident.replace(",", " ").split()}
        if struct and struct.upper() not in tokens:
            bits.append(struct)
        dte = lot_dte(p)
        if dte is not None:
            bits.append(f"DTE={dte}")
        upnl = lot_upnl(p)
        if upnl is not None:
            bits.append(f"uPnL={_fmt_lot_num(upnl)}")
        held = lot_days_held(p, fills=fills, plans=plans)
        if held is not None:
            bits.append(f"days_held={held}")
        labels.append(" ".join(bits))
        if len(labels) >= limit:
            break
    return labels


def worst_fact_open_lots(day: dict[str, Any] | None) -> list[str]:
    """Every open lot the wake / worst-fact path must address. Context only."""
    d = day if isinstance(day, dict) else {}
    return [str(x).strip() for x in (d.get("open_lots") or []) if str(x).strip()]


def account_float(account: dict[str, Any] | None, *keys: str) -> float | None:
    """Read a numeric IBKR tag. 0.0 is valid — do not fall through with ``or``."""
    acct = account if isinstance(account, dict) else {}
    for key in keys:
        if key in acct and acct[key] is not None:
            try:
                return float(acct[key])
            except (TypeError, ValueError):
                continue
        lower = str(key).lower()
        for ak, av in acct.items():
            if str(ak).lower() == lower and av is not None:
                try:
                    return float(av)
                except (TypeError, ValueError):
                    break
    return None


def daily_pnl_of(account: dict[str, Any] | None) -> float | None:
    """IBKR DailyPnL (today vs prior close). Not UnrealizedPnL vs average cost."""
    return account_float(account, "dailypnl", "DailyPnL")


def lot_upnl(pos: dict[str, Any] | None) -> float | None:
    """IBKR lot UnrealizedPnL in dollars. Missing is None, not 0."""
    if not isinstance(pos, dict):
        return None
    for key in ("unrealizedPNL", "unrealized_pnl", "uPnL"):
        raw = pos.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def open_upnl_of(positions: list[dict] | None) -> float | None:
    """Signed sum of lot unrealized PnL. Not DailyPnL and not scorecard edge."""
    total = 0.0
    n = 0
    for p in positions or []:
        v = lot_upnl(p)
        if v is None:
            continue
        total += v
        n += 1
    if not n:
        return None
    return round(total, 2)


def _minutes_to_open(world: Any) -> int | None:
    pulse = getattr(world, "pulse", None) if world is not None else None
    if not isinstance(pulse, dict):
        pulse = {}
    sess = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    if sess.get("countdown_to") == "open" and sess.get("countdown_s") is not None:
        try:
            return max(0, int(float(sess["countdown_s"]) // 60))
        except (TypeError, ValueError):
            pass
    hours = pulse.get("market_hours") if isinstance(pulse.get("market_hours"), dict) else {}
    if hours.get("minutes_to_open") is not None:
        try:
            return max(0, int(float(hours["minutes_to_open"])))
        except (TypeError, ValueError):
            pass
    status = str(
        sess.get("status")
        or getattr(world, "session_status", None)
        or ""
    ).lower()
    if status not in ("premarket", "closed", "postmarket"):
        return None
    try:
        from abcxauto.marketdata.market_hours import get_session_info

        info = get_session_info()
        mins = info.get("minutes_to_open")
        if mins is not None:
            return max(0, int(float(mins)))
    except Exception:
        pass
    return None


def day_facts(world: Any, scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Session forest: IBKR day, open uPnL, NL vs start minus model. Not one number."""
    sc = scorecard if isinstance(scorecard, dict) else {}
    conc = concentration(getattr(world, "positions", None))
    risk_pct = None
    try:
        risk_pct = float(getattr(get_config(), "max_risk_per_trade_pct", None) or 0) or None
    except (TypeError, ValueError):
        risk_pct = None
    daily = getattr(world, "daily_pnl", None)
    halt = {}
    try:
        from abcxauto.book import clerk_halt_facts

        halt = clerk_halt_facts(
            getattr(world, "net_liquidation", None),
            daily,
        )
    except Exception:
        halt = {}
    lot_lasts = format_lot_lasts(world)
    working_exits = format_working_exits(
        getattr(world, "open_orders", None),
        getattr(world, "positions", None),
    )
    sq = getattr(world, "stop_qty_fact", None)
    if isinstance(sq, dict) and sq and working_exits:
        match = sq.get("match")
        if match is True:
            working_exits += " stop_qty=match"
        elif match is False:
            working_exits += " stop_qty=mismatch"
    candle_source = str(getattr(world, "candle_source", None) or "") or "none"
    nl = getattr(world, "net_liquidation", None)
    open_upnl = open_upnl_of(getattr(world, "positions", None))
    edge_usd = sc.get("edge_usd")
    model_cost = sc.get("model_cost_usd")
    # Current NL is the denominator for pct_of_nl siblings (keep $ fields).
    daily_pct = pct_of_nl(daily, nl)
    # Prefer book.daily_pnl_pct when world already computed it.
    book = getattr(world, "book", None) if isinstance(getattr(world, "book", None), dict) else {}
    if book.get("daily_pnl_pct") is not None:
        try:
            daily_pct = float(book["daily_pnl_pct"])
        except (TypeError, ValueError):
            pass
    vs_start = sc.get("book_pnl")
    halt_at = halt.get("halt_trips_at_usd")
    day_vs = halt.get("ibkr_day_vs_halt")
    floors = None
    try:
        from abcxauto.risk_gates import sizing_floors_active

        floors = bool(sizing_floors_active())
    except Exception:
        try:
            floors = bool(getattr(get_config(), "sizing_floors", False))
        except Exception:
            floors = None
    port = dict(getattr(world, "portfolio_risk", None) or {})
    cap_liq = _stamp_day_capital_liquidity(world, port, nl)
    if isinstance(cap_liq, dict):
        port["capital_liquidity"] = cap_liq
    mins_open = _minutes_to_open(world)
    pulse = getattr(world, "pulse", None) if isinstance(getattr(world, "pulse", None), dict) else {}
    sess_block = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    vol_rows = _day_vol(world)
    alarms = _wake_lot_alarms(world)
    facts: dict[str, Any] = {
        "nl": nl,
        "ibkr_daily_pnl": daily,
        "daily_pnl": daily,
        "daily_pnl_pct": daily_pct,
        "daily_pnl_pct_of_nl": daily_pct,
        "ibkr_daily_pnl_pct_of_nl": daily_pct,
        "open_upnl": open_upnl,
        "open_upnl_pct_of_nl": pct_of_nl(open_upnl, nl),
        "nl_vs_start": vs_start,
        "nl_vs_start_pct_of_nl": (
            sc.get("book_return_pct")
            if sc.get("book_return_pct") is not None
            else pct_of_nl(vs_start, nl)
        ),
        "startup_nl": sc.get("startup_cash"),
        "beating_model": sc.get("beating_model"),
        "edge_usd": edge_usd,
        "edge_pct_of_nl": pct_of_nl(edge_usd, nl),
        "edge_meaning": "nl_vs_start_minus_model",
        "book_return_pct": sc.get("book_return_pct"),
        "model_cost_usd": model_cost,
        "model_cost_pct_of_nl": pct_of_nl(model_cost, nl),
        "names": conc["names"],
        "lots": conc["lots"],
        "structures": conc["structures"],
        "by_name": conc["by_name"],
        "cloned": conc["cloned"],
        "open_lots": lot_labels(
            getattr(world, "positions", None),
            fills=getattr(world, "fills", None),
            plans=(
                list(getattr(world, "trade_plans", None) or [])
                or (
                    [getattr(world, "trade_plan")]
                    if getattr(world, "trade_plan", None)
                    else []
                )
            ),
        ),
        "mix": structure_mix(getattr(world, "positions", None)),
        "capacity": dict(getattr(world, "capacity", None) or {}),
        # Ceiling knob — not the working size. Wake prints max_risk=, not risk/trade=.
        "max_risk_per_trade_pct": risk_pct,
        "risk_per_trade_pct": risk_pct,
        "playbook": {},
        "lot_lasts": lot_lasts,
        "working_exits": working_exits,
        "halt_trips_at_usd": halt_at,
        "halt_trips_at_pct_of_nl": pct_of_nl(halt_at, nl),
        "ibkr_day_vs_halt": day_vs,
        "ibkr_day_vs_halt_pct_of_nl": pct_of_nl(day_vs, nl),
        "clerk_halted": halt.get("clerk_halted"),
        "candle_source": candle_source,
        "vol": vol_rows,
        "vol_bit": _vol_wake_bit(vol_rows),
        "session_cap": _session_cap_day(world),
        "stop_dist": alarms.get("stop_dist"),
        "working_order_missing": alarms.get("working_order_missing"),
        "sizing_floors": floors,
        # Soft concentration / liquidity % of NL (from WorldState._portfolio_risk).
        "portfolio_risk": port,
        "exposure": port.get("exposure"),
        "capital_liquidity": cap_liq,
        "buying_power_usd": _real_float(
            port.get("buying_power_usd") if isinstance(port, dict) else None
        ),
        "cash_only": bool(getattr(get_config(), "cash_only", True)),
        "minutes_to_open": mins_open,
        "countdown_to": sess_block.get("countdown_to"),
        "countdown_human": sess_block.get("countdown_human"),
        "tradable_now": pulse.get("tradable_now"),
    }
    # Book fact, never a refuse. Unpriceable lots come back "unknown".
    #   {"symbol": {SYM: {"usd", "pct"} | {"usd": "unknown"}}, "underlying": {...}}
    try:
        from abcxauto.risk_gates import defined_risk_concentration

        max_loss = defined_risk_concentration(
            attach_covering_last_stops(
                getattr(world, "positions", None),
                getattr(world, "open_orders", None),
            ),
            nl,
        )
        if isinstance(max_loss, dict):
            facts["defined_risk_concentration"] = max_loss
    except Exception:
        pass
    # Same allocation bag the book tool paints — wake needs the liquidity
    # tail on continued chats when prior tool rows are omitted.
    try:
        cash = _total_cash_in(cap_liq)
        if cash is None:
            cash = _total_cash_in(port)
        alloc = allocation_facts(
            list(getattr(world, "positions", None) or []),
            net_liq=nl,
            total_cash=cash,
            quotes=getattr(world, "ibkr_live_quotes", None),
            orders=list(getattr(world, "open_orders", None) or []),
        )
        if isinstance(alloc, dict) and (
            alloc.get("lots")
            or alloc.get("cash_pct_nl") is not None
            or alloc.get("liquidity")
            or alloc.get("risk_to_stop_usd") is not None
        ):
            facts["allocation"] = alloc
    except Exception:
        pass
    facts["div_positions"] = _compact_div_positions(getattr(world, "positions", None))
    return facts


def _compact_div_positions(positions: Any) -> list[dict[str, Any]]:
    """Lots the diversification page can weigh. Cash rows dropped."""
    out: list[dict[str, Any]] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sym = str(pos.get("symbol") or "").strip()
        if not sym:
            continue
        sec = str(pos.get("secType") or pos.get("sec_type") or "STK")
        if sec.upper() in ("CASH", "BAL", "MONEY"):
            continue
        qty = pos.get("quantity")
        if qty is None:
            qty = pos.get("position")
        row: dict[str, Any] = {"symbol": sym, "secType": sec, "quantity": qty}
        for key in ("marketValue", "market_value", "right", "underlying", "underSymbol"):
            if pos.get(key) not in (None, ""):
                row[key] = pos.get(key)
        out.append(row)
    return out


def _session_cap_day(world: Any) -> dict[str, Any]:
    """Looks/tokens left this session. Empty when the counter is dark."""
    try:
        from abcxauto.session_caps import usage

        session = str(getattr(world, "session_status", "") or "")
        u = usage(session)
    except Exception:
        return {}
    if not isinstance(u, dict):
        return {}
    return {
        "looks_left": u.get("looks_left"),
        "tokens_left": u.get("tokens_left"),
        "look_cap": u.get("look_cap"),
        "token_cap": u.get("token_cap"),
        "hit": u.get("hit"),
    }


def _order_stop_px(order: dict[str, Any]) -> float | None:
    for key in ("aux_price", "auxPrice", "stop_price", "stopPrice", "stop"):
        raw = order.get(key)
        if raw in (None, "", 0, 0.0, "0"):
            continue
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


def _covering_last_stop_px(
    position: dict[str, Any],
    orders: list[dict[str, Any]] | None,
) -> float | None:
    """Working last-stop that covers this lot. Same join as ``_wake_lot_alarms``."""
    try:
        from abcxauto.broker.order_types import is_stop_order
        from abcxauto.monitor import covering_exits
    except Exception:
        return None
    exits = covering_exits(position, orders)
    stops = [
        o
        for o in exits
        if is_stop_order(str(o.get("order_type") or o.get("orderType") or ""))
    ]
    for o in stops or exits:
        stop_px = _order_stop_px(o)
        if stop_px is not None:
            return stop_px
    return None


def attach_covering_last_stops(
    positions: Any,
    open_orders: Any = None,
) -> list[Any]:
    """Copy lots and paint covering last-stop (and entry/avg if present).

    A filled market_bracket leaves the IBKR position row without
    stop/entry; the working STP lives on ``open_orders``. Display only —
    does not mutate the live book.
    """
    orders = [o for o in (open_orders or []) if isinstance(o, dict)]
    out: list[Any] = []
    for p in positions or []:
        if not isinstance(p, dict):
            out.append(p)
            continue
        row = dict(p)
        stop_px = _covering_last_stop_px(row, orders)
        if stop_px is not None:
            if row.get("stop") in (None, "", 0, 0.0, "0"):
                row["stop"] = stop_px
            if row.get("stop_price") in (None, "", 0, 0.0, "0"):
                row["stop_price"] = stop_px
        if row.get("entry_price") in (None, ""):
            avg = position_avg_facts(row).get("avg")
            if avg is not None:
                row["entry_price"] = avg
        if row.get("avg") in (None, ""):
            avg = position_avg_facts(row).get("avg")
            if avg is not None:
                row["avg"] = avg
        out.append(row)
    return out


def _lot_last_px(pos: dict[str, Any], quotes: dict[str, Any]) -> float | None:
    row = compact_position(pos, extra=True)
    for raw in (row.get("mkt"), pos.get("market_price"), pos.get("marketPrice"), pos.get("last")):
        try:
            px = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            px = None
        if px is not None and px > 0:
            return px
    sym = str(pos.get("symbol") or "").upper().strip()
    if not sym:
        return None
    hit = quotes.get(sym)
    if isinstance(hit, dict):
        hit = hit.get("last") if hit.get("last") is not None else hit.get("mid")
    try:
        px = float(hit) if hit is not None else None
    except (TypeError, ValueError):
        return None
    return px if px is not None and px > 0 else None


def _wake_lot_alarms(world: Any) -> dict[str, Any]:
    """Distance to written stop + lots with no covering working order."""
    positions = list(getattr(world, "positions", None) or [])
    orders = list(getattr(world, "open_orders", None) or [])
    quotes = getattr(world, "ibkr_live_quotes", None) or {}
    if not isinstance(quotes, dict):
        quotes = {}
    try:
        from abcxauto.monitor import covering_exits
    except Exception:
        return {"stop_dist": None, "working_order_missing": []}
    missing: list[str] = []
    closest: dict[str, Any] | None = None
    for p in positions:
        if not isinstance(p, dict):
            continue
        qty = _row_signed_qty(p)
        if abs(qty) < 1e-9:
            continue
        ident = lot_ident(p)
        exits = covering_exits(p, orders)
        if not exits:
            missing.append(ident)
        last = _lot_last_px(p, quotes)
        stop_px = _covering_last_stop_px(p, orders)
        if last is None or stop_px is None:
            continue
        dist = abs(float(last) - float(stop_px))
        row = {
            "ident": ident,
            "last": last,
            "stop": stop_px,
            "dist": round(dist, 4),
        }
        if closest is None or dist < float(closest.get("dist") or 1e18):
            closest = row
    return {"stop_dist": closest, "working_order_missing": missing}


def _day_vol(world: Any) -> list[dict[str, Any]]:
    """Clipped this-look vol. Empty when nothing is taped."""
    try:
        from abcxauto.vol_fact import clip_vol_facts

        return clip_vol_facts(getattr(world, "vol_facts", None))
    except Exception:
        return []


def _vol_wake_bit(rows: Any) -> str:
    try:
        from abcxauto.vol_fact import wake_vol_bit

        return wake_vol_bit(rows)
    except Exception:
        return ""


def _wake_has_live_lots(day: dict[str, Any] | None) -> bool:
    """Journal/IBKR lots with nonzero qty. Residue JSON and last_turn.flat are not the book."""
    d = day if isinstance(day, dict) else {}
    for ident in d.get("open_lots") or []:
        if str(ident).strip():
            return True
    try:
        if int(d.get("lots") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    mix = d.get("mix") if isinstance(d.get("mix"), dict) else {}
    for v in mix.values():
        try:
            if int(v or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def trade_plan_matches_stk(plan: Any, positions: list[dict] | None) -> bool:
    """Think gets a plan only when STK qty for that symbol is live and same-side."""
    from abcxauto.trade_plan import stk_qty_for_symbol

    if plan is None:
        return False
    if isinstance(plan, dict):
        sym = str(plan.get("symbol") or "")
        direction = str(plan.get("direction") or "LONG").upper()
    else:
        sym = str(getattr(plan, "symbol", "") or "")
        direction = str(getattr(plan, "direction", "LONG") or "LONG").upper()
    qty = stk_qty_for_symbol(positions, sym)
    if abs(qty) < 1e-9:
        return False
    if direction == "SHORT":
        return qty < 0
    return qty > 0


def _ibkr_live_mark(
    snap: dict[str, Any],
    positions: list[dict],
) -> tuple[str, Any]:
    """Live mark from this snap. Empty book does not default to SPY or scan junk."""
    from abcxauto.trade_plan import book_has_risk

    quotes = snap.get("ibkr_live_quotes") or {}
    if not isinstance(quotes, dict):
        quotes = {}
    explicit = str(snap.get("ibkr_live_symbol") or "").strip()
    last_raw = snap.get("ibkr_live_last")
    empty = not book_has_risk(positions)
    if empty:
        # quote() may pin a candidate. A scan sweep must not.
        if explicit and last_raw is not None:
            return explicit, last_raw
        return "", None
    if explicit:
        sym = explicit
    elif "SPY" in quotes:
        sym = "SPY"
    else:
        sym = ""
    if last_raw is not None:
        last = last_raw
    elif sym and sym in quotes:
        last = quotes.get(sym)
    else:
        last = quotes.get("SPY")
    return str(sym), last


def _pnl_wake_bits(day: dict[str, Any]) -> str:
    """Review unit is % of NL; $ kept second only to reconstruct the book."""

    def _bit(usd: Any, pct: Any) -> str:
        if pct is not None:
            if usd is None:
                return f"{pct}% NL"
            return f"{pct}% NL (${usd})"
        if usd is None:
            return "?"
        return f"${usd}"

    dp = day.get("ibkr_daily_pnl")
    if dp is None:
        dp = day.get("daily_pnl")
    dp_pct = day.get("daily_pnl_pct_of_nl")
    if dp_pct is None:
        dp_pct = day.get("daily_pnl_pct")
    vs = day.get("nl_vs_start")
    vs_pct = day.get("nl_vs_start_pct_of_nl")
    if vs_pct is None:
        vs_pct = day.get("book_return_pct")
    return (
        f"ibkrDay={_bit(dp, dp_pct)} "
        f"haltAt={_bit(day.get('halt_trips_at_usd'), day.get('halt_trips_at_pct_of_nl'))} "
        f"openU={_bit(day.get('open_upnl'), day.get('open_upnl_pct_of_nl'))} "
        f"vsStart={_bit(vs, vs_pct)}(inception) "
        f"edgeVsModel={_bit(day.get('edge_usd'), day.get('edge_pct_of_nl'))} "
        f"cost={_bit(day.get('model_cost_usd'), day.get('model_cost_pct_of_nl'))} "
        f"beating={day.get('beating_model')}"
    )


def _portfolio_wake_bits(day: dict[str, Any]) -> str:
    """Leftover $ / cash% / deployed% plus top + liquidity tail (facts only).

    Continuation chats (previous_response_id) omit prior book tool rows, so
    cut-half / cut-all / risk-to-stop must live on this wake line — same
    dollars as ``allocation_line``, no advice wording.
    """
    exp = day.get("exposure") if isinstance(day.get("exposure"), dict) else {}
    port = day.get("portfolio_risk") if isinstance(day.get("portfolio_risk"), dict) else {}
    bits: list[str] = []
    cash_pct, deployed = cash_deployed_pct(day)
    head = _leftover_deployed_text(_leftover_usd_of(day), cash_pct, deployed)
    if head:
        bits.append(head)
    top_pct = exp.get("top_concentration_pct")
    if top_pct is None:
        top_pct = port.get("top_concentration_pct")
    top_sym = exp.get("top_symbol") or port.get("top_symbol") or ""
    if top_pct is not None:
        sym = f" {top_sym}" if top_sym else ""
        bits.append(f"top{sym}={top_pct}% NL")
    liq_tail = _allocation_liquidity_tail(day)
    if liq_tail:
        bits.append(liq_tail)
    bp = _real_float(day.get("buying_power_usd"))
    if bp is None:
        bp = _real_float(port.get("buying_power_usd"))
    if bp is not None:
        bits.append(f"buying_power={_fmt_usd_compact(bp)}")
    if "cash_only" in day:
        bits.append("cash_only=" + ("on" if day.get("cash_only") else "off"))
    return " ".join(bits)


def _lot_context_line(day: dict[str, Any]) -> str:
    """Per-lot last, stop, target, and dollars to the target. Facts only.

    Continuation drops the book tool row, so this geometry has to ride the wake.
    """
    alloc = day.get("allocation") if isinstance(day.get("allocation"), dict) else {}
    nl = _real_float(day.get("nl"))
    if nl is None:
        nl = _real_float(alloc.get("nl"))
    bits: list[str] = []
    for lot in alloc.get("lots") or []:
        if not isinstance(lot, dict):
            continue
        sym = str(lot.get("symbol") or "").strip()
        if not sym:
            continue
        qty = lot.get("qty")
        qty_bit = str(qty) if qty is not None else ""
        part = f"{sym}{qty_bit}"
        if lot.get("pct_nl") is not None:
            part += f" {lot['pct_nl']}%"
        if lot.get("last") is not None:
            part += f" {lot['last']}"
        if lot.get("uPnL") is not None:
            part += f" uPnL={lot['uPnL']}"
        if lot.get("risk_pct_nl") is not None:
            part += f" risk={lot['risk_pct_nl']}%"
        if lot.get("stop") is not None:
            part += f" stp={lot['stop']}"
        if lot.get("target") is not None:
            part += f" tgt={lot['target']}"
        to_tgt = _real_float(lot.get("to_target_usd"))
        if to_tgt is not None:
            part += f" to_tgt={_fmt_usd_compact(to_tgt)}"
            if nl is not None and nl > 0:
                part += f" {round(to_tgt / nl * 100.0, 2)}%NL"
        eff = _real_float(lot.get("eff"))
        if eff is not None:
            part += f" eff={eff:.4f}"
        bits.append(part)
    return " | ".join(bits)


def _allocation_rank_line(day: dict[str, Any]) -> str:
    """Open lots, lowest target-dollars per deployed dollar first.

    ``eff`` is to-target dollars divided by market value. A missing target
    sorts after any lot that has one. Not a buy list and not a send.
    """
    alloc = day.get("allocation") if isinstance(day.get("allocation"), dict) else {}
    ranked: list[tuple[bool, float, float, int, dict[str, Any]]] = []
    for i, lot in enumerate(alloc.get("lots") or []):
        if not isinstance(lot, dict) or not str(lot.get("symbol") or "").strip():
            continue
        eff = _real_float(lot.get("eff"))
        cap = _real_float(lot.get("pct_nl")) or 0.0
        ranked.append((eff is None, eff if eff is not None else 0.0, -cap, i, lot))
    ranked.sort()
    bits: list[str] = []
    nl = _real_float(day.get("nl"))
    if nl is None:
        nl = _real_float(alloc.get("nl"))
    for _missing, _eff, _cap, _i, lot in ranked:
        sym = str(lot.get("symbol") or "").strip()
        part = sym
        if lot.get("pct_nl") is not None:
            part += f" cap={lot['pct_nl']}%NL"
        if lot.get("risk_pct_nl") is not None:
            part += f" risk={lot['risk_pct_nl']}%NL"
        to_tgt = _real_float(lot.get("to_target_usd"))
        if to_tgt is not None:
            part += f" to_tgt={_fmt_usd_compact(to_tgt)}"
            if nl is not None and nl > 0:
                part += f" {round(to_tgt / nl * 100.0, 2)}%NL"
        eff = _real_float(lot.get("eff"))
        if eff is not None:
            part += f" eff={eff:.4f}"
        bits.append(part)
    return " | ".join(bits)


def _halt_is_tight(day: dict[str, Any]) -> bool:
    if day.get("clerk_halted"):
        return True
    try:
        trips = float(day["halt_trips_at_usd"])
        vs = float(day["ibkr_day_vs_halt"])
    except (TypeError, ValueError, KeyError):
        return False
    if trips >= 0:
        return vs <= 0
    budget = abs(trips)
    if budget <= 0:
        return vs <= 0
    return vs <= 0 or (vs / budget) <= 0.25


def _session_cap_line(cap: Any) -> str:
    if not isinstance(cap, dict) or not cap:
        return ""
    looks_left = cap.get("looks_left")
    tokens_left = cap.get("tokens_left")
    bits: list[str] = []
    if looks_left is not None:
        bits.append(f"{int(looks_left)} looks")
    if tokens_left is not None:
        bits.append(f"{int(tokens_left)} tokens")
    if not bits:
        return ""
    return "session_cap remaining=" + ", ".join(bits)


def _session_cap_from_counter(session: str) -> dict[str, Any]:
    try:
        from abcxauto.session_caps import usage

        u = usage(session)
    except Exception:
        return {}
    if not isinstance(u, dict):
        return {}
    return {
        "looks_left": u.get("looks_left"),
        "tokens_left": u.get("tokens_left"),
        "look_cap": u.get("look_cap"),
        "token_cap": u.get("token_cap"),
        "hit": u.get("hit"),
    }


# Leading-line prefix so a protected stop / missing working order cannot
# parse as an order ticket. Not a hold-law. unprotected= stays bare.
WAKE_FACT_PREFIX = "fact:"
# US equity tick. Last-tick last/dist noise is not a stop move.
STOP_DIST_TICK = 0.01
_CLOSEST_STOP_RE = re.compile(
    r"closest_stop\s+(?P<ident>.+?)\s+dist=(?P<dist>[-+]?(?:\d+\.?\d*|\.\d+))"
    r"\s+stop=(?P<stop>[-+]?(?:\d+\.?\d*|\.\d+))"
    r"(?:\s+last=(?P<last>[-+]?(?:\d+\.?\d*|\.\d+)))?",
    re.IGNORECASE,
)


def _desk_fact_line(body: str) -> str:
    bit = str(body or "").strip()
    if not bit:
        return ""
    return f"{WAKE_FACT_PREFIX} {bit}"


def wake_fact_line(text: str) -> str:
    """First line of a wake / poke. Empty when there is no text."""
    return str(text or "").splitlines()[0].strip() if text else ""


def _lead_body(text: str) -> str:
    """First-line body: drop ``fact:`` prefix and a trailing period."""
    bit = wake_fact_line(text)
    if not bit:
        return ""
    bit = bit.rstrip(".").strip()
    if bit.lower().startswith("fact:"):
        bit = bit.split(":", 1)[1].strip()
    return bit


def _csv_identity(raw: str) -> frozenset[str]:
    """Membership of a comma-separated lead (order and string form do not matter)."""
    return frozenset(x.strip() for x in str(raw or "").split(",") if x.strip())


def parse_desk_fact(raw: Any) -> dict[str, Any] | None:
    """Lead-fact identity: closest_stop tick, missing-order set, unprotected list.

    None when the first line is not a collapsible lead (session=, session_cap,
    halt). Identity is the set/list/tick, not the poke string.
    """
    body = _lead_body(str(raw or ""))
    if not body:
        return None
    stop = parse_closest_stop(raw)
    if stop is not None:
        return {"kind": "closest_stop", "stop": stop}
    low = body.lower()
    if low.startswith("working_order_missing"):
        rest = body.split(None, 1)[1] if " " in body else ""
        items = _csv_identity(rest)
        if not items:
            return None
        return {"kind": "working_order_missing", "items": items}
    if low.startswith("unprotected="):
        items = _csv_identity(body.split("=", 1)[1])
        items = frozenset(x for x in items if x.lower() != "none")
        if not items:
            return None
        return {"kind": "unprotected", "items": items}
    return None


def parse_closest_stop(raw: Any) -> dict[str, Any] | None:
    """ident + stop from a stop_dist row or a ``fact: closest_stop`` line."""
    if isinstance(raw, dict):
        ident = str(raw.get("ident") or "").strip()
        if not ident:
            return None
        try:
            stop = float(raw.get("stop"))
        except (TypeError, ValueError):
            return None
        row: dict[str, Any] = {"ident": ident, "stop": stop}
        for key in ("dist", "last"):
            try:
                if raw.get(key) is not None:
                    row[key] = float(raw[key])
            except (TypeError, ValueError):
                continue
        return row
    bit = wake_fact_line(str(raw or ""))
    if not bit:
        return None
    m = _CLOSEST_STOP_RE.search(bit)
    if not m:
        return None
    ident = str(m.group("ident") or "").strip()
    if not ident:
        return None
    try:
        stop = float(m.group("stop"))
    except (TypeError, ValueError):
        return None
    row = {"ident": ident, "stop": stop}
    for key in ("dist", "last"):
        try:
            if m.group(key) is not None:
                row[key] = float(m.group(key))
        except (TypeError, ValueError, IndexError):
            continue
    return row


def closest_stop_moved_more_than_a_tick(
    prev: Any,
    cur: Any,
    *,
    tick: float = STOP_DIST_TICK,
) -> bool:
    """True when ident changed or the stop price moved more than a tick.

    Last-tick last/dist noise is not a move. No previous closest_stop is a
    move so the first fact still lands. Unparseable current is a move so we
    never swallow a real poke.
    """
    b = parse_closest_stop(cur)
    if b is None:
        return True
    a = parse_closest_stop(prev)
    if a is None:
        return True
    if a.get("ident") != b.get("ident"):
        return True
    try:
        step = abs(float(tick))
    except (TypeError, ValueError):
        step = STOP_DIST_TICK
    if step <= 0:
        step = STOP_DIST_TICK
    try:
        return abs(float(b["stop"]) - float(a["stop"])) > step
    except (TypeError, ValueError):
        return True


def desk_fact_is_duplicate(prev: Any, cur: Any) -> bool:
    """True when the collapsible lead-fact identity did not move.

    closest_stop: same ident, stop within a tick (last/dist noise is not a
    move). working_order_missing: same SET of labels. unprotected: same
    LIST of names. First occurrence still lands. session= / session_cap /
    halt are not collapsed.
    """
    b = parse_desk_fact(cur)
    if b is None:
        return False
    a = parse_desk_fact(prev)
    if a is None:
        return False
    if a.get("kind") != b.get("kind"):
        return False
    kind = str(a.get("kind") or "")
    if kind == "closest_stop":
        return not closest_stop_moved_more_than_a_tick(prev, cur)
    if kind in ("working_order_missing", "unprotected"):
        return a.get("items") == b.get("items")
    return False


def desk_fact_changed(prev: Any, cur: Any) -> bool:
    """True when a collapsible lead fact actually moved (first occurrence counts).

    Unparseable leads are not a change — stay-up sits; fill / order_change
    still poke on their own.
    """
    if parse_desk_fact(cur) is None:
        return False
    return not desk_fact_is_duplicate(prev, cur)


def omit_duplicate_fact_lead(prev: Any, text: str) -> str:
    """Keep one identical collapsible lead line; drop a later copy."""
    raw = str(text or "")
    if not desk_fact_is_duplicate(prev, raw):
        return raw
    lines = raw.splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[1:]).strip()


def _real_float(raw: Any) -> float | None:
    """Coerce a number. Missing / unreadable is None — never invented 0."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:
        return None
    return val


def _total_cash_in(bag: Any) -> float | None:
    """Real total_cash on a bag or its capital_liquidity / portfolio_risk."""
    if not isinstance(bag, dict):
        return None
    hit = _real_float(bag.get("total_cash"))
    if hit is not None:
        return hit
    cap = bag.get("capital_liquidity")
    if isinstance(cap, dict):
        hit = _real_float(cap.get("total_cash"))
        if hit is not None:
            return hit
    port = bag.get("portfolio_risk")
    if isinstance(port, dict) and port is not bag:
        return _total_cash_in(port)
    return None


def _leftover_usd_of(bag: dict[str, Any] | None) -> float | None:
    src = bag if isinstance(bag, dict) else {}
    alloc = src.get("allocation") if isinstance(src.get("allocation"), dict) else {}
    hit = _real_float(alloc.get("leftover_usd"))
    if hit is not None:
        return hit
    hit = _real_float(src.get("leftover_usd"))
    if hit is not None:
        return hit
    return _total_cash_in(src)


def _day_pnl_of(bag: dict[str, Any] | None) -> float | None:
    src = bag if isinstance(bag, dict) else {}
    hit = _real_float(src.get("ibkr_daily_pnl"))
    if hit is not None:
        return hit
    return _real_float(src.get("daily_pnl"))


def _fmt_usd_compact(val: float) -> str:
    n = round(float(val), 2)
    if abs(n - round(n)) < 1e-9:
        return f"${int(round(n))}"
    return f"${n:.2f}"


def _fmt_pnl_compact(val: float) -> str:
    n = round(float(val), 2)
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def _leftover_deployed_text(
    leftover_usd: float | None,
    cash_pct: float | None,
    deployed_pct: float | None,
    day_pnl: float | None = None,
) -> str:
    """Comparable leftover $ / % vs deployed %."""
    if leftover_usd is None and cash_pct is None and deployed_pct is None:
        return ""
    bits: list[str] = []
    if leftover_usd is not None or cash_pct is not None:
        bits.append("leftover")
        if leftover_usd is not None:
            bits.append(_fmt_usd_compact(leftover_usd))
        if cash_pct is not None:
            bits.append(f"cash={cash_pct}%")
    if deployed_pct is not None:
        bits.append(f"deployed={deployed_pct}%")
    if day_pnl is not None:
        bits.append(f"day={_fmt_pnl_compact(day_pnl)} on deployed")
    return " ".join(bits)


def cash_deployed_pct(bag: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """cash_pct_nl, deployed_pct_nl from allocation or capital_liquidity."""
    src = bag if isinstance(bag, dict) else {}
    cap = src.get("capital_liquidity") if isinstance(src.get("capital_liquidity"), dict) else {}
    if not cap:
        port = src.get("portfolio_risk") if isinstance(src.get("portfolio_risk"), dict) else {}
        cap = port.get("capital_liquidity") if isinstance(port.get("capital_liquidity"), dict) else {}
    alloc = src.get("allocation") if isinstance(src.get("allocation"), dict) else {}
    cash = cap.get("cash_pct_nl")
    if cash is None:
        cash = alloc.get("cash_pct_nl")
    dep = cap.get("deployed_long_pct_nl")
    if dep is None:
        dep = alloc.get("deployed_pct_nl")
    return _real_float(cash), _real_float(dep)


def idle_cash_line(bag: dict[str, Any] | None) -> str:
    """One fact when leftover cash is larger than deployed lots."""
    cash, dep = cash_deployed_pct(bag)
    if cash is None or dep is None or cash <= dep:
        return ""
    return _leftover_deployed_text(_leftover_usd_of(bag), cash, dep, _day_pnl_of(bag))


def leftover_dominates(bag: dict[str, Any] | None) -> bool:
    """True when leftover cash is larger than deployed lots."""
    return bool(idle_cash_line(bag))


def book_still_working(
    bag: dict[str, Any] | None,
    positions: Any = None,
) -> bool:
    """True when a lot is on. The next look still has work."""
    _, dep = cash_deployed_pct(bag)
    if dep is not None and dep > 0:
        return True
    if isinstance(positions, list):
        return any(isinstance(row, dict) and row for row in positions)
    return False


def leftover_relook_due(
    bag: dict[str, Any] | None,
    *,
    session: str = "",
    last_look_mono: float | None = None,
    now_mono: float | None = None,
    researched: bool = False,
) -> bool:
    """RTH leftover > deployed, last look sat long enough. Not a general chair."""
    if str(session or "").strip().lower() != "regular":
        return False
    if not leftover_dominates(bag):
        return False
    if last_look_mono is None or now_mono is None:
        return False
    try:
        if researched:
            from abcxauto.park_clock import researched_leftover_relook_s

            need = float(researched_leftover_relook_s())
        else:
            from abcxauto.park_clock import leftover_relook_s

            need = float(leftover_relook_s())
    except Exception:
        need = 15 * 60.0 if researched else 90.0
    return (float(now_mono) - float(last_look_mono)) >= need


def look_gathered_research(payload: dict[str, Any] | None) -> bool:
    """True when a non-book name has this-look structure and read.

    Held positions do not count. Scan-only looks (no structure+read on a
    non-book name) return False.
    """
    if not isinstance(payload, dict) or not payload:
        return False
    from abcxauto.desk_mode import _read_this_look, _structure_this_look

    snap: dict[str, Any] = {
        "session_range": (
            payload.get("session_range")
            if isinstance(payload.get("session_range"), dict)
            else {}
        ),
        "news_items": (
            list(payload.get("news_items") or [])
            if isinstance(payload.get("news_items"), list)
            else []
        ),
        "research_web": (
            payload.get("research_web")
            if isinstance(payload.get("research_web"), dict)
            else {}
        ),
        "option_facts": payload.get("option_facts"),
        "_research_bag": (
            payload.get("_research_bag")
            if isinstance(payload.get("_research_bag"), dict)
            else {}
        ),
    }
    held: set[str] = set()
    for row in payload.get("positions") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("symbol") or "").upper().strip()
        if name:
            held.add(name)
    candidates: set[str] = set()
    store = snap.get("session_range")
    if isinstance(store, dict):
        for key in store:
            name = str(key or "").upper().strip()
            if name:
                candidates.add(name)
    for it in snap.get("news_items") or []:
        if not isinstance(it, dict):
            continue
        for key in ("symbol", "ticker", "underlying"):
            name = str(it.get(key) or "").upper().strip()
            if name:
                candidates.add(name)
    facts = snap.get("option_facts")
    if isinstance(facts, dict):
        facts = facts.get("facts")
    if isinstance(facts, list):
        for row in facts:
            if not isinstance(row, dict):
                continue
            for key in ("symbol", "ticker", "underlying"):
                name = str(row.get(key) or "").upper().strip()
                if name:
                    candidates.add(name)
    bag = snap.get("_research_bag")
    if isinstance(bag, dict):
        for raw in bag.get("symbols") or []:
            name = str(raw or "").upper().strip()
            if name:
                candidates.add(name)
    for sym in candidates:
        if sym in held:
            continue
        if _structure_this_look(snap, sym) and _read_this_look(snap, sym, ""):
            return True
    return False


def worst_wake_fact(
    *,
    unprotected: list[str] | None,
    day: dict[str, Any] | None = None,
    session: str = "",
) -> str:
    """One leading fact: unprotected, idle cash, stop distance, missing order, halt, cap.

    Not a strategy menu. Unprotected STK already fail-closes — publish it first.
    """
    day = day if isinstance(day, dict) else {}
    unprot = [str(x).strip() for x in (unprotected or []) if str(x).strip()]
    if unprot:
        return "unprotected=" + ",".join(unprot)
    idle = idle_cash_line(day)
    if idle:
        return _desk_fact_line(idle)
    stop = day.get("stop_dist") if isinstance(day.get("stop_dist"), dict) else None
    if stop and stop.get("ident"):
        ident = stop.get("ident")
        dist = stop.get("dist")
        px = stop.get("stop")
        last = stop.get("last")
        if dist is not None and px is not None:
            bit = f"closest_stop {ident} dist={dist} stop={px}"
            if last is not None:
                bit += f" last={last}"
            ctx = _lot_context_line(day)
            if ctx:
                bit += " | " + ctx
            bp = _real_float(day.get("buying_power_usd"))
            if bp is None:
                port = (
                    day.get("portfolio_risk")
                    if isinstance(day.get("portfolio_risk"), dict)
                    else {}
                )
                bp = _real_float(port.get("buying_power_usd"))
            if bp is not None:
                bit += f" buying_power={_fmt_usd_compact(bp)}"
            if "cash_only" in day:
                bit += " cash_only=" + ("on" if day.get("cash_only") else "off")
            return _desk_fact_line(bit)
    missing = [
        str(x).strip()
        for x in (day.get("working_order_missing") or [])
        if str(x).strip()
    ]
    if missing:
        return _desk_fact_line("working_order_missing " + ",".join(missing[:6]))
    if _halt_is_tight(day):
        dp = day.get("ibkr_daily_pnl")
        if dp is None:
            dp = day.get("daily_pnl")
        halt_at = day.get("halt_trips_at_usd")
        vs = day.get("ibkr_day_vs_halt")
        bits = [f"ibkrDay={dp}", f"vs haltAt={halt_at}"]
        if vs is not None:
            bits.append(f"room={vs}")
        return " ".join(str(b) for b in bits)
    cap = day.get("session_cap")
    if not isinstance(cap, dict) or not cap:
        cap = _session_cap_from_counter(session)
    return _session_cap_line(cap)


def format_wake(
    *,
    cycle: int,
    session: str,
    flat: bool,
    unprotected: list[str] | None,
    ibkr_up: bool,
    day: dict[str, Any] | None = None,
) -> str:
    """Desk brief. Live book facts; no tape= and no watch=. Scan is a tool.

    ``cycle`` is journal/logs. Not painted on the brief.
    """
    _ = cycle
    unprot = ",".join(unprotected) if unprotected else "none"
    day = day if isinstance(day, dict) else {}
    lots = worst_fact_open_lots(day)
    lot_s = ",".join(lots) if lots else ""
    mix_s = format_mix(day.get("mix") if isinstance(day.get("mix"), dict) else {})
    cap = day.get("capacity") if isinstance(day.get("capacity"), dict) else {}
    open_n = cap.get("open_count", cap.get("open"))
    max_n = cap.get("max_open_positions", cap.get("max"))
    ev = None
    try:
        from abcxauto.park_clock import last_wake

        ev = last_wake()
    except Exception:
        ev = None
    pnl_bits = _pnl_wake_bits(day)
    port_bits = _portfolio_wake_bits(day)
    live_lots = _wake_has_live_lots(day)
    # Lots are the book. last_turn.flat / leftover prev= are not.
    paint_flat = False if live_lots else bool(flat)
    risk = day.get("max_risk_per_trade_pct")
    if risk is None:
        risk = day.get("risk_per_trade_pct")
    floors = day.get("sizing_floors")
    floors_bit = ""
    if floors is True:
        floors_bit = " floors=on"
    elif floors is False:
        floors_bit = " floors=off"
    parts = [
        f"session={session} flat={paint_flat} "
        f"unprotected={unprot} ibkr={'up' if ibkr_up else 'down'}.",
    ]
    mins = day.get("minutes_to_open")
    if mins is not None and str(session or "").lower() in (
        "premarket",
        "closed",
        "postmarket",
    ):
        parts.append(f"minutes_to_open={mins}.")
    if day:
        # max_risk= is the self_tune ceiling, not the ticket size.
        # open=N; denominator only when Grok/operator set a positive mop.
        nl = day.get("nl")
        if nl is None:
            nl = cap.get("nl")
        nl_bit = f" nl={nl}" if nl not in (None, "") else ""
        try:
            max_i = int(max_n) if max_n not in (None, "") else 0
        except (TypeError, ValueError):
            max_i = 0
        open_bit = f"open={open_n}/{max_i}" if max_i > 0 else f"open={open_n}"
        parts.append(
            f"names={day.get('names')} lots={day.get('lots')} "
            f"{pnl_bits} "
            f"max_risk={risk}%{floors_bit} {open_bit}{nl_bit}."
        )
        if port_bits:
            parts.append(f"{port_bits}.")
        lot_ctx = _lot_context_line(day)
        if lot_ctx:
            parts.append(f"{lot_ctx}.")
        alloc_rank = _allocation_rank_line(day)
        if alloc_rank:
            parts.append(f"alloc {alloc_rank}.")
        if lot_s:
            parts.append(f"open_lots={lot_s}.")
        if (
            str(session or "").lower() == "regular"
            and day.get("countdown_to") == "close"
            and day.get("countdown_human")
        ):
            parts.append(f"close_in={day.get('countdown_human')}.")
        tradable = day.get("tradable_now") if isinstance(day.get("tradable_now"), dict) else {}
        if tradable.get("equity_rth") is False:
            parts.append("equity_rth=off.")
        if day.get("lot_lasts"):
            parts.append(f"{day.get('lot_lasts')}.")
        if day.get("working_exits"):
            parts.append(f"exits={day.get('working_exits')}.")
        src = str(day.get("candle_source") or "").strip()
        if src and src not in ("none",):
            parts.append(f"candles={src}.")
        vol_bit = str(day.get("vol_bit") or "").strip()
        if not vol_bit:
            vol_bit = _vol_wake_bit(day.get("vol"))
        if vol_bit:
            parts.append(f"vol={vol_bit}.")
        if mix_s:
            parts.append(f"mix={mix_s}.")
        if ev is not None:
            parts.append(f"event={ev.kind} {ev.detail}.".strip())
        # Preformatted day lines from other modules — facts only, no invented RS.
        body_so_far = " ".join(parts)
        alloc_line = day.get("alloc_line")
        if isinstance(alloc_line, str):
            al = alloc_line.strip()
            if al:
                check = al.rstrip(".")
                if check and check not in body_so_far:
                    parts.append(al if al.endswith(".") else f"{al}.")
        research_line = day.get("research_line")
        if isinstance(research_line, str):
            rl = research_line.strip()
            if rl:
                parts.append(rl if rl.endswith(".") else f"{rl}.")
        spend_line = day.get("spend_line")
        if isinstance(spend_line, str) and spend_line.strip():
            sl = spend_line.strip()
            parts.append(sl if sl.endswith(".") else f"{sl}.")
        else:
            spend_usd = _real_float(day.get("session_spend_usd"))
            if spend_usd is not None:
                parts.append(f"spend session={_fmt_usd_compact(spend_usd)}.")
            elif day.get("session_spend_unknown") is True:
                parts.append("spend session=unknown.")
        # leftover say / prev= / unused= stay off wake.
    # Desk facts only — no trailing "send." (Grok reads that as an operator command).
    body = " ".join(parts)
    try:
        from abcxauto.desk_mode import desk_mode_wake_bit

        full = False
        if isinstance(day, dict) and day.get("research_brief_full") is True:
            full = True
        mode_bit = desk_mode_wake_bit(session, rth_full=full)
        if mode_bit:
            body = f"{body} {mode_bit}".strip()
    except Exception:
        logger.debug("desk mode wake bit failed", exc_info=True)
    try:
        from abcxauto.memory import memory_wake_bit

        note_bit = memory_wake_bit()
        if note_bit:
            body = f"{body} {note_bit}".strip()
    except Exception:
        logger.debug("memory wake bit failed", exc_info=True)
    # pace_line / work_line lead the desk (facts only — no sell/rotate/should/must).
    head: list[str] = []
    pace_line = day.get("pace_line") if isinstance(day, dict) else None
    if isinstance(pace_line, str) and pace_line.strip():
        pl = pace_line.strip()
        head.append(pl if pl.endswith(".") else f"{pl}.")
    work_line = day.get("work_line") if isinstance(day, dict) else None
    if isinstance(work_line, str) and work_line.strip():
        wl = work_line.strip()
        head.append(wl if wl.endswith(".") else f"{wl}.")
    lead = worst_wake_fact(unprotected=unprotected, day=day, session=session)
    if lead:
        if not lead.endswith("."):
            lead = lead + "."
        head.append(lead)
    if isinstance(day, dict) and day.get("book_stale") is True:
        body = f"{body} book_stale".strip()
    pages = _directional_pages(day if isinstance(day, dict) else {})
    if pages:
        body = f"{body}\n{pages}".strip() if body else pages
    if head:
        return "\n".join(head) + (f"\n{body}" if body else "")
    return body


def _directional_pages(day: dict[str, Any]) -> str:
    """Diversification, size, and order-type facts. Inputs filled, answers blank."""
    if not isinstance(day.get("allocation"), dict):
        return ""
    alloc = day["allocation"]
    lots = [r for r in (alloc.get("lots") or []) if isinstance(r, dict)]
    nl = _real_float(alloc.get("nl"))
    if nl is None:
        nl = _real_float(day.get("nl"))
    cash = _real_float(alloc.get("leftover_usd"))
    raw_pos = day.get("div_positions")
    has_mv = False
    if isinstance(raw_pos, list):
        for pos in raw_pos:
            if not isinstance(pos, dict):
                continue
            if _real_float(pos.get("marketValue") or pos.get("market_value")):
                has_mv = True
                break
    if has_mv:
        positions = [p for p in raw_pos if isinstance(p, dict)]
    else:
        positions = []
        for lot in lots:
            sym = str(lot.get("symbol") or "").strip()
            if not sym:
                continue
            qty = lot.get("qty")
            last = _real_float(lot.get("last"))
            row: dict[str, Any] = {
                "symbol": sym,
                "secType": str(lot.get("secType") or lot.get("sec_type") or "STK"),
                "quantity": qty if qty is not None else 0,
            }
            mv = _real_float(lot.get("marketValue"))
            if mv is None and last is not None and qty is not None:
                try:
                    mv = abs(float(qty)) * last
                except (TypeError, ValueError):
                    mv = None
            if mv is not None:
                row["marketValue"] = mv
            positions.append(row)
    blocks: list[str] = []
    try:
        from abcxauto.div_fact import diversification_facts, format_diversification

        betas = day.get("betas") if isinstance(day.get("betas"), dict) else None
        heat = day.get("heat_groups") if isinstance(day.get("heat_groups"), list) else None
        facts = diversification_facts(
            positions,
            net_liq=nl,
            total_cash=cash,
            betas=betas,
            heat_groups=heat,
        )
        board = day.get("board_line")
        div = format_diversification(
            facts, board_line=board if isinstance(board, str) else None
        )
        if div.strip():
            blocks.append(div.strip())
    except Exception:
        logger.debug("diversification page failed", exc_info=True)
    price = stop = target = entry = None
    if lots:
        price = _real_float(lots[0].get("last"))
        stop = _real_float(lots[0].get("stop"))
        target = _real_float(lots[0].get("target"))
        entry = _real_float(lots[0].get("avg"))
        if entry is None:
            entry = price
    path = None
    try:
        from abcxauto.memory import get_journal
        from abcxauto.path_math import path_from_journal

        raw = path_from_journal(
            get_journal(),
            equity=nl,
            risk_pct=day.get("max_risk_per_trade_pct"),
        )
        if isinstance(raw, dict):
            path = raw
    except Exception:
        path = None
    try:
        from abcxauto.size_fact import format_size_page

        size = format_size_page(
            nl=nl,
            price=price,
            entry=entry,
            stop=stop,
            atr=_real_float(day.get("atr")),
            sigma_annual=_real_float(day.get("sigma_annual")),
            max_loss_per_contract=_real_float(day.get("max_loss_per_contract")),
            max_risk_per_trade_pct=day.get("max_risk_per_trade_pct"),
            path=path if isinstance(path, dict) else None,
        )
        if size.strip():
            blocks.append(size.strip())
    except Exception:
        logger.debug("size page failed", exc_info=True)
    try:
        from abcxauto.order_type_fact import format_order_type_page

        cash_only = day.get("cash_only")
        order = format_order_type_page(
            nl=nl,
            cash_only=True if cash_only is None else bool(cash_only),
            price=price,
            entry=entry,
            stop=stop,
            target=target,
            atr=_real_float(day.get("atr")),
            debit=day.get("debit"),
            credit=day.get("credit"),
            width=day.get("width"),
            wing=day.get("wing"),
            strike=day.get("strike"),
            put_strike=day.get("put_strike"),
            call_strike=day.get("call_strike"),
            net_debit=day.get("net_debit"),
            bid=day.get("bid"),
            ask=day.get("ask"),
            delta=day.get("delta"),
            theta=day.get("theta"),
            vega=day.get("vega"),
            iv=day.get("iv"),
            rv=day.get("rv"),
            front_iv=day.get("front_iv"),
            back_iv=day.get("back_iv"),
            stock_bid=day.get("stock_bid"),
            stock_ask=day.get("stock_ask"),
        )
        if order.strip():
            blocks.append(order.strip())
    except Exception:
        logger.debug("order type page failed", exc_info=True)
    return "\n".join(blocks)


def _quote_px(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        px = _real_float(row.get(key))
        if px is not None and px > 0:
            return px
    return None


def _nested_quote(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("ibkr", "mda"):
        inner = row.get(key)
        if isinstance(inner, dict):
            return inner
    return row


def _held_stop_and_avg(
    world: Any, symbol: str
) -> tuple[float | None, float | None]:
    su = str(symbol or "").strip().upper()
    if not su:
        return None, None
    positions = list(getattr(world, "positions", None) or [])
    orders = list(getattr(world, "open_orders", None) or [])
    stop = avg = None
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        if str(pos.get("symbol") or "").strip().upper() != su:
            continue
        if stop is None:
            try:
                stop = _covering_last_stop_px(pos, orders)
            except Exception:
                stop = None
        if avg is None:
            try:
                avg = position_avg_facts(pos).get("avg")
            except Exception:
                avg = _real_float(pos.get("avgCost") or pos.get("avg"))
        if stop is not None and avg is not None:
            break
    return _real_float(stop), _real_float(avg)


def attach_tool_math(payload: Any, world: Any) -> None:
    """Recompute size and payoff on a quote the model just received."""
    if not isinstance(payload, dict) or world is None:
        return
    rows = payload.get("quotes")
    targets = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [payload]
    try:
        nl = _real_float(getattr(world, "net_liquidation", None))
    except Exception:
        nl = None
    try:
        cash_only = bool(getattr(get_config(), "cash_only", True))
        risk = getattr(get_config(), "max_risk_per_trade_pct", None)
    except Exception:
        cash_only = True
        risk = None
    for row in targets:
        _attach_one_tool_math(row, world, nl=nl, cash_only=cash_only, risk=risk)


def _attach_one_tool_math(
    row: dict[str, Any],
    world: Any,
    *,
    nl: float | None,
    cash_only: bool,
    risk: Any,
) -> None:
    live = _nested_quote(row)
    price = _quote_px(row, "last", "mid", "mark") or _quote_px(live, "last", "mid", "mark")
    bid = _quote_px(row, "bid") or _quote_px(live, "bid")
    ask = _quote_px(row, "ask") or _quote_px(live, "ask")
    if price is None and bid is not None and ask is not None:
        price = (bid + ask) / 2.0
    sym = str(row.get("symbol") or live.get("symbol") or "").strip().upper()
    stop, avg = _held_stop_and_avg(world, sym)
    und_px = None
    quotes = getattr(world, "ibkr_live_quotes", None)
    if isinstance(quotes, dict) and sym:
        q = quotes.get(sym)
        if isinstance(q, dict):
            und_px = _quote_px(q, "last", "mid")
        else:
            und_px = _real_float(q)
    strike = _real_float(row.get("strike"))
    if strike is None:
        strike = _real_float(live.get("strike"))
    # Finite positive strike => option quote; otherwise stock.
    is_option = strike is not None and strike > 0
    entry = avg if avg is not None else (und_px if is_option else price)
    stock_px = und_px if is_option else price
    try:
        from abcxauto.size_fact import format_size_page

        row["size_page"] = format_size_page(
            nl=nl,
            price=stock_px,
            entry=entry,
            stop=stop,
            max_risk_per_trade_pct=risk,
        ).strip()
    except Exception:
        logger.debug("tool size page failed", exc_info=True)
    try:
        from abcxauto.order_type_fact import format_order_type_page

        if is_option:
            mid = None
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
            debit = mid if mid is not None else price
            delta = _real_float(row.get("delta"))
            if delta is None:
                delta = _real_float(live.get("delta"))
            theta = _real_float(row.get("theta"))
            if theta is None:
                theta = _real_float(live.get("theta"))
            vega = _real_float(row.get("vega"))
            if vega is None:
                vega = _real_float(live.get("vega"))
            iv = _real_float(row.get("iv"))
            if iv is None:
                iv = _real_float(live.get("iv"))
            # Distinct stock keys only — never invent from the option bid/ask.
            stock_bid = _quote_px(row, "stock_bid")
            stock_ask = _quote_px(row, "stock_ask")
            row["order_type"] = format_order_type_page(
                nl=nl,
                cash_only=cash_only,
                price=stock_px,
                entry=entry,
                stop=stop,
                debit=debit,
                strike=strike,
                bid=bid,
                ask=ask,
                delta=delta,
                theta=theta,
                vega=vega,
                iv=iv,
                stock_bid=stock_bid,
                stock_ask=stock_ask,
            ).strip()
        else:
            # Stock quote: bid/ask feed stock_bid/stock_ask only.
            row["order_type"] = format_order_type_page(
                nl=nl,
                cash_only=cash_only,
                price=stock_px,
                entry=entry,
                stop=stop,
                stock_bid=bid,
                stock_ask=ask,
            ).strip()
    except Exception:
        logger.debug("tool order page failed", exc_info=True)


def attach_book_math(payload: Any, world: Any) -> None:
    """Same three pages the wake paints, on the book tool."""
    if not isinstance(payload, dict):
        return
    alloc = payload.get("allocation")
    if not isinstance(alloc, dict):
        return
    day: dict[str, Any] = {"allocation": alloc}
    if world is not None:
        day["div_positions"] = _compact_div_positions(getattr(world, "positions", None))
        day["nl"] = getattr(world, "net_liquidation", None)
    try:
        day["cash_only"] = bool(getattr(get_config(), "cash_only", True))
        day["max_risk_per_trade_pct"] = getattr(
            get_config(), "max_risk_per_trade_pct", None
        )
    except Exception:
        day["cash_only"] = True
    pages = _directional_pages(day)
    if not pages:
        return
    # Order catalog stays on the wake and on option_quote. The book tool
    # is an 8k clip; the two short pages fit beside the lots.
    div, _, rest = pages.partition("\nsize:\n")
    if div.strip():
        payload["diversification"] = div.strip()
    if rest:
        size, _, _order = rest.partition("\nq=floor(NL * f / loss_per_unit)")
        payload["size_page"] = ("size:\n" + size).strip()


def _session_phase(session_status: str, current_et: str | None = None) -> str:
    s = (session_status or "").lower()
    if s != "regular":
        return s or "closed"
    # Heuristic from HH:MM if present
    try:
        hhmm = (current_et or "")[:5]
        if len(hhmm) >= 4 and ":" in hhmm:
            h, m = hhmm.split(":")[:2]
            minutes = int(h) * 60 + int(m)
            if minutes < 10 * 60 + 30:
                return "open"
            if minutes >= 15 * 60:
                return "close"
            return "mid"
    except Exception:
        pass
    return "mid"


def _regime_from_opps(opportunities: list[dict], pulse: dict) -> dict[str, Any]:
    """Feature-mix strip from tape metrics + session (not regime truth / not ranked)."""
    session = (pulse.get("session") or {}) if isinstance(pulse, dict) else {}
    status = str(session.get("status") or "").lower()
    phase = _session_phase(status, session.get("current_time_et"))
    rows = list(opportunities or [])[:12]
    above = 0
    below = 0
    pos_ret = 0
    dists: list[float] = []
    for o in rows:
        try:
            d = float(o.get("dist20"))
            dists.append(d)
            if d >= 0:
                above += 1
            else:
                below += 1
        except (TypeError, ValueError):
            if o.get("above_sma20") is True:
                above += 1
            elif o.get("above_sma20") is False:
                below += 1
        try:
            if float(o.get("ret5") or 0) > 0:
                pos_ret += 1
        except (TypeError, ValueError):
            pass
    if above >= 3 and above > below:
        trend = "bullish"
    elif below >= 3 and below > above:
        trend = "bearish"
    else:
        trend = "mixed"
    med = 0.0
    if dists:
        sd = sorted(dists)
        med = sd[len(sd) // 2]
    vol = (
        "elevated"
        if abs(med) > 0.03 or pos_ret >= max(3, len(rows) // 2 + 1)
        else ("normal" if rows else "quiet")
    )
    return {
        "session_status": status or "unknown",
        "session_phase": phase,
        "trend_bias": trend,
        "feature_mix_bias": trend,
        "vol_proxy": vol,
        "top_longs": above,
        "top_shorts": below,
        "median_dist20": round(med, 5),
        "pos_ret5_count": pos_ret,
        "avg_heuristic_rank": None,
        "avg_opp_score": None,
        "source": "tape_feature_mix",
    }


def _stamp_day_capital_liquidity(
    world: Any,
    port: dict[str, Any],
    nl: Any,
) -> dict[str, Any] | None:
    """capital_liquidity on day_facts. Rebuild percents when missing; never invent cash=0."""
    cap = port.get("capital_liquidity") if isinstance(port.get("capital_liquidity"), dict) else {}
    if cap.get("cash_pct_nl") is not None and cap.get("deployed_long_pct_nl") is not None:
        return cap
    book = getattr(world, "book", None) if world is not None else None
    total_cash = _total_cash_in(cap)
    if total_cash is None:
        total_cash = _total_cash_in(port)
    if total_cash is None:
        total_cash = _total_cash_in(book)
    try:
        nl_f = float(nl) if nl is not None else None
    except (TypeError, ValueError):
        nl_f = None
    if nl_f is not None and (nl_f <= 0 or nl_f != nl_f):
        nl_f = None
    if nl_f is None:
        return cap or None
    positions = [
        p
        for p in (getattr(world, "positions", None) or [])
        if isinstance(p, dict)
    ]
    rebuilt = _portfolio_risk(positions, nl_f, total_cash=total_cash)
    out = dict(rebuilt.get("capital_liquidity") or {})
    if total_cash is None:
        out.pop("total_cash", None)
        out.pop("cash_pct_nl", None)
    return out or None


def _portfolio_risk(
    positions: list[dict],
    net_liq: float | None,
    *,
    total_cash: float | None = None,
) -> dict[str, Any]:
    n = len(positions or [])
    top_pct = 0.0
    top_sym = ""
    by_sym: dict[str, float] = {}
    long_mv = 0.0
    if net_liq and net_liq > 0:
        best = 0.0
        for p in positions or []:
            try:
                mv = abs(float(p.get("marketValue") or p.get("market_value") or 0))
            except (TypeError, ValueError):
                mv = 0.0
            try:
                qty = float(p.get("quantity") or p.get("position") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0:
                long_mv += mv
            sym = str(p.get("symbol") or "").upper()
            if sym:
                by_sym[sym] = by_sym.get(sym, 0.0) + mv
            if mv > best:
                best = mv
                top_sym = str(p.get("symbol") or "")
        top_pct = pct_of_nl(best, net_liq, digits=2) or 0.0
    exposure = {
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "symbols": sorted(
            (
                {
                    "symbol": s,
                    "pct_nl": pct_of_nl(mv, net_liq, digits=2) or 0.0,
                }
                for s, mv in by_sym.items()
            ),
            key=lambda r: -float(r.get("pct_nl") or 0),
        )[:8],
    }
    cash = _real_float(total_cash)
    cash_pct = pct_of_nl(cash, net_liq, digits=2) if cash is not None else None
    deployed_pct = pct_of_nl(long_mv, net_liq, digits=2) or 0.0
    capital_liquidity: dict[str, Any] = {
        "cash_pct_nl": cash_pct,
        "deployed_long_pct_nl": deployed_pct,
    }
    if cash is not None:
        capital_liquidity["total_cash"] = round(cash, 2)
    return {
        "n_positions": n,
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "exposure": exposure,
        "capital_liquidity": capital_liquidity,
    }


def _as_px(raw: Any) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("last") if raw.get("last") is not None else raw.get("mid")
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def _lot_qty(pos: dict[str, Any]) -> float | None:
    for key in ("qty", "quantity", "position"):
        if pos.get(key) is None:
            continue
        try:
            return float(pos[key])
        except (TypeError, ValueError):
            continue
    return None


def _stop_map(orders: list[dict[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        typ = str(order.get("type") or order.get("order_type") or "").upper()
        role = str(order.get("role") or "").lower()
        if role == "entry":
            continue
        if typ not in ("STP", "STP LMT", "TRAIL") and order.get("stop") is None:
            continue
        sym = str(order.get("symbol") or "").upper().strip()
        px = _as_px(order.get("stop") or order.get("auxPrice") or order.get("aux_price"))
        if sym and px is not None:
            out[sym] = px
    return out


def _target_map(orders: list[dict[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        typ = str(order.get("type") or order.get("order_type") or "").upper()
        role = str(order.get("role") or "").lower()
        if role == "entry":
            continue
        if typ not in ("LMT", "LIMIT"):
            continue
        sym = str(order.get("symbol") or "").upper().strip()
        px = _as_px(
            order.get("lmt")
            or order.get("limit")
            or order.get("limit_price")
            or order.get("lmtPrice")
        )
        if sym and px is not None:
            out[sym] = px
    return out


def _lot_px_for_liquidity(pos: dict[str, Any], quote: Any) -> float | None:
    """Last from quote/lot, else bid already on the lot. No invented prices."""
    last = _as_px(quote)
    if last is not None:
        return last
    return _as_px(
        pos.get("mkt")
        or pos.get("market_price")
        or pos.get("marketPrice")
        or pos.get("last")
        or pos.get("bid")
    )


def _cut_half_shares(qty: float) -> int | None:
    """Whole shares freed by selling half a long lot. None when qty < 2."""
    if qty < 2:
        return None
    return max(1, int(math.floor(qty / 2.0)))


def allocation_facts(
    positions: list[dict[str, Any]] | None,
    *,
    net_liq: Any = None,
    total_cash: Any = None,
    quotes: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Lots ranked by capital used. Facts only."""
    try:
        nl = float(net_liq) if net_liq is not None else None
    except (TypeError, ValueError):
        nl = None
    if nl is not None and nl <= 0:
        nl = None
    qmap = quotes if isinstance(quotes, dict) else {}
    stops = _stop_map(orders)
    targets = _target_map(orders)
    lots: list[dict[str, Any]] = []
    deployed = 0.0
    risk_to_stop_sum = 0.0
    risk_to_stop_any = False
    cut_all_sum = 0.0
    cut_half_sum = 0.0
    cut_all_any = False
    cut_half_any = False
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sym = str(pos.get("symbol") or "").upper().strip()
        if not sym:
            continue
        qty = _lot_qty(pos)
        # Zero-qty ghosts are not open lots.
        if qty is None or abs(qty) < 1e-12:
            continue
        last = _lot_px_for_liquidity(pos, qmap.get(sym))
        sec = str(pos.get("secType") or pos.get("sec_type") or pos.get("sec") or "STK").upper()
        mult = 100.0 if sec.startswith("OPT") else 1.0
        mv = None
        for key in ("marketValue", "market_value"):
            mv = _as_px(pos.get(key))
            if mv is not None:
                break
        if mv is None and last is not None:
            mv = abs(qty) * last * mult
        if mv is not None:
            deployed += mv
        pct = pct_of_nl(mv, nl, digits=2) if mv is not None else None
        upnl = lot_upnl(pos)
        if upnl is None and last is not None:
            avg = position_avg_facts(pos).get("avg")
            if avg is not None:
                upnl = round((last - float(avg)) * qty * mult, 2)
        stop = stops.get(sym)
        if stop is None:
            stop = _as_px(pos.get("stop") or pos.get("stop_price"))
        risk_usd = None
        if stop is not None and last is not None:
            risk_usd = abs(last - stop) * abs(qty) * mult
        risk_pct = pct_of_nl(risk_usd, nl, digits=2) if risk_usd is not None and nl else None
        # Book risk-to-stop sums long lots only (abs(last-stop)*qty).
        risk_to_stop_lot = (
            abs(last - stop) * qty * mult
            if stop is not None and last is not None and qty > 0
            else None
        )
        row: dict[str, Any] = {"symbol": sym}
        row["qty"] = int(qty) if abs(qty - int(qty)) < 1e-9 else qty
        if last is not None:
            row["last"] = last
        avg = position_avg_facts(pos).get("avg")
        if avg is None:
            avg = _as_px(pos.get("avg"))
        if avg is not None:
            row["avg"] = avg
        if pct is not None:
            row["pct_nl"] = pct
        if upnl is not None:
            row["uPnL"] = round(float(upnl), 2)
        if risk_pct is not None:
            row["risk_pct_nl"] = risk_pct
        if stop is not None:
            row["stop"] = stop
        # STK long liquidity facts: dollars freed at last (or lot bid).
        if sec.startswith("STK") and qty > 0 and last is not None:
            cut_all = round(qty * last, 2)
            row["cut_all_usd"] = cut_all
            cut_all_sum += cut_all
            cut_all_any = True
            half_n = _cut_half_shares(qty)
            if half_n is not None:
                cut_half = round(half_n * last, 2)
                row["cut_half_usd"] = cut_half
                cut_half_sum += cut_half
                cut_half_any = True
        if risk_to_stop_lot is not None:
            risk_to_stop_sum += risk_to_stop_lot
            risk_to_stop_any = True
        target = targets.get(sym)
        if target is not None:
            row["target"] = target
            if last is not None:
                if qty > 0:
                    row["to_target_usd"] = round((target - last) * qty * mult, 2)
                elif qty < 0:
                    row["to_target_usd"] = round((last - target) * abs(qty) * mult, 2)
        if (
            row.get("to_target_usd") is not None
            and mv is not None
            and mv > 0
        ):
            row["eff"] = round(float(row["to_target_usd"]) / float(mv), 4)
        if row.keys() - {"symbol"}:
            lots.append(row)
    lots.sort(key=lambda r: -float(r.get("pct_nl") or 0))
    leftover_usd = _real_float(total_cash)
    if leftover_usd is not None:
        leftover_usd = round(leftover_usd, 2)
    cash_pct = pct_of_nl(leftover_usd, nl, digits=2) if leftover_usd is not None else None
    if cash_pct is None and nl is not None:
        cash_pct = pct_of_nl(max(0.0, nl - deployed), nl, digits=2)
    deployed_pct = pct_of_nl(deployed, nl, digits=2) if nl is not None else None
    out: dict[str, Any] = {"lots": lots[:8]}
    if leftover_usd is not None:
        out["leftover_usd"] = leftover_usd
    out["deployed_usd"] = round(deployed, 2)
    if cash_pct is not None:
        out["cash_pct_nl"] = cash_pct
    if deployed_pct is not None:
        out["deployed_pct_nl"] = deployed_pct
    if nl is not None:
        out["nl"] = round(nl, 2)
    to_tgt_vals = [
        r.get("to_target_usd")
        for r in lots
        if isinstance(r, dict) and r.get("to_target_usd") is not None
    ]
    if to_tgt_vals:
        out["lots_to_target_usd"] = round(sum(float(v) for v in to_tgt_vals), 2)
    if cut_all_any or cut_half_any:
        liq: dict[str, Any] = {}
        if cut_half_any:
            liq["cut_half_usd"] = round(cut_half_sum, 2)
        if cut_all_any:
            liq["cut_all_usd"] = round(cut_all_sum, 2)
        out["liquidity"] = liq
    if risk_to_stop_any:
        out["risk_to_stop_usd"] = round(risk_to_stop_sum, 2)
    return out


def _allocation_liquidity_tail(bag: dict[str, Any] | None) -> str:
    """cut-half / cut-all / risk-to-stop dollars. Facts only — no advice."""
    src = bag if isinstance(bag, dict) else {}
    alloc = src.get("allocation") if isinstance(src.get("allocation"), dict) else src
    liq = alloc.get("liquidity") if isinstance(alloc.get("liquidity"), dict) else {}
    if not liq and isinstance(src.get("liquidity"), dict):
        liq = src["liquidity"]
    cut_half = _real_float(liq.get("cut_half_usd"))
    cut_all = _real_float(liq.get("cut_all_usd"))
    risk_stop = _real_float(alloc.get("risk_to_stop_usd"))
    if risk_stop is None:
        risk_stop = _real_float(src.get("risk_to_stop_usd"))
    tail_bits: list[str] = []
    if cut_half is not None:
        tail_bits.append(f"cut-half {_fmt_usd_compact(cut_half)}")
    if cut_all is not None:
        tail_bits.append(f"cut-all {_fmt_usd_compact(cut_all)}")
    if risk_stop is not None:
        tail_bits.append(f"risk-to-stop {_fmt_usd_compact(risk_stop)}")
    return " ".join(tail_bits)


def allocation_line(facts: dict[str, Any] | None) -> str:
    """One comparable line: leftover cash, then lots by capital used."""
    bag = facts if isinstance(facts, dict) else {}
    bits: list[str] = []
    leftover = _real_float(bag.get("leftover_usd"))
    cash = _real_float(bag.get("cash_pct_nl"))
    dep = _real_float(bag.get("deployed_pct_nl"))
    head = _leftover_deployed_text(leftover, cash, dep, _day_pnl_of(bag))
    lots_to_tgt = _real_float(bag.get("lots_to_target_usd"))
    if leftover is not None and lots_to_tgt is not None:
        vs = f" vs lots-to-target ${lots_to_tgt}"
        head = f"{head}{vs}" if head else vs.strip()
    if head:
        bits.append(head)
    for lot in bag.get("lots") or []:
        if not isinstance(lot, dict):
            continue
        sym = str(lot.get("symbol") or "").strip()
        if not sym:
            continue
        qty = lot.get("qty")
        qty_bit = str(qty) if qty is not None else ""
        part = f"{sym}{qty_bit}"
        if lot.get("pct_nl") is not None:
            part += f" {lot['pct_nl']}%"
        if lot.get("last") is not None:
            part += f" {lot['last']}"
        if lot.get("uPnL") is not None:
            part += f" uPnL={lot['uPnL']}"
        if lot.get("risk_pct_nl") is not None:
            part += f" risk={lot['risk_pct_nl']}%"
        if lot.get("stop") is not None:
            part += f" stp={lot['stop']}"
        if lot.get("target") is not None:
            part += f" tgt={lot['target']}"
        if lot.get("to_target_usd") is not None:
            part += f" to_tgt={lot['to_target_usd']}"
        bits.append(part)
    liq_tail = _allocation_liquidity_tail(bag)
    if liq_tail:
        bits.append(liq_tail)
    return " | ".join(bits)


def _scan_stashed_range(row: dict[str, Any]) -> bool:
    if str(row.get("print") or "") == "live_open":
        return True
    return str(row.get("source") or "").strip().lower() == "scan"


def range_compare_line(session_range: dict[str, Any] | None) -> str:
    """This-look candle ranges, largest |gap_pct| first. Not expected profit."""
    store = session_range if isinstance(session_range, dict) else {}
    ranked: list[tuple[float, str, str]] = []
    for raw_sym, row in store.items():
        if not isinstance(row, dict) or _scan_stashed_range(row):
            continue
        sym = str(raw_sym or "").upper().strip()
        if not sym:
            continue
        gap = _real_float(row.get("gap_pct"))
        vs = _real_float(row.get("vs_open"))
        if gap is None and vs is None:
            continue
        key = abs(gap) if gap is not None else abs(vs or 0.0)
        bit = sym
        if gap is not None:
            bit += f" gap={gap}"
        if vs is not None:
            bit += f" vs_open={vs}"
        ranked.append((key, sym, bit))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    bits = [row[2] for row in ranked[:6]]
    if not bits:
        return ""
    return "range " + " ".join(bits)


def _fmt_to_high_usd(usd: float) -> str:
    """Round dollars without cents when >= 1, else one decimal."""
    if float(usd) >= 1.0:
        return f"${int(round(float(usd)))}"
    return f"${round(float(usd), 1):.1f}"


def to_high_line(session_range: dict[str, Any] | None) -> str:
    """This-look dollar room to session high for sized names. Not expectancy."""
    store = session_range if isinstance(session_range, dict) else {}
    ranked: list[tuple[float, str, str]] = []
    for raw_sym, row in store.items():
        if not isinstance(row, dict) or _scan_stashed_range(row):
            continue
        sym = str(raw_sym or "").upper().strip()
        if not sym:
            continue
        last = _real_float(row.get("last"))
        high = _real_float(row.get("high"))
        size = row.get("size") if isinstance(row.get("size"), dict) else None
        if last is None or high is None or size is None:
            continue
        qty = _real_float(size.get("qty"))
        if qty is None or qty <= 0:
            continue
        usd = (high - last) * qty
        if usd <= 0:
            continue
        ranked.append((usd, sym, f"{sym} {_fmt_to_high_usd(usd)}"))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    bits = [row[2] for row in ranked[:6]]
    if not bits:
        return ""
    return "to_high " + " ".join(bits)


@dataclass
class WorldState:
    cycle: int
    session_status: str
    flat: bool
    needs_protection: bool
    unprotected: list[str]
    net_liquidation: float | None
    daily_pnl: float
    positions: list[dict]
    open_orders: list[dict]
    opportunities: list[dict]
    news_items: list[dict]
    risk_posture: str
    effective_posture: str
    gates: dict[str, Any]
    envelope: dict[str, Any]
    regime: dict[str, Any]
    portfolio_risk: dict[str, Any]
    working_thesis: str
    recent_decisions: list[dict]
    trade_plan: dict[str, Any] | None
    trade_plans: list[dict[str, Any]] = field(default_factory=list)
    capacity: dict[str, Any] = field(default_factory=dict)
    structure_lessons: list[dict] = field(default_factory=list)
    structure_cooldown: dict[str, str] = field(default_factory=dict)
    book: dict[str, Any] = field(default_factory=dict)
    pulse: dict[str, Any] = field(default_factory=dict)
    taken_at: str = ""
    ibkr_live_last: float | None = None
    ibkr_live_symbol: str = ""
    ibkr_live_quotes: dict[str, float] = field(default_factory=dict)
    candle_source: str = ""
    scan_fetched: list[str] = field(default_factory=list)
    option_facts: list[dict] = field(default_factory=list)
    vol_facts: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    stop_qty_fact: dict[str, Any] | None = None
    book_reconciled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "session_status": self.session_status,
            "flat": self.flat,
            "needs_protection": self.needs_protection,
            "unprotected": list(self.unprotected),
            "net_liquidation": self.net_liquidation,
            "daily_pnl": self.daily_pnl,
            "n_positions": len(self.positions),
            "n_orders": len(self.open_orders),
            "opportunities": self.opportunities[:12],
            "scan_fetched": list(self.scan_fetched),
            "option_facts": list(self.option_facts[:8]),
            "vol_facts": list(self.vol_facts[:6]),
            "stop_qty_fact": self.stop_qty_fact,
            "ibkr_live_last": self.ibkr_live_last,
            "ibkr_live_symbol": self.ibkr_live_symbol,
            "ibkr_live_quotes": dict(self.ibkr_live_quotes or {}),
            "candle_source": self.candle_source or "none",
            "news_items": [
                {"symbol": n.get("symbol"), "headline": str(n.get("headline") or "")[:160]}
                for n in self.news_items[:12]
                if n.get("headline")
            ],
            "risk_posture": self.risk_posture,
            "effective_posture": self.effective_posture,
            "gates": self.gates,
            "envelope": self.envelope,
            "regime": self.regime,
            "portfolio_risk": self.portfolio_risk,
            "recent_decisions": self.recent_decisions[:3],
            "trade_plan": self.trade_plan,
            "trade_plans": list(self.trade_plans[:12]),
            "capacity": dict(self.capacity or {}),
            "structure_lessons": self.structure_lessons[:5],
            "structure_cooldown": dict(self.structure_cooldown),
            "taken_at": self.taken_at,
            "mix": structure_mix(self.positions),
            "open_lots": lot_labels(
                self.positions,
                fills=self.fills,
                plans=(
                    list(self.trade_plans or [])
                    or ([self.trade_plan] if self.trade_plan else [])
                ),
            ),
        }


def build_world_state(
    *,
    cycle: int,
    snap: dict[str, Any],
    opportunities: list[dict],
    news_items: list[dict],
) -> WorldState:
    """Assemble WorldState from snap + scan + journal + plan."""
    from abcxauto.book import build_book_from_snap
    from abcxauto.memory import get_journal

    positions, orders, book_reconciled = reconcile_book_with_fills(
        list(snap.get("positions") or []),
        list(snap.get("open_orders") or []),
        snap.get("fills"),
    )
    snap["positions"] = positions
    snap["open_orders"] = orders
    snap["book_reconciled"] = book_reconciled
    acct = snap.get("account") or {}
    pulse = snap.get("reality_pulse") or {}
    protection = snap.get("protection") or {}
    unprotected = list(protection.get("unprotected_symbols") or [])
    session = str((pulse.get("session") or {}).get("status") or "").lower()
    net = account_float(acct, "netliquidation", "NetLiquidation")
    if net is None:
        raw = snap.get("net_liquidation")
        if raw is not None:
            try:
                net = float(raw)
            except (TypeError, ValueError):
                net = None
    pnl = daily_pnl_of(acct)
    if pnl is None:
        pnl = 0.0
    total_cash = account_float(
        acct, "totalcashvalue", "TotalCashValue", "total_cash", "TotalCash"
    )

    cfg = get_config()
    posture = str(getattr(cfg, "risk_posture", "") or "")
    eff = resolve_effective_posture(posture, getattr(cfg, "trading_mode", "paper") or "paper")
    env_snap = risk_envelope_snapshot()
    gates = env_snap.get("current") or {}
    envelope = env_snap.get("envelope") or {}

    recent: list[dict] = []
    try:
        j = get_journal()
        recent = j.recent_decisions(limit=5)
    except Exception:
        pass

    plans = [
        p for p in load_trade_plans() if trade_plan_matches_stk(p, positions)
    ]
    plan = plans[0] if plans else None
    plan_dict = plan.to_dict() if plan else None
    plans_dicts = [p.to_dict() for p in plans]
    regime = _regime_from_opps(opportunities, pulse)
    port_risk = _portfolio_risk(positions, net, total_cash=total_cash)
    bp = account_float(acct, "availablefunds", "AvailableFunds")
    if bp is not None and isinstance(port_risk, dict):
        port_risk["buying_power_usd"] = round(float(bp), 2)
    try:
        max_open = int(getattr(cfg, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_open = 0
    try:
        from abcxauto.self_tune import slot_cap_armed

        armed = slot_cap_armed(cfg)
    except Exception:
        armed = None
    cap = capacity_fact(
        positions,
        max_open_positions=max_open,
        open_orders=orders,
        net_liq=net,
        cap_armed=armed,
    )
    option_facts = list(snap.get("option_facts") or [])
    stop_fact = None
    try:
        from abcxauto.trade_plan import stop_qty_mismatch_fact

        stop_fact = stop_qty_mismatch_fact(positions, orders, None)
    except Exception:
        stop_fact = None

    book = snap.get("portfolio_state") or build_book_from_snap(snap)
    unreliable = bool(snap.get("book_unreliable"))
    if unreliable:
        gates = dict(gates) if isinstance(gates, dict) else {}
        gates["book_unreliable"] = True
    live_sym, live_last = _ibkr_live_mark(snap, positions)
    ws = WorldState(
        cycle=cycle,
        session_status=session or "unknown",
        flat=False if unreliable else book_is_flat(positions, orders, snap.get("fills")),
        needs_protection=bool(unprotected),
        unprotected=unprotected,
        net_liquidation=net,
        daily_pnl=pnl,
        positions=positions,
        open_orders=orders,
        opportunities=list(opportunities or []),
        news_items=list(news_items or []),
        risk_posture=posture,
        effective_posture=eff,
        gates=gates,
        envelope=envelope,
        regime=regime,
        portfolio_risk=port_risk,
        working_thesis="",
        recent_decisions=[
            {
                "strategy": d.get("strategy"),
                "action": d.get("action"),
                "rationale": (d.get("rationale") or "")[:100],
            }
            for d in recent[:3]
        ],
        trade_plan=plan_dict,
        trade_plans=plans_dicts,
        capacity=cap,
        structure_lessons=[],
        structure_cooldown={},
        book=book if isinstance(book, dict) else {},
        pulse=pulse if isinstance(pulse, dict) else {},
        taken_at=str(snap.get("taken_at") or ""),
        option_facts=option_facts,
        vol_facts=list(snap.get("vol_facts") or []),
        fills=list(snap.get("fills") or [])[:12],
        stop_qty_fact=stop_fact,
        book_reconciled=book_reconciled,
        ibkr_live_quotes=dict(snap.get("ibkr_live_quotes") or {}),
        candle_source=str(snap.get("candle_source") or "") or "none",
        scan_fetched=list(snap.get("scan_fetched") or []),
        ibkr_live_symbol=live_sym,
        ibkr_live_last=live_last,
    )
    try:
        from abcxauto.vol_fact import publish_vol_facts

        publish_vol_facts(ws, snap)
    except Exception:
        pass
    return ws


def capacity_allows_new_risk(world: Any, cfg: Any = None) -> bool:
    """Refuse new risk on count only when mop is a positive Grok/operator ceiling.

    mop 0/absent: no count refuse. Same on paper and live. Working entries
    reserve slots when the cap is armed.
    """
    c = cfg if cfg is not None else get_config()
    try:
        from abcxauto.self_tune import slot_cap_armed

        if not slot_cap_armed(c):
            return True
    except Exception:
        pass
    cap = getattr(world, "capacity", None) or {}
    if isinstance(cap, dict) and "allows_new_risk" in cap:
        return bool(cap.get("allows_new_risk"))
    try:
        max_n = int(getattr(c, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_n = 0
    if max_n <= 0:
        return True
    from abcxauto.trade_plan import open_position_count, working_entry_slots

    used = open_position_count(getattr(world, "positions", None))
    pending = working_entry_slots(
        getattr(world, "open_orders", None), getattr(world, "positions", None)
    )
    return used + pending < max_n
