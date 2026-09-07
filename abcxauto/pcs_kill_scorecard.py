"""PCS Arm v0 N=20 Kill Scorecard — session logging + window helpers.

Measurement / journal infrastructure only. Does not look, send, or open the
desk. Locked field names and reject rules match the PCS-skew arm v0 N=20
kill scorecard; STRESS_PASS is a different verdict and must not be reused.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

SESSION_ID_RE = re.compile(r"^pcs-v0-(\d{8})-(\d{2})$")

FILL_MARKS = frozenset({"ibkr_fill", "lambda_ba"})
SEND_VALUES = frozenset({"SEND", "NO_SEND"})

# Coded send_reason prefixes (locked). ``NO_SEND:other:`` may carry a suffix.
SEND_REASON_OK = frozenset(
    {
        "SEND:gate_clear",
        "NO_SEND:f10",
        "NO_SEND:dd_fuse",
        "NO_SEND:qty0_streak",
        "NO_SEND:credit_lt_floor",
        "NO_SEND:max_loss_gt_budget",
        "NO_SEND:lambda_breach",
        "NO_SEND:model_cost_cap",
        "NO_SEND:no_setup",
    }
)

ABORT_FUSES = frozenset({"none", "F10", "DD30", "QTY0_STREAK"})
VERDICTS = frozenset({"PASS", "FAIL", "ABORT"})

QTY0_STREAK_LIMIT_DEFAULT = 5
DD30_PCT = 30.0

REQUIRED_FIELDS = (
    "session_id",
    "session_date",
    "NL",
    "conservative_pnl",
    "model_cost",
    "lambda",
    "credit",
    "max_loss",
    "send",
    "send_reason",
    "qty",
    "f10_ok",
    "dd_pct",
)

# Accept λ as alias for lambda in caller payloads.
_LAMBDA_KEYS = ("lambda", "λ")


def chicago_session_date(now: datetime | None = None) -> str:
    """ISO calendar date in America/Chicago."""
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(CHICAGO).date().isoformat()


def make_session_id(session_date: str, nn: int) -> str:
    """Build ``pcs-v0-YYYYMMDD-NN`` from an ISO Chicago date and 1-based index."""
    day = str(session_date or "").strip()
    if len(day) >= 10 and day[4] == "-" and day[7] == "-":
        ymd = day[:4] + day[5:7] + day[8:10]
    else:
        digits = "".join(ch for ch in day if ch.isdigit())
        ymd = digits[:8]
    if len(ymd) != 8:
        raise ValueError(f"session_date must be ISO YYYY-MM-DD, got {session_date!r}")
    n = int(nn)
    if n < 1 or n > 99:
        raise ValueError(f"session index NN must be 01..99, got {nn!r}")
    return f"pcs-v0-{ymd}-{n:02d}"


def _as_float(raw: Any, *, name: str, allow_none: bool = False) -> float | None:
    if raw is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if val != val:  # NaN
        raise ValueError(f"{name} must be finite")
    return val


def _as_bool(raw: Any, *, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "0", "1"):
        return raw.strip().lower() in ("true", "1")
    raise ValueError(f"{name} must be bool")


def _send_reason_ok(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    if text in SEND_REASON_OK:
        return True
    return text.startswith("NO_SEND:other:")


def _pick_lambda(payload: Mapping[str, Any]) -> Any:
    for key in _LAMBDA_KEYS:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def validate_fill_mark(fill_mark: Any) -> tuple[bool, str | None]:
    """Mid (or unknown) marks are INVALID — never SEND credit."""
    mark = str(fill_mark or "").strip().lower() if fill_mark is not None else ""
    if mark == "mid":
        return False, "fill_mark=mid"
    if mark not in FILL_MARKS:
        return False, f"fill_mark={mark or 'missing'}"
    return True, None


def compute_derived(
    *,
    NL: float,
    conservative_pnl: float,
    model_cost: float,
    credit: float,
    max_loss: float,
) -> dict[str, float | None]:
    rr = None
    if max_loss not in (0, 0.0):
        try:
            rr = float(credit) / float(max_loss)
        except (TypeError, ValueError, ZeroDivisionError):
            rr = None
    return {
        "session_edge_conservative": float(conservative_pnl) - float(model_cost),
        "nl_incl_model": float(NL) - float(model_cost),
        "rr_credit": rr,
    }


def compute_diagnose(
    *,
    session_start_NL: float,
    conservative_pnl: float,
    model_cost: float,
    fill_mark: str,
    ba_commission_USD: float,
) -> dict[str, Any]:
    """Diagnose % columns. ``model_cost`` null is fail-closed upstream."""
    base = float(session_start_NL)
    if base <= 0:
        raise ValueError("session_start_NL must be > 0 for diagnose %")
    book_return_pct = (float(conservative_pnl) / base) * 100.0
    model_cost_pct = (float(model_cost) / base) * 100.0
    return {
        "session_start_NL": base,
        "book_return_pct": book_return_pct,
        "model_cost_USD": float(model_cost),
        "model_cost_pct": model_cost_pct,
        "session_score": book_return_pct - model_cost_pct,
        "fill_mark": fill_mark,
        "ba_commission_USD": float(ba_commission_USD),
    }


def normalize_session_row(
    payload: Mapping[str, Any],
    *,
    strict_send_credit: bool = True,
) -> dict[str, Any]:
    """Validate + attach derived / Diagnose fields.

    Returns a row dict with ``valid`` bool. Invalid rows stay measurable for
    ``invalid_rows`` / fuse accounting but never receive SEND credit.
    """
    errors: list[str] = []
    row: dict[str, Any] = {}

    session_id = str(payload.get("session_id") or "").strip()
    if not SESSION_ID_RE.match(session_id):
        errors.append("session_id")
    row["session_id"] = session_id

    session_date = str(payload.get("session_date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", session_date):
        errors.append("session_date")
    row["session_date"] = session_date

    try:
        row["NL"] = _as_float(payload.get("NL"), name="NL")
    except ValueError:
        errors.append("NL")
        row["NL"] = None

    try:
        row["conservative_pnl"] = _as_float(
            payload.get("conservative_pnl"), name="conservative_pnl"
        )
    except ValueError:
        errors.append("conservative_pnl")
        row["conservative_pnl"] = None

    optimistic = payload.get("optimistic_pnl", payload.get("optimistic"))
    if optimistic is not None and row["conservative_pnl"] is not None:
        try:
            opt = _as_float(optimistic, name="optimistic_pnl")
            row["optimistic_pnl"] = opt
            if opt is not None and row["conservative_pnl"] > opt:
                errors.append("conservative_pnl>optimistic")
        except ValueError:
            errors.append("optimistic_pnl")

    # model_cost null ⇒ fail-closed INVALID (unbilled)
    raw_cost = payload.get("model_cost", payload.get("model_cost_USD"))
    if raw_cost is None:
        errors.append("model_cost_null")
        row["model_cost"] = None
    else:
        try:
            cost = _as_float(raw_cost, name="model_cost")
            if cost is None or cost < 0:
                errors.append("model_cost<0")
                row["model_cost"] = cost
            else:
                row["model_cost"] = cost
        except ValueError:
            errors.append("model_cost")
            row["model_cost"] = None

    try:
        row["lambda"] = _as_float(_pick_lambda(payload), name="lambda")
    except ValueError:
        errors.append("lambda")
        row["lambda"] = None
    if "λ" in payload:
        row["λ"] = row["lambda"]

    try:
        row["credit"] = _as_float(payload.get("credit"), name="credit")
    except ValueError:
        errors.append("credit")
        row["credit"] = None

    try:
        row["max_loss"] = _as_float(payload.get("max_loss"), name="max_loss")
    except ValueError:
        errors.append("max_loss")
        row["max_loss"] = None

    send = str(payload.get("send") or "").strip().upper()
    if send not in SEND_VALUES:
        errors.append("send")
    row["send"] = send or None

    send_reason = str(payload.get("send_reason") or "").strip()
    if not _send_reason_ok(send_reason):
        errors.append("send_reason")
    row["send_reason"] = send_reason

    try:
        row["qty"] = _as_float(payload.get("qty"), name="qty")
    except ValueError:
        errors.append("qty")
        row["qty"] = None

    try:
        row["f10_ok"] = _as_bool(payload.get("f10_ok"), name="f10_ok")
    except ValueError:
        errors.append("f10_ok")
        row["f10_ok"] = None

    if "f10_tripped" in payload:
        try:
            row["f10_tripped"] = _as_bool(payload.get("f10_tripped"), name="f10_tripped")
        except ValueError:
            row["f10_tripped"] = None
    if "loop_halted" in payload:
        try:
            row["loop_halted"] = _as_bool(payload.get("loop_halted"), name="loop_halted")
        except ValueError:
            row["loop_halted"] = None
    if "model_cost_post_trip_USD" in payload or "model_cost_post_trip_usd" in payload:
        raw = payload.get("model_cost_post_trip_USD", payload.get("model_cost_post_trip_usd"))
        try:
            row["model_cost_post_trip_USD"] = _as_float(raw, name="model_cost_post_trip_USD")
        except ValueError:
            row["model_cost_post_trip_USD"] = 0.0

    try:
        row["dd_pct"] = _as_float(payload.get("dd_pct"), name="dd_pct")
    except ValueError:
        errors.append("dd_pct")
        row["dd_pct"] = None

    if "notes" in payload and payload.get("notes") is not None:
        row["notes"] = str(payload.get("notes"))

    fill_ok, fill_err = validate_fill_mark(payload.get("fill_mark"))
    fill_mark = str(payload.get("fill_mark") or "").strip().lower()
    row["fill_mark"] = fill_mark or None
    if not fill_ok:
        errors.append(fill_err or "fill_mark")

    # Diagnose base: require or compute from NL
    start_nl_raw = payload.get("session_start_NL", payload.get("session_start_nl"))
    if start_nl_raw is None:
        start_nl_raw = row.get("NL")
    try:
        session_start_NL = _as_float(start_nl_raw, name="session_start_NL")
    except ValueError:
        session_start_NL = None
        errors.append("session_start_NL")

    ba_raw = payload.get("ba_commission_USD", payload.get("ba_commission_usd", 0.0))
    try:
        ba_commission = _as_float(ba_raw, name="ba_commission_USD")
    except ValueError:
        ba_commission = None
        errors.append("ba_commission_USD")

    valid = not errors
    row["valid"] = valid
    row["invalid_reasons"] = list(errors)

    # SEND credit only on valid rows; mid / null model_cost never credit SEND.
    send_credited = bool(valid and send == "SEND")
    if strict_send_credit and send == "SEND" and not valid:
        send_credited = False
    row["send_credited"] = send_credited

    if (
        row.get("NL") is not None
        and row.get("conservative_pnl") is not None
        and row.get("model_cost") is not None
        and row.get("credit") is not None
        and row.get("max_loss") is not None
    ):
        row.update(
            compute_derived(
                NL=float(row["NL"]),
                conservative_pnl=float(row["conservative_pnl"]),
                model_cost=float(row["model_cost"]),
                credit=float(row["credit"]),
                max_loss=float(row["max_loss"]),
            )
        )
    else:
        row.setdefault("session_edge_conservative", None)
        row.setdefault("nl_incl_model", None)
        row.setdefault("rr_credit", None)

    if (
        session_start_NL is not None
        and session_start_NL > 0
        and row.get("conservative_pnl") is not None
        and row.get("model_cost") is not None
        and ba_commission is not None
        and fill_ok
    ):
        row.update(
            compute_diagnose(
                session_start_NL=float(session_start_NL),
                conservative_pnl=float(row["conservative_pnl"]),
                model_cost=float(row["model_cost"]),
                fill_mark=fill_mark,
                ba_commission_USD=float(ba_commission),
            )
        )
    else:
        row["session_start_NL"] = session_start_NL
        row["book_return_pct"] = None
        row["model_cost_USD"] = row.get("model_cost")
        row["model_cost_pct"] = None
        row["session_score"] = None
        row["ba_commission_USD"] = ba_commission

    return row


def _qty0_streak_max(rows: Iterable[Mapping[str, Any]]) -> int:
    best = 0
    cur = 0
    for row in rows:
        qty = row.get("qty")
        try:
            is_zero = qty is not None and float(qty) == 0.0
        except (TypeError, ValueError):
            is_zero = False
        if is_zero:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def window_aggregates(
    rows: Iterable[Mapping[str, Any]],
    *,
    qty0_streak_limit: int = QTY0_STREAK_LIMIT_DEFAULT,
    dd_abort_pct: float = DD30_PCT,
    normalize: bool = True,
) -> dict[str, Any]:
    """Compute N-window helpers + PASS/FAIL/ABORT (not STRESS_PASS)."""
    limit = max(1, int(qty0_streak_limit))
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if normalize:
            normalized.append(normalize_session_row(raw))
        else:
            normalized.append(dict(raw))

    invalid_rows = sum(1 for r in normalized if not r.get("valid"))
    valid = [r for r in normalized if r.get("valid")]

    def _sum(key: str) -> float:
        total = 0.0
        for r in valid:
            val = r.get(key)
            if val is None:
                continue
            total += float(val)
        return total

    sum_NL = _sum("NL")
    sum_conservative_pnl = _sum("conservative_pnl")
    sum_model_cost = _sum("model_cost")
    net_conservative = sum_conservative_pnl - sum_model_cost

    credited_sends = sum(1 for r in normalized if r.get("send_credited"))
    n_all = len(normalized)
    send_rate = (credited_sends / n_all) if n_all else 0.0

    lambdas = [float(r["lambda"]) for r in valid if r.get("lambda") is not None]
    mean_lambda = (sum(lambdas) / len(lambdas)) if lambdas else None

    dd_vals = [float(r["dd_pct"]) for r in valid if r.get("dd_pct") is not None]
    max_dd_pct = max(dd_vals) if dd_vals else None

    qty0_streak_max = _qty0_streak_max(normalized)
    f10_breach_count = sum(1 for r in normalized if r.get("f10_ok") is False)

    scores = [
        float(r["session_score"])
        for r in valid
        if r.get("session_score") is not None
    ]
    mean_score = (sum(scores) / len(scores)) if scores else None

    abort_fuse = "none"
    if f10_breach_count > 0:
        abort_fuse = "F10"
    if max_dd_pct is not None and max_dd_pct >= float(dd_abort_pct):
        abort_fuse = "DD30"
    if qty0_streak_max >= limit:
        abort_fuse = "QTY0_STREAK"

    # Precedence when multiple trip: QTY0 > DD30 > F10 (last assignment wins
    # above). Recompute with explicit priority so the fuse name is stable.
    if qty0_streak_max >= limit:
        abort_fuse = "QTY0_STREAK"
    elif max_dd_pct is not None and max_dd_pct >= float(dd_abort_pct):
        abort_fuse = "DD30"
    elif f10_breach_count > 0:
        abort_fuse = "F10"
    else:
        abort_fuse = "none"

    if abort_fuse != "none":
        verdict = "ABORT"
    elif n_all == 0 or invalid_rows == n_all:
        verdict = "FAIL"
    elif mean_score is not None and mean_score > 0 and net_conservative > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"

    cancel_all_invoked = False
    working_orders_after_abort = None
    if abort_fuse != "none":
        try:
            from abcxauto.abort_fuse import abort_cancel_overlay

            overlay = abort_cancel_overlay()
            cancel_all_invoked = bool(overlay.get("cancel_all_invoked"))
            working_orders_after_abort = overlay.get("working_orders_after_abort")
        except Exception:
            cancel_all_invoked = False
            working_orders_after_abort = None

    return {
        "n": n_all,
        "n_valid": len(valid),
        "sum_NL": sum_NL,
        "sum_conservative_pnl": sum_conservative_pnl,
        "sum_model_cost": sum_model_cost,
        "net_conservative": net_conservative,
        "send_rate": send_rate,
        "send_credited": credited_sends,
        "mean_lambda": mean_lambda,
        "mean_λ": mean_lambda,
        "max_dd_pct": max_dd_pct,
        "qty0_streak_max": qty0_streak_max,
        "qty0_streak_limit": limit,
        "f10_breach_count": f10_breach_count,
        "invalid_rows": invalid_rows,
        "mean_session_score": mean_score,
        "verdict": verdict,
        "abort_fuse": abort_fuse,
        "cancel_all_invoked": cancel_all_invoked,
        "working_orders_after_abort": working_orders_after_abort,
        "rows": normalized,
    }


def record_ready_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a session payload for journal persist."""
    return normalize_session_row(payload)
