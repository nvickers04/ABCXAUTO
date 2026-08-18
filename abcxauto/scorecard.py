"""Primary scorecard: book return % of starting NetLiq vs model API cost."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Short → long. Grok picks which window is enough; code only reports facts.
# Promote / inception beating stays the full-book row on compute_scorecard.
HORIZONS: tuple[tuple[str, int | None], ...] = (
    ("15m", 15 * 60),
    ("1h", 3600),
    ("4h", 4 * 3600),
    ("1d", 86400),
    ("1w", 7 * 86400),
    ("1m", 30 * 86400),
    ("inception", None),
)

# grok-4.6 list rates (xAI, <200k prompt). Override via env if billing differs.
_DEFAULT_IN_USD_PER_MTOK = 2.0
_DEFAULT_OUT_USD_PER_MTOK = 6.0
_DEFAULT_CACHED_USD_PER_MTOK = 0.5
_LONG_PROMPT_TOKENS = 200_000


def _cfg_float(name: str, default: float) -> float:
    import os

    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    cached_tokens: int = 0,
    in_rate: float | None = None,
    out_rate: float | None = None,
    cached_rate: float | None = None,
) -> float:
    inn_n = max(0, int(input_tokens or 0))
    out_n = max(0, int(output_tokens or 0))
    cached_n = max(0, int(cached_tokens or 0))
    prompt = inn_n + cached_n
    long = prompt >= _LONG_PROMPT_TOKENS
    inn = in_rate if in_rate is not None else _cfg_float(
        "ABCXAUTO_MODEL_INPUT_USD_PER_MTOK", _DEFAULT_IN_USD_PER_MTOK
    )
    out = out_rate if out_rate is not None else _cfg_float(
        "ABCXAUTO_MODEL_OUTPUT_USD_PER_MTOK", _DEFAULT_OUT_USD_PER_MTOK
    )
    cached = cached_rate if cached_rate is not None else _cfg_float(
        "ABCXAUTO_MODEL_CACHED_USD_PER_MTOK", _DEFAULT_CACHED_USD_PER_MTOK
    )
    if long and in_rate is None:
        inn *= 2.0
    if long and out_rate is None:
        out *= 2.0
    if long and cached_rate is None:
        cached *= 2.0
    return (
        (inn_n / 1_000_000.0) * inn
        + (cached_n / 1_000_000.0) * cached
        + (out_n / 1_000_000.0) * out
    )


def estimate_tokens(text: str) -> int:
    """Rough token count when the SDK does not return usage."""
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def _usage_int(src: Any, *names: str) -> int:
    if src is None:
        return 0
    for name in names:
        if "." in name:
            cur: Any = src
            ok = True
            for part in name.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = getattr(cur, part, None)
                if cur is None:
                    ok = False
                    break
            raw = cur if ok else None
        elif isinstance(src, dict):
            raw = src.get(name)
        else:
            raw = getattr(src, name, None)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


def usage_from_response(
    resp: Any,
    *extra: Any,
    think_text: str = "",
    say_text: str = "",
) -> dict[str, int]:
    """Pull prompt/completion/cached/reasoning from an xAI response, else estimate."""
    blob = None
    for obj in (resp, *extra):
        if obj is None:
            continue
        if isinstance(obj, dict) and obj.get("usage") is not None:
            blob = obj.get("usage")
            break
        got = getattr(obj, "usage", None)
        if got is not None:
            blob = got
            break
        if isinstance(obj, dict) and (
            obj.get("prompt_tokens") is not None or obj.get("input_tokens") is not None
        ):
            blob = obj
            break
    inn = _usage_int(blob, "prompt_tokens", "input_tokens", "prompt_text_tokens")
    cached = _usage_int(
        blob,
        "cached_tokens",
        "cached_prompt_tokens",
        "prompt_tokens_details.cached_tokens",
    )
    out = _usage_int(blob, "completion_tokens", "output_tokens")
    reason = _usage_int(
        blob,
        "reasoning_tokens",
        "completion_tokens_details.reasoning_tokens",
    )
    if blob is None or (inn == 0 and out == 0 and reason == 0):
        out = estimate_tokens(say_text) if say_text else 0
        reason = estimate_tokens(think_text) if think_text else 0
        return {
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": out + reason,
            "reasoning_tokens": reason,
        }
    billed_out = out if out else reason
    return {
        "input_tokens": inn,
        "cached_tokens": cached,
        "output_tokens": billed_out,
        "reasoning_tokens": reason,
    }


def _utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _window_row(
    *,
    label: str,
    horizon_s: int | None,
    start_nl: float | None,
    start_ts: str | None,
    current: float | None,
    usage: dict[str, Any],
    snaps: int,
    now: datetime,
) -> dict[str, Any]:
    book_pnl = None
    book_return_pct = None
    if current is not None and start_nl is not None and start_nl > 0:
        book_pnl = float(current) - float(start_nl)
        book_return_pct = (book_pnl / float(start_nl)) * 100.0
    cost = float(usage.get("cost_usd") or 0.0)
    edge = None if book_pnl is None else (book_pnl - cost)
    beating = None if edge is None else (edge > 0)
    span_s = None
    if start_ts:
        try:
            st = datetime.fromisoformat(str(start_ts).replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            span_s = max(0.0, (now - st.astimezone(timezone.utc)).total_seconds())
        except Exception:
            span_s = None
    if span_s is None and horizon_s:
        span_s = float(horizon_s)
    coverage = "ok"
    if horizon_s is None:
        coverage = "ok"
    elif start_nl is None:
        coverage = "none"
    elif span_s is not None and span_s < 0.5 * float(horizon_s):
        coverage = "thin"
    hours = (span_s / 3600.0) if span_s and span_s > 0 else None
    edge_per_hour = (edge / hours) if edge is not None and hours else None
    return {
        "label": label,
        "horizon_s": horizon_s,
        "span_s": span_s,
        "coverage": coverage,
        "start_nl": float(start_nl) if start_nl else None,
        "start_ts": start_ts,
        "book_pnl": book_pnl,
        "book_return_pct": book_return_pct,
        "model_cost_usd": cost,
        "model_calls": int(usage.get("calls") or 0),
        "edge_usd": edge,
        "edge_per_hour": edge_per_hour,
        "beating_model": beating,
        "snaps": int(snaps or 0),
    }


def compute_scorecard(
    *,
    equity: float | None = None,
    journal: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Book P&L vs model cost, scored as % of starting NetLiq.

    Top-level beating_model is inception (promote / floor). ``windows`` are
    shorter looks; ``fastest_beating`` is the shortest non-thin window that
    is ahead of the model bill. Grok chooses which window is enough.
    """
    if journal is None:
        try:
            from abcxauto.memory import get_journal

            journal = get_journal()
        except Exception:
            journal = None

    startup = None
    current = equity
    usage = {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    if journal is not None:
        try:
            if hasattr(journal, "startup_cash"):
                startup = journal.startup_cash()
        except Exception:
            startup = None
        try:
            if current is None and hasattr(journal, "account_performance"):
                perf = journal.account_performance() or {}
                nl = perf.get("net_liquidation")
                if nl is not None:
                    current = float(nl)
        except Exception:
            pass
        try:
            if hasattr(journal, "model_usage_totals"):
                usage = dict(journal.model_usage_totals() or usage)
        except Exception:
            pass

    book_pnl = None
    book_return_pct = None
    if current is not None and startup is not None and startup > 0:
        try:
            current = float(current)
            book_pnl = current - float(startup)
            book_return_pct = (book_pnl / float(startup)) * 100.0
        except (TypeError, ValueError):
            current = None
            book_pnl = None

    cost = float(usage.get("cost_usd") or 0.0)
    edge = None if book_pnl is None else (book_pnl - cost)
    beating = None if edge is None else (edge > 0)

    clock = _utc(now)
    windows: dict[str, dict[str, Any]] = {}
    if journal is not None:
        for label, horizon_s in HORIZONS:
            if horizon_s is None:
                start_ts = None
                try:
                    if hasattr(journal, "first_snapshot"):
                        _nl, start_ts = journal.first_snapshot()
                except Exception:
                    start_ts = None
                windows[label] = _window_row(
                    label=label,
                    horizon_s=None,
                    start_nl=startup,
                    start_ts=start_ts,
                    current=current,
                    usage=usage,
                    snaps=0,
                    now=clock,
                )
                continue
            cutoff = clock - timedelta(seconds=int(horizon_s))
            cutoff_iso = _iso(cutoff)
            start_nl, start_ts = None, None
            try:
                if hasattr(journal, "nav_at_or_before"):
                    start_nl, start_ts = journal.nav_at_or_before(cutoff_iso)
            except Exception:
                start_nl, start_ts = None, None
            win_usage = dict(usage)
            try:
                if hasattr(journal, "model_usage_since"):
                    win_usage = dict(journal.model_usage_since(cutoff_iso) or win_usage)
            except Exception:
                pass
            snaps = 0
            try:
                if hasattr(journal, "snapshot_count_since"):
                    snaps = int(journal.snapshot_count_since(cutoff_iso) or 0)
            except Exception:
                snaps = 0
            windows[label] = _window_row(
                label=label,
                horizon_s=int(horizon_s),
                start_nl=start_nl,
                start_ts=start_ts,
                current=current,
                usage=win_usage,
                snaps=snaps,
                now=clock,
            )

    fastest_beating = None
    best_pace = None
    best_pace_val = None
    for label, _h in HORIZONS:
        row = windows.get(label) or {}
        if row.get("coverage") != "ok":
            continue
        if row.get("beating_model") is True and fastest_beating is None:
            fastest_beating = label
        eph = row.get("edge_per_hour")
        if (
            row.get("beating_model") is True
            and eph is not None
            and (best_pace_val is None or eph > best_pace_val)
        ):
            best_pace_val = eph
            best_pace = label

    return {
        "startup_cash": float(startup) if startup else None,
        "net_liquidation": current,
        "book_pnl": book_pnl,
        "book_return_pct": book_return_pct,
        "model_calls": int(usage.get("calls") or 0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "model_cost_usd": cost,
        "edge_usd": edge,
        "beating_model": beating,
        "goal": "book return % of starting NetLiq > cost of the model",
        "windows": windows,
        "fastest_beating": fastest_beating,
        "best_pace": best_pace,
    }


def format_scorecard_block(
    *,
    equity: float | None = None,
    journal: Any = None,
    sc: dict[str, Any] | None = None,
) -> str:
    sc = sc if isinstance(sc, dict) and sc else compute_scorecard(equity=equity, journal=journal)
    beat = sc.get("beating_model")
    if beat is True:
        verdict = "BEATING the model bill"
    elif beat is False:
        verdict = "LOSING to the model bill"
    else:
        verdict = "insufficient book history"
    pnl = sc.get("book_pnl")
    pnl_s = f"{pnl:+.2f}" if pnl is not None else "n/a"
    ret = sc.get("book_return_pct")
    ret_s = f"{ret:+.2f}%" if ret is not None else "n/a"
    edge = sc.get("edge_usd")
    edge_s = f"{edge:+.2f}" if edge is not None else "n/a"
    start = sc.get("startup_cash")
    start_s = f"{start:.2f}" if start is not None else "n/a"
    lines = [
        "SCORECARD:",
        f"- first_NL={start_s} NL={sc.get('net_liquidation')}",
        f"- book_pnl={pnl_s} ({ret_s} of starting NetLiq)",
        f"- model_cost=${sc['model_cost_usd']:.4f} "
        f"({sc['model_calls']} calls, {sc['input_tokens']}+{sc['output_tokens']} tok)",
        f"- edge(book−model)={edge_s} → {verdict}",
        f"- fastest_beating={sc.get('fastest_beating') or 'none'} "
        f"best_pace={sc.get('best_pace') or 'none'}",
    ]
    wins = sc.get("windows") or {}
    bits = []
    for label, _h in HORIZONS:
        row = wins.get(label)
        if not isinstance(row, dict):
            continue
        cov = row.get("coverage") or ""
        if cov == "none":
            bits.append(f"{label}:none")
            continue
        wr = row.get("book_return_pct")
        we = row.get("edge_usd")
        wr_s = f"{wr:+.2f}%" if wr is not None else "n/a"
        we_s = f"{we:+.2f}" if we is not None else "n/a"
        mark = "BEAT" if row.get("beating_model") is True else (
            "behind" if row.get("beating_model") is False else cov
        )
        bits.append(f"{label}:{wr_s}/{we_s}/{mark}")
    if bits:
        lines.append("- windows " + " ".join(bits))
    return "\n".join(lines) + "\n"
