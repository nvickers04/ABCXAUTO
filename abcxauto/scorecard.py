"""Primary scorecard: book return % of starting NetLiq vs model API cost.

Hero / session is the current ET regular session (RTH). Inception stays a
window row and the promote floor — never the hero. vs SPY is a real print
pair or blank; this module does not invent a SPY series.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Short → long. Grok picks which window is enough; code only reports facts.
# Promote / inception beating stays the full-book row on compute_scorecard.
# Hero and sess use rth_session_start, not these labels.
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


def rth_session_start(now: datetime | None = None) -> tuple[datetime, str]:
    """09:30 ET bell of the current (or most recent) weekday regular session.

    After the bell on a weekday: that day's open. Before the bell, or on a
    weekend: the previous weekday's open. Returns (UTC datetime, ET date).
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    clock = _utc(now).astimezone(et)
    day = clock.date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    bell = datetime(day.year, day.month, day.day, 9, 30, tzinfo=et)
    if clock < bell:
        day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        bell = datetime(day.year, day.month, day.day, 9, 30, tzinfo=et)
    return bell.astimezone(timezone.utc), day.isoformat()


def _et_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:10] if len(text) >= 10 else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return dt.astimezone(timezone.utc).date().isoformat()


def _finite_px(raw: Any) -> float | None:
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    if px != px or px <= 0:
        return None
    return px


def _absorb_spy_prints(blob: Any, out: dict[str, float]) -> None:
    """Copy last/open/close from a quote dict or a last print. No series."""
    if isinstance(blob, dict):
        for key in ("last", "open", "close"):
            px = _finite_px(blob.get(key))
            if px is not None:
                out.setdefault(key, px)
        if "last" not in out:
            px = _finite_px(blob.get("mid"))
            if px is not None:
                out["last"] = px
        return
    px = _finite_px(blob)
    if px is not None:
        out.setdefault("last", px)


def spy_prints(spy: Any = None, *, load_last_turn: bool = True) -> dict[str, float]:
    """IBKR SPY last/open/close already on the book. Never fetches a series.

    ``spy`` may be a last print, a quote dict, or ``ibkr_live_quotes``. When
    omitted, last_turn.ibkr_live_quotes['SPY'] is used if present.
    """
    out: dict[str, float] = {}
    if spy is not None:
        if isinstance(spy, dict) and (
            "SPY" in spy or "spy" in spy or "ibkr_live_quotes" in spy
        ):
            quotes = spy.get("ibkr_live_quotes") if "ibkr_live_quotes" in spy else spy
            if isinstance(quotes, dict):
                _absorb_spy_prints(quotes.get("SPY") or quotes.get("spy"), out)
            _absorb_spy_prints(spy.get("spy_quote") if isinstance(spy, dict) else None, out)
            if not out:
                _absorb_spy_prints(spy, out)
        else:
            _absorb_spy_prints(spy, out)
        return out
    if not load_last_turn:
        return out
    try:
        from abcxauto.think_stream import LAST_TURN_PATH

        raw = LAST_TURN_PATH.read_text(encoding="utf-8")
    except Exception:
        return out
    try:
        import json

        data = json.loads(raw) if raw else {}
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    quotes = data.get("ibkr_live_quotes")
    if isinstance(quotes, dict):
        _absorb_spy_prints(quotes.get("SPY") or quotes.get("spy"), out)
    _absorb_spy_prints(data.get("spy_quote"), out)
    return out


def spy_return_pct(
    label: str,
    prints: dict[str, float] | None,
    *,
    session: bool = False,
) -> float | None:
    """Same-window SPY return from prints already on the book.

    Session: last vs today's open. 1d: last vs prior close. Other windows
    need a series we do not have — return None rather than reuse the wrong
    pair (open-to-last is not a 15m return).
    """
    blob = prints if isinstance(prints, dict) else {}
    last = _finite_px(blob.get("last"))
    if last is None:
        return None
    if session:
        open_px = _finite_px(blob.get("open"))
        if open_px is None:
            return None
        return (last / open_px - 1.0) * 100.0
    if label == "1d":
        close = _finite_px(blob.get("close"))
        if close is None:
            return None
        return (last / close - 1.0) * 100.0
    return None


def max_dd_usd(points: list[float] | None) -> float | None:
    """Peak-to-trough dollar drawdown of an NL path. None when empty."""
    xs = [p for p in (points or []) if isinstance(p, (int, float))]
    if not xs:
        return None
    peak = float(xs[0])
    dd = 0.0
    for raw in xs:
        px = float(raw)
        if px > peak:
            peak = px
        drop = peak - px
        if drop > dd:
            dd = drop
    return dd


def _pct_of_start(value: float | None, start_nl: float | None) -> float | None:
    """``value`` as percent of ``start_nl``. None when base is missing/non-positive."""
    if value is None or start_nl is None:
        return None
    try:
        base = float(start_nl)
        if base <= 0:
            return None
        return (float(value) / base) * 100.0
    except (TypeError, ValueError):
        return None


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
    spy_return_pct: float | None = None,
) -> dict[str, Any]:
    book_pnl = None
    book_return_pct = None
    if current is not None and start_nl is not None and start_nl > 0:
        book_pnl = float(current) - float(start_nl)
        book_return_pct = (book_pnl / float(start_nl)) * 100.0
    cost = float(usage.get("cost_usd") or 0.0)
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
    elif span_s is not None and span_s > 1.5 * float(horizon_s):
        # Snapshot is older than the window. Using it as start_nl would
        # label leftover ΔNL (and % of that leftover base) as this horizon.
        coverage = "stale"
        book_pnl = None
        book_return_pct = None
    if coverage in ("none", "stale"):
        edge = None
        beating = None
        pct_base = None
    else:
        edge = None if book_pnl is None else (book_pnl - cost)
        beating = None if edge is None else (edge > 0)
        pct_base = float(start_nl) if start_nl else None
    hours = (span_s / 3600.0) if span_s and span_s > 0 else None
    edge_per_hour = (edge / hours) if edge is not None and hours else None
    base = float(start_nl) if start_nl else None
    return {
        "label": label,
        "horizon_s": horizon_s,
        "span_s": span_s,
        "coverage": coverage,
        "start_nl": base,
        "start_ts": start_ts,
        "book_pnl": book_pnl,
        "book_return_pct": book_return_pct,
        "model_cost_usd": cost,
        "model_cost_pct": _pct_of_start(cost, pct_base),
        "model_calls": int(usage.get("calls") or 0),
        "edge_usd": edge,
        "edge_pct": _pct_of_start(edge, pct_base),
        "edge_per_hour": edge_per_hour,
        "beating_model": beating,
        "snaps": int(snaps or 0),
        "spy_return_pct": spy_return_pct,
    }


def compute_scorecard(
    *,
    equity: float | None = None,
    journal: Any = None,
    now: datetime | None = None,
    spy: Any = None,
) -> dict[str, Any]:
    """Book P&L vs model cost, scored as % of starting NetLiq.

    Top-level beating_model is inception (promote / floor). ``session`` is
    this RTH (current ET regular session) — not a leftover model marker.
    ``windows`` are shorter looks; each carries ``spy_return_pct`` (None
    unless a real SPY print pair exists for that window). ``fastest_beating``
    is the shortest non-thin window ahead of the model bill.
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
    start_base = float(startup) if startup else None
    model_cost_pct = _pct_of_start(cost, start_base)
    edge_pct = _pct_of_start(edge, start_base)

    clock = _utc(now)
    spy_px = spy_prints(spy)
    spy_last = spy_px.get("last")
    windows: dict[str, dict[str, Any]] = {}
    if journal is not None:
        for label, horizon_s in HORIZONS:
            spy_ret = spy_return_pct(label, spy_px)
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
                    spy_return_pct=spy_ret,
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
                spy_return_pct=spy_ret,
            )

    session: dict[str, Any] | None = None
    if journal is not None:
        model_name = ""
        try:
            from abcxauto.config import get_config

            model_name = str(getattr(get_config(), "model", "") or "")
        except Exception:
            model_name = ""
        bell_utc, session_date = rth_session_start(clock)
        start_ts = _iso(bell_utc)
        start_nl = None
        start_obs = None
        try:
            if hasattr(journal, "nav_at_or_after"):
                start_nl, start_obs = journal.nav_at_or_after(start_ts)
        except Exception:
            start_nl, start_obs = None, None
        if start_nl is None:
            try:
                if hasattr(journal, "nav_at_or_before"):
                    pre_nl, pre_ts = journal.nav_at_or_before(start_ts)
                    if pre_nl is not None and _et_date(pre_ts) == session_date:
                        start_nl, start_obs = pre_nl, pre_ts
            except Exception:
                pass
        sess_usage = {
            "calls": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        try:
            if hasattr(journal, "model_usage_since"):
                sess_usage = dict(journal.model_usage_since(start_ts) or sess_usage)
        except Exception:
            pass
        fills = {"n": 0, "wins": 0, "sum": 0.0}
        try:
            if hasattr(journal, "closed_fill_stats_since"):
                fills = dict(journal.closed_fill_stats_since(start_ts) or fills)
        except Exception:
            pass
        commissions = None
        try:
            if hasattr(journal, "commissions_since"):
                commissions = float(journal.commissions_since(start_ts) or 0.0)
        except Exception:
            commissions = None
        path_pts: list[float] = []
        try:
            if hasattr(journal, "nav_path_since"):
                for _ts, nl in journal.nav_path_since(start_ts) or []:
                    try:
                        path_pts.append(float(nl))
                    except (TypeError, ValueError):
                        continue
        except Exception:
            path_pts = []
        if start_nl is not None:
            try:
                path_pts = [float(start_nl)] + path_pts
            except (TypeError, ValueError):
                pass
        if current is not None:
            try:
                path_pts.append(float(current))
            except (TypeError, ValueError):
                pass
        sess_pnl = None
        sess_ret = None
        if current is not None and start_nl is not None:
            try:
                sess_pnl = float(current) - float(start_nl)
                if float(start_nl) > 0:
                    sess_ret = (sess_pnl / float(start_nl)) * 100.0
            except (TypeError, ValueError):
                sess_pnl = None
        sess_cost = float(sess_usage.get("cost_usd") or 0.0)
        sess_edge = None if sess_pnl is None else (sess_pnl - sess_cost)
        sess_base = float(start_nl) if start_nl is not None else None
        end_nl = None
        try:
            end_nl = float(current) if current is not None else None
        except (TypeError, ValueError):
            end_nl = None
        dd = max_dd_usd(path_pts) if sess_base is not None else None
        session = {
            "kind": "rth",
            "model": model_name,
            "session_date": session_date,
            "started_at": start_ts,
            "startup_nl": sess_base,
            "end_nl": end_nl,
            "book_pnl": sess_pnl,
            "book_return_pct": sess_ret,
            "model_cost_usd": sess_cost,
            "model_cost_pct": _pct_of_start(sess_cost, sess_base),
            "model_calls": int(sess_usage.get("calls") or 0),
            "edge_usd": sess_edge,
            "edge_pct": _pct_of_start(sess_edge, sess_base),
            "fills": int(fills.get("n") or 0),
            "wins": int(fills.get("wins") or 0),
            "commissions_usd": commissions,
            "max_dd_usd": dd,
            "spy_return_pct": spy_return_pct("rth", spy_px, session=True),
            "start_obs_ts": start_obs,
        }

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
        "startup_cash": start_base,
        "net_liquidation": current,
        "book_pnl": book_pnl,
        "book_return_pct": book_return_pct,
        "model_calls": int(usage.get("calls") or 0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "model_cost_usd": cost,
        "model_cost_pct": model_cost_pct,
        "edge_usd": edge,
        "edge_pct": edge_pct,
        "beating_model": beating,
        "goal": "book return % of starting NetLiq > cost of the model",
        "windows": windows,
        "fastest_beating": fastest_beating,
        "best_pace": best_pace,
        "spy_last": spy_last,
        "since_start": {
            "book_pnl": book_pnl,
            "model_cost_usd": cost,
            "model_cost_pct": model_cost_pct,
            "edge_usd": edge,
            "edge_pct": edge_pct,
            "beating_model": beating,
            "startup_cash": start_base,
        },
        "session": session,
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
    edge_pct = sc.get("edge_pct")
    edge_pct_s = f"{edge_pct:+.4f}%" if edge_pct is not None else "n/a"
    cost_pct = sc.get("model_cost_pct")
    cost_pct_s = f"{cost_pct:.4f}%" if cost_pct is not None else "n/a"
    start = sc.get("startup_cash")
    start_s = f"{start:.2f}" if start is not None else "n/a"
    # Paper: book_pnl is paper; model_cost $ is REAL xAI cash. Live: both real.
    paper = True
    try:
        from abcxauto.config import get_config

        cfg = get_config()
        paper = bool(getattr(cfg, "is_paper", True)) or (
            str(getattr(cfg, "trading_mode", "paper") or "paper").lower() != "live"
        )
    except Exception:
        paper = True
    if paper:
        book_tag = " paper"
        cost_tag = " real xAI"
    else:
        book_tag = ""
        cost_tag = ""
    lines = ["SCORECARD:"]
    # Session = this RTH (ET regular session). Promote / BEATING-vs-LOSING
    # stay on inception below. A leftover model marker is not sess.
    sess = sc.get("session")
    if isinstance(sess, dict) and sess:
        sret = sess.get("book_return_pct")
        if sret is None:
            s_nl = sess.get("startup_nl")
            spnl = sess.get("book_pnl")
            if spnl is not None and s_nl is not None and float(s_nl) > 0:
                sret = (float(spnl) / float(s_nl)) * 100.0
        sret_s = f"{sret:+.2f}%" if sret is not None else "n/a"
        scost = sess.get("model_cost_usd")
        scost_s = f"${float(scost):.4f}" if scost is not None else "n/a"
        scost_pct = sess.get("model_cost_pct")
        scost_pct_s = f"{scost_pct:.4f}%" if scost_pct is not None else "n/a"
        sedge_pct = sess.get("edge_pct")
        sedge_pct_s = f"{sedge_pct:+.4f}%" if sedge_pct is not None else "n/a"
        sedge = sess.get("edge_usd")
        sedge_s = f"{sedge:+.2f}" if sedge is not None else "n/a"
        fills = sess.get("fills")
        swins = sess.get("wins")
        if fills not in (None, 0):
            fill_s = f"{swins}/{fills}"
        else:
            fill_s = str(fills if fills is not None else 0)
        model = sess.get("model") or ""
        model_bit = f" model={model}" if model else ""
        rth_date = sess.get("session_date") or ""
        date_bit = f"{rth_date} RTH " if rth_date else "RTH "
        start_nl = sess.get("startup_nl")
        end_nl = sess.get("end_nl")
        start_s = f"{float(start_nl):.2f}" if isinstance(start_nl, (int, float)) else "n/a"
        end_s = f"{float(end_nl):.2f}" if isinstance(end_nl, (int, float)) else "n/a"
        comm = sess.get("commissions_usd")
        comm_s = f"${float(comm):.2f}" if isinstance(comm, (int, float)) else "n/a"
        dd = sess.get("max_dd_usd")
        dd_s = f"${float(dd):.2f}" if isinstance(dd, (int, float)) else "n/a"
        spy_r = sess.get("spy_return_pct")
        spy_s = f"{float(spy_r):+.2f}%" if isinstance(spy_r, (int, float)) else "—"
        lines.append(
            f"- session {date_bit}book={sret_s}{book_tag} "
            f"start={start_s} end={end_s} "
            f"model_cost={scost_pct_s} ({scost_s}{cost_tag}) "
            f"({int(sess.get('model_calls') or 0)} calls) "
            f"edge={sedge_pct_s} ({sedge_s}$) "
            f"comm={comm_s} maxDD={dd_s} vsSPY={spy_s} "
            f"fills={fill_s}{model_bit}"
        )
    lines.extend(
        [
            f"- first_NL={start_s} NL={sc.get('net_liquidation')}",
            f"- book_return={ret_s} of starting NetLiq ({pnl_s}${book_tag})",
            f"- model_cost={cost_pct_s} of starting NetLiq "
            f"(${sc['model_cost_usd']:.4f}{cost_tag or ' cash'}, "
            f"{sc['model_calls']} calls, "
            f"{sc['input_tokens']}+{sc['output_tokens']} tok)",
            f"- edge={edge_pct_s} ({edge_s}$) → {verdict}",
            f"- fastest_beating={sc.get('fastest_beating') or 'none'} "
            f"best_pace={sc.get('best_pace') or 'none'}",
        ]
    )
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
        we_pct = row.get("edge_pct")
        wr_s = f"{wr:+.2f}%" if wr is not None else "n/a"
        we_s = f"{we_pct:+.4f}%" if we_pct is not None else "n/a"
        mark = "BEAT" if row.get("beating_model") is True else (
            "behind" if row.get("beating_model") is False else cov
        )
        spy_r = row.get("spy_return_pct")
        spy_s = f"{float(spy_r):+.2f}%" if isinstance(spy_r, (int, float)) else "—"
        bits.append(f"{label}:{wr_s}/{we_s}/{mark}/spy={spy_s}")
    if bits:
        lines.append("- windows " + " ".join(bits))
    return "\n".join(lines) + "\n"
