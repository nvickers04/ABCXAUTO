"""Read-only gate, fill-quality, and model-spend rollups for the cockpit.

The cockpit calls these functions. It does not write SQL. Writes stay on the
journal hot path; this module only reads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from abcxauto.memory.journal_support import (
    _et_calendar_date,
    _et_day_utc_range,
    _fill_multiplier,
    _split_gate_stage,
    _utc_iso,
)
from abcxauto.scorecard import compute_scorecard, rth_session_start
from abcxauto.send_marks import finite_px, signed_slippage_of
from abcxauto.strategy_params import OPTION_STRATEGIES

logger = logging.getLogger(__name__)

_OPT_SEC = frozenset({"OPT", "FOP", "BAG"})


def _journal(journal: Any = None) -> Any:
    if journal is not None:
        return journal
    from abcxauto.memory import get_journal

    return get_journal()


def _clock(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _session_window(now: datetime | None = None) -> tuple[str, str, str]:
    """(since_iso, until_iso, session_date) for the current RTH session."""
    clock = _clock(now)
    bell, session_date = rth_session_start(clock)
    since = _utc_iso(bell) or ""
    until = _utc_iso(clock) or ""
    return since, until, session_date


def _day_window(now: datetime | None = None) -> tuple[str, str, str]:
    """(since_iso, until_iso, et_date) for the current America/New_York day."""
    clock = _clock(now)
    day = _et_calendar_date(clock) or ""
    bounds = _et_day_utc_range(day) if day else None
    if not bounds:
        stamp = _utc_iso(clock) or ""
        return stamp, stamp, day
    lo, hi = bounds
    until = _utc_iso(clock) or hi
    return lo, until, day


def _rows(journal: Any, sql: str, args: tuple) -> list[dict]:
    if journal is None or not getattr(journal, "enabled", True):
        return []
    try:
        journal._ensure_schema()
        with journal._connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
    except Exception:
        logger.exception("gate_stats query failed")
        return []


def _finite(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    return num


def _stage_of(row: dict) -> str:
    stored = str(row.get("stage") or "").strip()
    if stored:
        return stored
    parsed, _note = _split_gate_stage(row.get("reason"))
    return parsed


def _reason_key(row: dict) -> str:
    _stage, note = _split_gate_stage(row.get("reason"))
    text = note or str(row.get("reason") or "").strip()
    return text or "(none)"


def _example(row: dict) -> dict[str, Any]:
    return {
        "ts": row.get("ts"),
        "stage": _stage_of(row),
        "reason": _reason_key(row),
        "reason_raw": row.get("reason"),
        "symbol": str(row.get("symbol") or ""),
        "strategy": str(row.get("strategy") or ""),
        "proposal_id": row.get("proposal_id"),
        "source": row.get("source"),
    }


def _rollup_rejections(rows: list[dict], *, since: str, until: str, label: str) -> dict:
    by_reason: dict[tuple[str, str], dict] = {}
    by_stage: dict[str, int] = {}
    for row in rows:
        stage = _stage_of(row) or "(none)"
        reason = _reason_key(row)
        key = (stage, reason)
        bucket = by_reason.get(key)
        if bucket is None:
            by_reason[key] = {
                "stage": stage if stage != "(none)" else "",
                "reason": reason,
                "count": 1,
                "latest": _example(row),
            }
        else:
            bucket["count"] += 1
            latest = bucket.get("latest") or {}
            if str(row.get("ts") or "") >= str(latest.get("ts") or ""):
                bucket["latest"] = _example(row)
        by_stage[stage] = by_stage.get(stage, 0) + 1
    reasons = sorted(
        by_reason.values(),
        key=lambda item: (-int(item["count"]), str(item.get("stage") or ""), str(item.get("reason") or "")),
    )
    stages = [
        {
            "stage": stage if stage != "(none)" else "",
            "count": count,
        }
        for stage, count in sorted(by_stage.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "kind": label,
        "since": since,
        "until": until,
        "total": len(rows),
        "by_reason": reasons,
        "by_stage": stages,
    }


def gate_rejections(*, journal: Any = None, now: datetime | None = None) -> dict:
    """Refused gates rolled up by reason and stage for this RTH session and ET day.

    Each reason carries a count and the most recent example (symbol, strategy,
    stage, ts). ``stage`` is the column when present, else the clerk
    ``stage: note`` prefix.
    """
    j = _journal(journal)
    sess_lo, sess_hi, session_date = _session_window(now)
    day_lo, day_hi, day = _day_window(now)
    sql = """
        SELECT g.ts, g.proposal_id, g.reason, g.stage,
               p.symbol, p.strategy, p.source
        FROM gate_decisions g
        LEFT JOIN proposals p ON p.id = g.proposal_id
        WHERE g.allowed = 0
          AND g.ts >= ? AND g.ts < ?
        ORDER BY g.id DESC
    """
    session_rows = _rows(j, sql, (sess_lo, sess_hi))
    day_rows = _rows(j, sql, (day_lo, day_hi))
    session = _rollup_rejections(session_rows, since=sess_lo, until=sess_hi, label="session")
    session["session_date"] = session_date
    day_out = _rollup_rejections(day_rows, since=day_lo, until=day_hi, label="day")
    day_out["day"] = day
    return {"session": session, "day": day_out}


def _optionish(sec_type: Any, strategy: Any, multiplier: float) -> bool:
    sec = str(sec_type or "").upper()
    if sec in _OPT_SEC:
        return True
    if multiplier and multiplier != 1.0:
        return True
    return str(strategy or "").strip().lower() in OPTION_STRATEGIES


def _multiplier_of(row: dict) -> float:
    stored = _finite(row.get("multiplier"))
    if stored is not None and stored > 0:
        return stored
    if _optionish(row.get("sec_type"), row.get("strategy"), 0.0):
        return 100.0
    return _fill_multiplier(row)


def _benchmark(sent: Optional[float], last: Optional[float]) -> Optional[str]:
    if sent is None and last is None:
        return None
    if sent is None:
        return "last"
    if last is not None and abs(sent - last) <= 1e-9:
        return "last"
    return "limit"


def _fill_row(row: dict) -> dict[str, Any]:
    fill_px = finite_px(row.get("fill_price") if row.get("fill_price") is not None else row.get("price"))
    sent = finite_px(row.get("sent_price"))
    last = finite_px(row.get("ibkr_last") if row.get("ibkr_last") is not None else row.get("mark_last"))
    if sent is None:
        sent = finite_px(row.get("mark_sent"))
    if last is None:
        last = finite_px(row.get("mark_last"))
    slip = _finite(row.get("signed_slippage"))
    if slip is None:
        slip = _finite(row.get("mark_slippage"))
    if slip is None:
        slip = signed_slippage_of(
            fill_price=fill_px,
            sent_price=sent if sent is not None else last,
            side=row.get("side"),
            mid=last,
        )
    qty = _finite(row.get("quantity"))
    if qty is None:
        qty = 0.0
    mult = _multiplier_of(row)
    slip_usd = None if slip is None else round(float(slip) * abs(qty) * float(mult), 6)
    bench_sent = sent if sent is not None else last
    return {
        "ts": row.get("ts"),
        "exec_id": row.get("exec_id"),
        "order_id": row.get("order_id"),
        "symbol": str(row.get("symbol") or ""),
        "sec_type": row.get("sec_type"),
        "strategy": row.get("strategy") or "",
        "side": row.get("side"),
        "quantity": qty,
        "fill_price": fill_px,
        "sent_price": bench_sent,
        "ibkr_last": last,
        "benchmark": _benchmark(sent, last),
        "signed_slippage": slip,
        "multiplier": mult,
        "slippage_usd": slip_usd,
        "fill_label": row.get("fill_label"),
    }


def _aggregate_fills(fills: list[dict]) -> dict[str, Any]:
    priced = [row for row in fills if row.get("signed_slippage") is not None]
    usd = [float(row["slippage_usd"]) for row in priced if row.get("slippage_usd") is not None]
    slips = [float(row["signed_slippage"]) for row in priced]
    adverse = sum(1 for s in slips if s > 0)
    favor = sum(1 for s in slips if s < 0)
    flat = sum(1 for s in slips if s == 0)
    return {
        "n": len(fills),
        "n_priced": len(priced),
        "mean_slippage": None if not slips else round(sum(slips) / len(slips), 6),
        "sum_slippage_usd": None if not usd else round(sum(usd), 6),
        "mean_slippage_usd": None if not usd else round(sum(usd) / len(usd), 6),
        "adverse_n": adverse,
        "favorable_n": favor,
        "flat_n": flat,
    }


def _fill_quality_window(journal: Any, since: str, until: str, *, label: str) -> dict:
    sql = """
        SELECT f.ts, f.exec_id, f.order_id, f.symbol, f.sec_type, f.side,
               f.quantity, f.price, f.ibkr_last, f.sent_price, f.signed_slippage,
               f.fill_label, f.multiplier,
               sm.sent_price AS mark_sent, sm.ibkr_last AS mark_last,
               sm.signed_slippage AS mark_slippage, sm.strategy
        FROM fills f
        LEFT JOIN send_marks sm ON sm.id = (
            SELECT id FROM send_marks
            WHERE order_id = f.order_id
            ORDER BY id DESC LIMIT 1
        )
        WHERE f.ts >= ? AND f.ts < ?
        ORDER BY f.id DESC
    """
    raw = _rows(journal, sql, (since, until))
    fills = [_fill_row(row) for row in raw]
    out = {
        "kind": label,
        "since": since,
        "until": until,
        "fills": fills,
        "aggregate": _aggregate_fills(fills),
    }
    return out


def fill_quality(*, journal: Any = None, now: datetime | None = None) -> dict:
    """Fill vs intended limit/last at send. Options use the contract multiplier.

    Per-fill ``signed_slippage`` is adverse-positive (BUY fill − bench).
    ``slippage_usd`` is that print × |qty| × multiplier (100 on OPT/FOP/BAG).
    """
    j = _journal(journal)
    sess_lo, sess_hi, session_date = _session_window(now)
    day_lo, day_hi, day = _day_window(now)
    session = _fill_quality_window(j, sess_lo, sess_hi, label="session")
    session["session_date"] = session_date
    day_out = _fill_quality_window(j, day_lo, day_hi, label="day")
    day_out["day"] = day
    return {"session": session, "day": day_out}


def _spend_from_session(sess: Any) -> dict[str, Any]:
    if not isinstance(sess, dict) or not sess:
        return {
            "kind": "session",
            "model_cost_usd": 0.0,
            "model_calls": 0,
            "book_pnl": None,
            "book_return_pct": None,
            "edge_usd": None,
            "beating_model": None,
        }
    cost = sess.get("model_cost_usd")
    try:
        cost_f = float(cost or 0.0)
    except (TypeError, ValueError):
        cost_f = 0.0
    pnl = sess.get("book_pnl")
    try:
        pnl_f = float(pnl) if pnl is not None else None
    except (TypeError, ValueError):
        pnl_f = None
    edge = sess.get("edge_usd")
    if edge is None and pnl_f is not None:
        edge = pnl_f - cost_f
    beat = sess.get("beating_model")
    if beat is None and edge is not None:
        beat = edge > 0
    return {
        "kind": "session",
        "session_date": sess.get("session_date"),
        "started_at": sess.get("started_at"),
        "model": sess.get("model"),
        "startup_nl": sess.get("startup_nl"),
        "end_nl": sess.get("end_nl"),
        "model_cost_usd": cost_f,
        "model_cost_pct": sess.get("model_cost_pct"),
        "model_calls": int(sess.get("model_calls") or 0),
        "book_pnl": pnl_f,
        "book_return_pct": sess.get("book_return_pct"),
        "edge_usd": edge,
        "edge_pct": sess.get("edge_pct"),
        "beating_model": beat,
    }


def _et_day_spend(journal: Any, scorecard: dict, now: datetime | None) -> dict[str, Any]:
    """Calendar-day spend vs book, using the same journal facts scorecard uses."""
    day_lo, day_hi, day = _day_window(now)
    usage = {
        "calls": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    try:
        if hasattr(journal, "model_usage_since"):
            usage = dict(journal.model_usage_since(day_lo) or usage)
    except Exception:
        pass
    start_nl = None
    start_ts = None
    try:
        if hasattr(journal, "first_nl_on_et_day") and day:
            start_nl, start_ts = journal.first_nl_on_et_day(day)
    except Exception:
        start_nl, start_ts = None, None
    current = scorecard.get("net_liquidation")
    book_pnl = None
    book_ret = None
    if current is not None and start_nl is not None:
        try:
            book_pnl = float(current) - float(start_nl)
            if float(start_nl) > 0:
                book_ret = (book_pnl / float(start_nl)) * 100.0
        except (TypeError, ValueError):
            book_pnl = None
            book_ret = None
    cost = float(usage.get("cost_usd") or 0.0)
    edge = None if book_pnl is None else book_pnl - cost
    start_base = float(start_nl) if start_nl else None
    from abcxauto.scorecard import _pct_of_start

    return {
        "kind": "day",
        "day": day,
        "since": day_lo,
        "until": day_hi,
        "startup_nl": start_base,
        "start_ts": start_ts,
        "end_nl": current,
        "model_cost_usd": cost,
        "model_cost_pct": _pct_of_start(cost, start_base),
        "model_calls": int(usage.get("calls") or 0),
        "book_pnl": book_pnl,
        "book_return_pct": book_ret,
        "edge_usd": edge,
        "edge_pct": _pct_of_start(edge, start_base),
        "beating_model": None if edge is None else edge > 0,
    }


def model_spend(
    *,
    journal: Any = None,
    equity: float | None = None,
    now: datetime | None = None,
) -> dict:
    """Session and ET-day model cost against book return.

    Session is ``compute_scorecard``'s RTH row. Day uses the same journal
    usage/NAV readers; it does not invent a second cost formula.
    """
    j = _journal(journal)
    sc = compute_scorecard(equity=equity, journal=j, now=now)
    return {
        "session": _spend_from_session(sc.get("session")),
        "day": _et_day_spend(j, sc, now),
    }
