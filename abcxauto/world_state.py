"""WorldState — live book facts for Grok tools (no LLM)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.opportunity_scan import QUOTE_SOURCES_BLOCK, format_scan_tape
from abcxauto.structure_grade import (
    recent_structure_lessons,
    structure_cooldown_symbols,
)
from abcxauto.trade_plan import capacity_fact, load_trade_plan, load_trade_plans

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IDLE_STATE_PATH = _REPO_ROOT / "idle_streak_state.json"
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


def compact_position(pos: dict[str, Any], *, extra: bool = True) -> dict[str, Any]:
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
    return row


def reconcile_book_with_fills(
    positions: list[dict] | None,
    orders: list[dict] | None,
    fills: list[dict] | None,
    *,
    window_s: float = FILL_WINDOW_S,
) -> tuple[list[dict], list[dict], bool]:
    """Drop lots / working tickets that recent fills already closed."""
    pos_out = [dict(p) for p in (positions or []) if isinstance(p, dict)]
    ord_out = [dict(o) for o in (orders or []) if isinstance(o, dict)]
    sold: dict[str, float] = {}
    filled_ids: set[str] = set()
    for f in fills or []:
        if not isinstance(f, dict) or not fill_in_window(f, window_s=window_s):
            continue
        oid = f.get("order_id") if f.get("order_id") is not None else f.get("orderId")
        if oid is not None and str(oid):
            filled_ids.add(str(oid))
        pid = f.get("permId") if f.get("permId") is not None else f.get("perm_id")
        if pid is not None and str(pid):
            filled_ids.add(str(pid))
        side = str(f.get("side") or f.get("action") or "").upper()
        if side not in ("SLD", "SELL"):
            continue
        cid = str(f.get("conId") or f.get("con_id") or "")
        if not cid:
            continue
        try:
            qty = abs(float(f.get("quantity") if f.get("quantity") is not None else f.get("shares") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0:
            sold[cid] = sold.get(cid, 0.0) + qty
    reconciled = False
    kept_pos: list[dict] = []
    for p in pos_out:
        cid = str(p.get("conId") or p.get("con_id") or "")
        try:
            qty = abs(float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            ))
        except (TypeError, ValueError):
            qty = 0.0
        take = sold.get(cid, 0.0) if cid else 0.0
        if take > 0 and qty > 0:
            reconciled = True
            left = qty - take
            if left < 1e-9:
                continue
            sign = 1.0
            try:
                raw_q = float(p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0)
                if raw_q < 0:
                    sign = -1.0
            except (TypeError, ValueError):
                pass
            if "quantity" in p:
                p["quantity"] = sign * left
            if "position" in p:
                p["position"] = sign * left
        kept_pos.append(p)
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


def _lot_card_tag(ident: str, row: dict[str, Any]) -> str:
    try:
        from abcxauto.lab_playbook import load_lab

        lab = load_lab()
    except Exception:
        return ""
    pre = lab.get("lots_at_write") if isinstance(lab.get("lots_at_write"), list) else []
    if not pre:
        return ""
    keys = {str(x) for x in pre if x}
    con = str(row.get("conId") or row.get("con_id") or "")
    if con and con in keys:
        return " pre"
    if ident in keys:
        return " pre"
    for s in keys:
        if ident.startswith(s) or s.startswith(ident):
            return " pre"
    return " this"


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


def lot_labels(positions: list[dict] | None, *, limit: int = 16) -> list[str]:
    """Short lot identities for wake / last_turn. Not a rank."""
    labels: list[str] = []
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
        ident = lot_ident(p)
        extra = _lot_mtm_suffix(row, qty)
        card = _lot_card_tag(ident, row)
        if extra or card:
            ident = f"{ident}{extra}{card}"
        labels.append(ident)
        if len(labels) >= limit:
            break
    return labels


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


def day_facts(world: Any, scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Session forest: edge vs model, daily PnL, name concentration."""
    sc = scorecard if isinstance(scorecard, dict) else {}
    conc = concentration(getattr(world, "positions", None))
    risk_pct = None
    try:
        risk_pct = float(getattr(get_config(), "max_risk_per_trade_pct", None) or 0) or None
    except (TypeError, ValueError):
        risk_pct = None
    return {
        "nl": getattr(world, "net_liquidation", None),
        "daily_pnl": getattr(world, "daily_pnl", None),
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "model_cost_usd": sc.get("model_cost_usd"),
        "names": conc["names"],
        "lots": conc["lots"],
        "structures": conc["structures"],
        "by_name": conc["by_name"],
        "cloned": conc["cloned"],
        "open_lots": lot_labels(getattr(world, "positions", None)),
        "mix": structure_mix(getattr(world, "positions", None)),
        "capacity": dict(getattr(world, "capacity", None) or {}),
        "risk_per_trade_pct": risk_pct,
        "playbook": _playbook_day(sc),
    }


def _playbook_day(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    try:
        from abcxauto.lab_playbook import playbook_glance

        return playbook_glance(scorecard)
    except Exception:
        return {}


def format_wake(
    *,
    cycle: int,
    session: str,
    flat: bool,
    unprotected: list[str] | None,
    ibkr_up: bool,
    day: dict[str, Any] | None = None,
) -> str:
    """Desk brief. Fill/order_change is a delta, not a discovery assignment."""
    unprot = ",".join(unprotected) if unprotected else "none"
    day = day if isinstance(day, dict) else {}
    lots = day.get("open_lots") or []
    lot_s = ",".join(str(x) for x in lots[:12]) if lots else ""
    mix_s = format_mix(day.get("mix") if isinstance(day.get("mix"), dict) else {})
    cap = day.get("capacity") if isinstance(day.get("capacity"), dict) else {}
    open_n = cap.get("open_count", cap.get("open"))
    max_n = cap.get("max_open_positions", cap.get("max"))
    ev = None
    try:
        from abcxauto.wake_bus import last_wake

        ev = last_wake()
    except Exception:
        ev = None
    kind = str(ev.kind or "") if ev is not None else ""
    brief: dict[str, Any] = {}
    try:
        from abcxauto.think_stream import load_desk_brief

        brief = load_desk_brief()
    except Exception:
        brief = {}
    prev_strat = brief.get("strat") or brief.get("previous_strat") or ""
    prev_sends = brief.get("sends") if brief.get("sends") is not None else 0
    if kind in ("fill", "order_change", "book_move"):
        detail = (ev.detail or "").strip() if ev is not None else ""
        event_s = f"event={kind} {detail}.".strip() if detail else f"event={kind}."
        parts = [
            event_s,
            f"prev={prev_strat or '—'} sends={prev_sends}.",
            f"session={session} flat={flat} NL={day.get('nl')} "
            f"open={open_n}/{max_n} mix={mix_s or 'none'}.",
        ]
        if lot_s:
            parts.append(f"open_lots={lot_s}.")
        if unprot != "none":
            parts.append(f"unprotected={unprot}.")
        dp = day.get("daily_pnl")
        parts.append(f"dayPnL={dp if dp is not None else '?'}.")
        parts.append("This is a delta. send|set_wake.")
        return " ".join(p for p in parts if p)
    risk = day.get("risk_per_trade_pct")
    parts = [
        f"Cycle {cycle}. session={session} flat={flat} "
        f"unprotected={unprot} ibkr={'up' if ibkr_up else 'down'}.",
    ]
    if day:
        dp = day.get("daily_pnl")
        parts.append(
            f"names={day.get('names')} lots={day.get('lots')} "
            f"dayPnL={dp if dp is not None else '?'} "
            f"edge={day.get('edge_usd')} beating={day.get('beating_model')} "
            f"risk/trade={risk}% open={open_n}/{max_n}."
        )
        if lot_s:
            parts.append(f"open_lots={lot_s}.")
        if mix_s:
            parts.append(f"mix={mix_s}.")
        if ev is not None:
            parts.append(f"event={ev.kind} {ev.detail}.".strip())
        if prev_strat:
            parts.append(f"prev={prev_strat} sends={prev_sends}.")
        pb = day.get("playbook") if isinstance(day.get("playbook"), dict) else {}
        if pb.get("revision") is not None:
            parts.append(
                f"playbook rev={pb.get('revision')} "
                f"since_write={pb.get('since_write_edge')} "
                f"now_edge={pb.get('now_edge')} "
                f"4h={pb.get('win_4h')}."
            )
            from abcxauto.lab_playbook import format_ledger_line

            ledger = format_ledger_line(pb)
            if ledger:
                parts.append(f"ledger {ledger}.")
    parts.append("send|set_wake.")
    return " ".join(parts)


def _idle_path() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_IDLE_STREAK_PATH", "").strip()
    return Path(raw) if raw else _IDLE_STATE_PATH


def load_idle_streak() -> dict[str, Any]:
    p = _idle_path()
    if not p.is_file():
        return {"count": 0, "top_symbol": "", "last_dismiss": ""}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"count": 0, "top_symbol": "", "last_dismiss": ""}
    except Exception:
        return {"count": 0, "top_symbol": "", "last_dismiss": ""}


def save_idle_streak(state: dict[str, Any]) -> None:
    p = _idle_path()
    try:
        p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("save_idle_streak failed")


def reset_idle_streak() -> None:
    save_idle_streak({"count": 0, "top_symbol": "", "last_dismiss": ""})


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
    from abcxauto.config import ROTATION_THIN_CASH_PCT

    capital_liquidity = {
        "total_cash": round(cash, 2),
        "cash_pct_nl": cash_pct,
        "deployed_long_pct_nl": deployed_pct,
        "cash_thin": bool(cash_pct < float(ROTATION_THIN_CASH_PCT)),
        "note": "Fact — liquidity vs NL; not a hold/sell gate",
    }
    return {
        "n_positions": n,
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "exposure": exposure,
        "capital_liquidity": capital_liquidity,
    }


def hunt_cooldown_remaining(recent_decisions: list[dict], symbol: str) -> int:
    """Cycles of hunt cooldown left for symbol (0 = clear)."""
    if not symbol:
        return 0
    sym = symbol.upper()
    for i, d in enumerate(recent_decisions or []):
        strat = str(d.get("strategy") or d.get("action") or "").lower()
        if strat in ("bracket", "market_bracket"):
            rat = str(d.get("rationale") or "")
            blob = rat.upper() + json.dumps(d.get("outcome") or {}, default=str).upper()
            if sym in blob:
                return max(0, 2 - i)
    return 0


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
    idle_streak: int = 0
    idle_top_symbol: str = ""
    structure_lessons: list[dict] = field(default_factory=list)
    structure_cooldown: dict[str, str] = field(default_factory=dict)
    book: dict[str, Any] = field(default_factory=dict)
    pulse: dict[str, Any] = field(default_factory=dict)
    taken_at: str = ""
    ibkr_live_last: float | None = None
    ibkr_live_symbol: str = ""
    ibkr_live_quotes: dict[str, float] = field(default_factory=dict)
    scan_fetched: list[str] = field(default_factory=list)
    option_facts: list[dict] = field(default_factory=list)
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
            "stop_qty_fact": self.stop_qty_fact,
            "ibkr_live_last": self.ibkr_live_last,
            "ibkr_live_symbol": self.ibkr_live_symbol,
            "ibkr_live_quotes": dict(self.ibkr_live_quotes or {}),
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
            "idle_streak": self.idle_streak,
            "idle_top_symbol": self.idle_top_symbol,
            "structure_lessons": self.structure_lessons[:5],
            "structure_cooldown": dict(self.structure_cooldown),
            "taken_at": self.taken_at,
            "mix": structure_mix(self.positions),
            "open_lots": lot_labels(self.positions),
        }

    def prompt_block(self, *, limit: int = 4500) -> str:
        """Compact WORLD block for the book tool."""
        reg = dict(self.regime or {})
        # Prompt-facing: heuristic mix, not market-regime truth.
        regime_prompt = {
            "session_status": reg.get("session_status"),
            "session_phase": reg.get("session_phase"),
            "feature_mix_bias": reg.get("feature_mix_bias") or reg.get("trend_bias"),
            "vol_proxy": reg.get("vol_proxy"),
            "top_longs": reg.get("top_longs"),
            "top_shorts": reg.get("top_shorts"),
            "avg_heuristic_rank": reg.get("avg_heuristic_rank") or reg.get("avg_opp_score"),
            "note": "heuristic from feature biases — not regime truth",
        }
        body = {
            "cycle": self.cycle,
            "session": self.session_status,
            "regime": regime_prompt,
            "flat": self.flat,
            "needs_protection": self.needs_protection,
            "unprotected": self.unprotected,
            "net_liquidation": self.net_liquidation,
            "daily_pnl": self.daily_pnl,
            "portfolio_risk": self.portfolio_risk,
            "posture": self.effective_posture or self.risk_posture,
            "gates": self.gates,
            "envelope": self.envelope,
            "working_thesis": self.working_thesis[:300],
            "trade_plan": self.trade_plan,
            "trade_plans": list(self.trade_plans[:8]),
            "capacity": dict(self.capacity or {}),
            "exposure": (self.portfolio_risk or {}).get("exposure"),
            "capital_liquidity": (self.portfolio_risk or {}).get("capital_liquidity"),
            "idle_streak": self.idle_streak,
            "structure_cooldown": self.structure_cooldown,
            "scan_tape": [
                {
                    "symbol": o.get("symbol"),
                    "source": o.get("source") or "mda",
                    "freshness": o.get("freshness") or "delayed",
                    "mda_last": o.get("mda_last") or o.get("last"),
                    "dist20": o.get("dist20"),
                    "ret5": o.get("ret5"),
                }
                for o in self.opportunities[:12]
            ],
            "ibkr_live_last": getattr(self, "ibkr_live_last", None),
            "ibkr_live_symbol": getattr(self, "ibkr_live_symbol", None),
            "ibkr_live_quotes": dict(getattr(self, "ibkr_live_quotes", None) or {}),
            "news": [
                f"[{n.get('symbol')}] {n.get('headline')}"
                for n in self.news_items[:8]
                if n.get("headline")
            ],
            "positions": [
                compact_position(p, extra=True) for p in self.positions[:12]
            ],
            "working_orders": compact_working_orders(self.open_orders),
            "stop_qty_fact": self.stop_qty_fact,
            "option_facts": self.option_facts[:8],
        }
        text = "WORLDSTATE:\n" + json.dumps(body, default=str)
        feats = format_scan_tape(self.opportunities)
        try:
            from abcxauto.option_facts import format_option_facts_for_prompt

            opt_block = format_option_facts_for_prompt(self.option_facts)
        except Exception:
            opt_block = ""
        out = (
            text[:limit]
            + "\n\n"
            + QUOTE_SOURCES_BLOCK
            + "\n\n"
            + feats
            + "\n\n"
            + opt_block
        )
        return out[: limit + 2200]


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

    plans = load_trade_plans()
    plan = plans[0] if plans else load_trade_plan()
    plan_dict = plan.to_dict() if plan else None
    plans_dicts = [p.to_dict() for p in plans]
    idle = load_idle_streak()
    regime = _regime_from_opps(opportunities, pulse)
    port_risk = _portfolio_risk(positions, net, total_cash=total_cash)
    try:
        max_open = int(getattr(cfg, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_open = 0
    cap = capacity_fact(positions, max_open_positions=max_open, open_orders=orders)
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
        idle_streak=int(idle.get("count") or 0),
        idle_top_symbol=str(idle.get("top_symbol") or ""),
        structure_lessons=lessons,
        structure_cooldown=cool,
        book=book if isinstance(book, dict) else {},
        pulse=pulse if isinstance(pulse, dict) else {},
        taken_at=str(snap.get("taken_at") or ""),
        option_facts=option_facts,
        fills=list(snap.get("fills") or [])[:12],
        stop_qty_fact=stop_fact,
        book_reconciled=book_reconciled,
        ibkr_live_quotes=dict(snap.get("ibkr_live_quotes") or {}),
        ibkr_live_symbol=str(
            snap.get("ibkr_live_symbol")
            or ("SPY" if "SPY" in (snap.get("ibkr_live_quotes") or {}) else "")
        ),
        ibkr_live_last=(
            snap.get("ibkr_live_last")
            if snap.get("ibkr_live_last") is not None
            else (snap.get("ibkr_live_quotes") or {}).get("SPY")
        ),
    )
    return ws


def capacity_allows_new_risk(world: Any, cfg: Any = None) -> bool:
    cap = getattr(world, "capacity", None) or {}
    if isinstance(cap, dict) and "allows_new_risk" in cap:
        return bool(cap.get("allows_new_risk"))
    c = cfg if cfg is not None else get_config()
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
