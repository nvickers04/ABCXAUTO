"""WorldState — live book facts for Grok tools (no LLM)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.structure_grade import (
    recent_structure_lessons,
    structure_cooldown_symbols,
)
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
        "qty": p.get("quantity") if p.get("quantity") is not None else p.get("position"),
        "avg": avg_row.get("avg"),
        "mkt": p.get("market_price") or p.get("marketPrice") or p.get("last"),
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
                row["uPnL_pct_nl"] = round(100.0 * float(upnl) / float(nl), 4)
            avg_usd = row.get("avg_usd")
            if avg_usd is not None:
                try:
                    row["avg_usd_pct_nl"] = round(100.0 * float(avg_usd) / float(nl), 4)
                except (TypeError, ValueError):
                    pass
            try:
                mv = abs(float(p.get("marketValue") or p.get("market_value") or 0))
            except (TypeError, ValueError):
                mv = 0.0
            if mv > 0:
                row["mv_pct_nl"] = round(100.0 * mv / float(nl), 4)
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
                    row["risk_pct_nl"] = round(100.0 * risk_usd / float(nl), 4)
    return row


def pct_of_nl(usd: Any, net_liq: Any) -> float | None:
    """Percent of NetLiq for a dollar amount. None when either side is unknown."""
    try:
        dollars = float(usd)
        nl = float(net_liq)
    except (TypeError, ValueError):
        return None
    if nl == 0:
        return None
    return round(100.0 * dollars / nl, 4)


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
        trail = o.get("trail_percent") or o.get("trailingPercent") or o.get("trail_amount")
        if trail not in (None, 0, 0.0, "0"):
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
    # Current NL is the denominator for clerk pct_of_nl siblings (keep $ fields).
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
        floors = None
    port = dict(getattr(world, "portfolio_risk", None) or {})
    tape_seed: list[str] = []
    try:
        from abcxauto.opportunity_scan import tape_seed_symbols

        tape_seed = tape_seed_symbols(getattr(world, "positions", None))
    except Exception:
        tape_seed = []
    mins_open = _minutes_to_open(world)
    pulse = getattr(world, "pulse", None) if isinstance(getattr(world, "pulse", None), dict) else {}
    sess_block = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    vol_rows = _day_vol(world)
    alarms = _wake_lot_alarms(world)
    return {
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
        "capital_liquidity": port.get("capital_liquidity"),
        # Optional seed for tools; format_wake does not print tape=.
        "tape_seed": tape_seed,
        "minutes_to_open": mins_open,
        "countdown_to": sess_block.get("countdown_to"),
        "countdown_human": sess_block.get("countdown_human"),
        "tradable_now": pulse.get("tradable_now"),
    }


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
        from abcxauto.broker.order_types import is_stop_order
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
        stops = [
            o
            for o in exits
            if is_stop_order(str(o.get("order_type") or o.get("orderType") or ""))
        ]
        if not exits:
            missing.append(ident)
        last = _lot_last_px(p, quotes)
        stop_px = None
        for o in stops or exits:
            stop_px = _order_stop_px(o)
            if stop_px is not None:
                break
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
    """Cash / deployed / top concentration as % of NL (facts only)."""
    cap = day.get("capital_liquidity") if isinstance(day.get("capital_liquidity"), dict) else {}
    exp = day.get("exposure") if isinstance(day.get("exposure"), dict) else {}
    port = day.get("portfolio_risk") if isinstance(day.get("portfolio_risk"), dict) else {}
    bits: list[str] = []
    cash_pct = cap.get("cash_pct_nl")
    if cash_pct is not None:
        bits.append(f"cash={cash_pct}% NL")
    deployed = cap.get("deployed_long_pct_nl")
    if deployed is not None:
        bits.append(f"deployed={deployed}% NL")
    top_pct = exp.get("top_concentration_pct")
    if top_pct is None:
        top_pct = port.get("top_concentration_pct")
    top_sym = exp.get("top_symbol") or port.get("top_symbol") or ""
    if top_pct is not None:
        sym = f" {top_sym}" if top_sym else ""
        bits.append(f"top{sym}={top_pct}% NL")
    return " ".join(bits)


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


def worst_wake_fact(
    *,
    unprotected: list[str] | None,
    day: dict[str, Any] | None = None,
    session: str = "",
) -> str:
    """One leading fact: unprotected, stop distance, missing working order, halt, cap.

    Not a strategy menu. Unprotected STK already fail-closes — publish it first.
    """
    day = day if isinstance(day, dict) else {}
    unprot = [str(x).strip() for x in (unprotected or []) if str(x).strip()]
    if unprot:
        return "unprotected=" + ",".join(unprot)
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
    """Desk brief. Live book facts; no canned tape= names. Scan is a tool.

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
        # leftover say / prev= / unused= stay off wake.
    # Desk facts only — no trailing "send." (Grok reads that as an operator command).
    body = " ".join(parts)
    lead = worst_wake_fact(unprotected=unprotected, day=day, session=session)
    if lead:
        if not lead.endswith("."):
            lead = lead + "."
        return f"{lead}\n{body}"
    return body


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


def _portfolio_risk(
    positions: list[dict],
    net_liq: float,
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
        top_pct = round(100.0 * best / float(net_liq), 2)
    # Soft exposure Fact (not a hold gate): top names + share of NL.
    exposure = {
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "symbols": sorted(
            (
                {
                    "symbol": s,
                    "pct_nl": round(100.0 * mv / float(net_liq), 2)
                    if net_liq and net_liq > 0
                    else 0.0,
                }
                for s, mv in by_sym.items()
            ),
            key=lambda r: -float(r.get("pct_nl") or 0),
        )[:8],
        "note": "Fact — soft concentration; not a narrative hold gate",
    }
    try:
        cash = float(total_cash) if total_cash is not None else 0.0
    except (TypeError, ValueError):
        cash = 0.0
    cash_pct = round(100.0 * cash / float(net_liq), 2) if net_liq and net_liq > 0 else 0.0
    deployed_pct = (
        round(100.0 * long_mv / float(net_liq), 2) if net_liq and net_liq > 0 else 0.0
    )
    capital_liquidity = {
        "total_cash": round(cash, 2),
        "cash_pct_nl": cash_pct,
        "deployed_long_pct_nl": deployed_pct,
        "note": "Fact — liquidity vs NL; not a hold/sell gate",
    }
    return {
        "n_positions": n,
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "exposure": exposure,
        "capital_liquidity": capital_liquidity,
    }


@dataclass
class WorldState:
    cycle: int
    session_status: str
    flat: bool
    needs_protection: bool
    unprotected: list[str]
    net_liquidation: float
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
            "working_thesis": self.working_thesis[:400],
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
    try:
        net = float(account_float(acct, "netliquidation", "NetLiquidation") or 0)
    except (TypeError, ValueError):
        net = 0.0
    pnl = daily_pnl_of(acct)
    if pnl is None:
        pnl = 0.0
    try:
        total_cash = float(
            acct.get("totalcashvalue")
            or acct.get("TotalCashValue")
            or acct.get("total_cash")
            or acct.get("TotalCash")
            or 0
        )
    except (TypeError, ValueError):
        total_cash = 0.0

    cfg = get_config()
    posture = str(getattr(cfg, "risk_posture", "") or "")
    eff = resolve_effective_posture(posture, getattr(cfg, "trading_mode", "paper") or "paper")
    env_snap = risk_envelope_snapshot()
    gates = env_snap.get("current") or {}
    envelope = env_snap.get("envelope") or {}

    thesis = ""
    recent: list[dict] = []
    try:
        j = get_journal()
        thesis = j.get_working_thesis() or ""
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
    lessons = recent_structure_lessons(5)
    cool = structure_cooldown_symbols(lessons)
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
        working_thesis=thesis,
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
        structure_lessons=lessons,
        structure_cooldown=cool,
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
