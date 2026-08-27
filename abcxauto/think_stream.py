"""Live Grok thinking stream — tool-loop tokens as they arrive.

Headless prints to stdout (ASCII). ProEngine binds so the UI can show the same buffer.
A short tail file lets Cursor review the stream without the window.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[str, str], None]

_lock = threading.Lock()
_listeners: list[Listener] = []
_engine: Any = None
_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
THINK_TAIL_PATH = _STATE_DIR / "think_tail.txt"
THINK_PREV_PATH = _STATE_DIR / "think_prev.txt"
LAST_TURN_PATH = _STATE_DIR / "last_turn.json"
DESK_BRIEF_PATH = _STATE_DIR / "desk_brief.json"
RUN_PATH = _STATE_DIR / "run.json"
_TAIL_MIN_INTERVAL = 2.0
_last_tail_write = 0.0
_run: dict[str, Any] = {}

logger = logging.getLogger(__name__)

_ASCII_PUNCT = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2265": ">=",
        "\u2264": "<=",
        "\u00b1": "+/-",
    }
)


def ascii_text(text: str) -> str:
    """Windows consoles are often cp1252 — never emit non-ASCII to stdout.

    Common punctuation is mapped to ASCII so a curly apostrophe does not
    paint as '?' (that looked like Grok answering a question mark).
    """
    t = (text or "").translate(_ASCII_PUNCT)
    return t.encode("ascii", "replace").decode("ascii")


def subscribe(fn: Listener) -> None:
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def unsubscribe(fn: Listener) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def bind_engine(engine: Any | None) -> None:
    """One ProEngine at a time; think_live buffer updates without flooding the UI queue."""
    global _engine
    _engine = engine


def emit(kind: str, text: str) -> None:
    if not text:
        return
    with _lock:
        fns = list(_listeners)
        eng = _engine
    if eng is not None:
        try:
            _append_engine(eng, kind, text)
        except Exception:
            logger.debug("think_stream engine append failed", exc_info=True)
    for fn in fns:
        try:
            fn(kind, text)
        except Exception:
            logger.debug("think_stream listener failed", exc_info=True)


def _append_engine(eng: Any, kind: str, text: str) -> None:
    s = getattr(eng, "state", None)
    if s is None:
        return
    if kind == "stage":
        label = ascii_text(text).strip().upper()
        if label in ("", "GROK", "JUDGE", "ACT"):
            piece = "\n--- GROK ---\n"
        else:
            piece = f"\n--- GROK {label} ---\n"
    elif kind == "stage_end":
        piece = "\n"
    else:
        piece = ascii_text(text)
    cur = getattr(s, "think_live", "") or ""
    s.think_live = (cur + piece)[-24000:]
    _write_think_tail(s.think_live)


def _write_think_tail(buf: str, *, force: bool = False) -> None:
    global _last_tail_write
    now = time.monotonic()
    if not force and now - _last_tail_write < _TAIL_MIN_INTERVAL:
        return
    _last_tail_write = now
    try:
        THINK_TAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        THINK_TAIL_PATH.write_text(buf[-8000:], encoding="utf-8")
    except OSError:
        logger.debug("think_tail write failed", exc_info=True)


def flush_think_tail() -> None:
    """Write the live buffer now so a kill does not lose the last chunks."""
    eng = _engine
    buf = ""
    if eng is not None:
        try:
            buf = str(getattr(getattr(eng, "state", None), "think_live", "") or "")
        except Exception:
            buf = ""
    if not buf and THINK_TAIL_PATH.is_file():
        return
    _write_think_tail(buf or (THINK_TAIL_PATH.read_text(encoding="utf-8") if THINK_TAIL_PATH.is_file() else ""), force=True)


def _archive_think_tail() -> None:
    try:
        if not THINK_TAIL_PATH.is_file():
            return
        text = THINK_TAIL_PATH.read_text(encoding="utf-8")
        if not text.strip():
            return
        THINK_PREV_PATH.parent.mkdir(parents=True, exist_ok=True)
        THINK_PREV_PATH.write_text(text, encoding="utf-8")
    except OSError:
        logger.debug("think_prev archive failed", exc_info=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, 0, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _compact_scan_hits(raw: Any) -> dict[str, Any]:
    """Keep the last screen's triage rows. Symbols-only scan_fetched is not enough."""
    if not isinstance(raw, dict) or not raw:
        return {}
    rows: list[dict[str, Any]] = []
    for r in list(raw.get("rows") or [])[:16]:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        item: dict[str, Any] = {"symbol": sym}
        for key in (
            "rank",
            "last",
            "open",
            "close",
            "change_pct",
            "open_gap_pct",
            "distance",
            "bid",
            "ask",
            "spread",
            "spread_pct",
            "quote_source",
        ):
            if r.get(key) not in (None, ""):
                item[key] = r[key]
        ibkr = r.get("ibkr") if isinstance(r.get("ibkr"), dict) else None
        if ibkr:
            live: dict[str, Any] = {}
            for key in ("last", "bid", "ask", "asof_iso", "freshness", "source"):
                if ibkr.get(key) not in (None, ""):
                    live[key] = ibkr[key]
            if live:
                item["ibkr"] = live
        mda = r.get("mda") if isinstance(r.get("mda"), dict) else None
        if mda:
            delayed: dict[str, Any] = {}
            for key in (
                "mda_last",
                "sma20",
                "dist20",
                "ret5",
                "freshness",
                "source",
                "asof_iso",
            ):
                if mda.get(key) not in (None, ""):
                    delayed[key] = mda[key]
            news = mda.get("news")
            if isinstance(news, list) and news:
                delayed["news_n"] = len(news)
            if delayed:
                item["mda"] = delayed
        rows.append(item)
    if not rows and not raw.get("scan_code") and not raw.get("arena"):
        return {}
    return {
        "source": raw.get("source"),
        "arena": raw.get("arena"),
        "scan_code": raw.get("scan_code"),
        "ranked": bool(raw.get("ranked")),
        "rank_meaning": raw.get("rank_meaning") or "",
        "quoted": raw.get("quoted"),
        "rows": rows,
    }


def _compact_live_quotes(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, val in list(raw.items())[:16]:
        name = str(key or "").upper().strip()
        raw_px = val.get("last") if isinstance(val, dict) else val
        if raw_px is None and isinstance(val, dict):
            raw_px = val.get("mid")
        try:
            px = float(raw_px)
        except (TypeError, ValueError):
            continue
        if name and px > 0:
            out[name] = px
    return out


def _open_gap_mag(row: dict[str, Any] | None) -> float:
    if not isinstance(row, dict):
        return -1.0
    gap = row.get("open_gap_pct")
    try:
        return abs(float(gap)) if gap is not None else -1.0
    except (TypeError, ValueError):
        return -1.0


def _signed_open_gap(row: dict[str, Any] | None) -> float:
    if not isinstance(row, dict) or row.get("open_gap_pct") is None:
        return 0.0
    try:
        return float(row.get("open_gap_pct"))
    except (TypeError, ValueError):
        return 0.0


def _card_wants_down_gaps() -> bool:
    try:
        from abcxauto.lab_playbook import live_card_send_facts

        return str(live_card_send_facts().get("direction") or "LONG").upper() != "SHORT"
    except Exception:
        return True


def sort_scan_rows(rows: list[Any] | None) -> list[dict[str, Any]]:
    """LONG flush cards need the down-gap first, not |gap| (a +4% name is not it)."""
    clean = [r for r in (rows or []) if isinstance(r, dict)]
    if _card_wants_down_gaps():
        return sorted(clean, key=_signed_open_gap)
    return sorted(clean, key=_signed_open_gap, reverse=True)


def merge_scan_hits(prior: Any, incoming: Any) -> dict[str, Any]:
    """Union this look's screens. Last-scan-wins used to hide the gap row."""
    new = _compact_scan_hits(incoming)
    old = _compact_scan_hits(prior)
    if not old:
        return {**new, "rows": sort_scan_rows(new.get("rows"))} if new else new
    if not new:
        return {**old, "rows": sort_scan_rows(old.get("rows"))}
    by: dict[str, dict[str, Any]] = {}
    down = _card_wants_down_gaps()
    for r in list(old.get("rows") or []) + list(new.get("rows") or []):
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        prev = by.get(sym)
        if prev is None:
            by[sym] = dict(r)
            continue
        keep = dict(prev)
        better = (
            _signed_open_gap(r) <= _signed_open_gap(prev)
            if down
            else _signed_open_gap(r) >= _signed_open_gap(prev)
        )
        if better:
            keep.update({k: v for k, v in r.items() if v not in (None, "")})
        else:
            for k, v in r.items():
                if keep.get(k) in (None, "") and v not in (None, ""):
                    keep[k] = v
        by[sym] = keep
    rows = sort_scan_rows(list(by.values()))
    if down:
        new_best = min((_signed_open_gap(r) for r in (new.get("rows") or [])), default=0.0)
        old_best = min((_signed_open_gap(r) for r in (old.get("rows") or [])), default=0.0)
        meta = new if new_best <= old_best else old
    else:
        new_best = max((_signed_open_gap(r) for r in (new.get("rows") or [])), default=0.0)
        old_best = max((_signed_open_gap(r) for r in (old.get("rows") or [])), default=0.0)
        meta = new if new_best >= old_best else old
    return {
        "source": meta.get("source") or new.get("source"),
        "arena": meta.get("arena") or new.get("arena"),
        "scan_code": meta.get("scan_code") or new.get("scan_code"),
        "ranked": bool(meta.get("ranked") if meta.get("ranked") is not None else new.get("ranked")),
        "rank_meaning": meta.get("rank_meaning") or new.get("rank_meaning") or "",
        "quoted": new.get("quoted") if new.get("quoted") not in (None, "") else old.get("quoted"),
        "rows": rows[:16],
    }


def current_run() -> dict[str, Any]:
    if _run:
        return dict(_run)
    return _read_json(RUN_PATH)


def mark_review_stale(*, archive_tail: bool = False) -> None:
    """Mark last_turn dead. Keep the think tail so a mid-turn kill is readable."""
    flush_think_tail()
    prev = _read_json(LAST_TURN_PATH)
    run = current_run()
    payload = {
        "stale": True,
        "previous_run_id": prev.get("run_id") or run.get("run_id") or "",
        "previous_strat": prev.get("strat") or "",
        "open_lots": list(prev.get("open_lots") or []),
        "net_liquidation": prev.get("net_liquidation"),
        "flat": prev.get("flat"),
        "session": prev.get("session") or {},
        "ibkr_connected": prev.get("ibkr_connected"),
        "mix": prev.get("mix") if isinstance(prev.get("mix"), dict) else {},
        "rationale": (prev.get("rationale") or "")[:1200],
        "tool_trace": list(prev.get("tool_trace") or []),
        "sends": prev.get("sends") or 0,
        "send_calls": prev.get("send_calls") or 0,
        "scan_fetched": list(prev.get("scan_fetched") or []),
        "scan_hits": _compact_scan_hits(prev.get("scan_hits")),
        "candle_source": prev.get("candle_source") or "none",
    }
    try:
        LAST_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_TURN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("last_turn stale mark failed", exc_info=True)
    if archive_tail:
        _archive_think_tail()
        try:
            if THINK_TAIL_PATH.is_file():
                THINK_TAIL_PATH.write_text("", encoding="utf-8")
        except OSError:
            logger.debug("think_tail clear failed", exc_info=True)


def _last_turn_is_this_hunt(prev: dict[str, Any] | None) -> bool:
    """A completed look from this hunt. Overnight and mid-turn kills are not."""
    if not isinstance(prev, dict) or not prev:
        return False
    if prev.get("stale"):
        return False
    if str(prev.get("strat") or "") == "in_progress":
        return False
    return last_look_is_fresh(prev)


def begin_run() -> dict[str, Any]:
    """Stamp a new process identity. Call after killing leftovers.

    A fresh completed look stays on disk so a clerk reload does not wipe
    the tape the next wake needs. Overnight and killed mid-turn still stale.
    """
    global _run
    prev = _read_json(LAST_TURN_PATH)
    if _last_turn_is_this_hunt(prev):
        _archive_think_tail()
        try:
            if THINK_TAIL_PATH.is_file():
                THINK_TAIL_PATH.write_text("", encoding="utf-8")
        except OSError:
            logger.debug("think_tail clear failed", exc_info=True)
    else:
        mark_review_stale(archive_tail=True)
    try:
        from abcxauto.park_clock import ensure_next_look

        # Overnight / after-close only. RTH and premarket write no sit clock.
        ensure_next_look(previous_set_at="")
    except Exception:
        logger.debug("grok_wake seed on begin_run failed", exc_info=True)
    _run = {
        "run_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUN_PATH.write_text(json.dumps(_run, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("run.json write failed", exc_info=True)
    return dict(_run)


_SESSION_KEEP = (
    "date",
    "today",
    "open",
    "high",
    "low",
    "last",
    "n",
    "vs_open",
    "vs_low",
    "above_open",
    "above_low",
    "bid",
    "ask",
    "spread",
    "spread_pct",
    "prior_close",
    "gap_pts",
    "gap_pct",
    "open_gap_pct",
    "retrace_30",
    "retrace_50",
    "size",
    "ticket",
)


def _refresh_session_today(rng: dict[str, Any]) -> dict[str, Any]:
    out = {key: rng[key] for key in _SESSION_KEEP if key in rng}
    day = str(out.get("date") or "")
    if day:
        try:
            from abcxauto.opportunity_scan import _et_calendar_day

            out["today"] = day == _et_calendar_day()
        except Exception:
            pass
    return out


def _compact_session_range(raw: Any) -> dict[str, Any]:
    """Keep today's opening-low tape only. A prior-day range is not a stop."""
    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, Any] = {}
    for sym, rng in raw.items():
        name = str(sym or "").upper().strip()
        if not name or not isinstance(rng, dict):
            continue
        row = _refresh_session_today(rng)
        if row.get("today") is False:
            continue
        if row.get("low") is None and row.get("open") is None:
            continue
        out[name] = row
        if len(out) >= 8:
            break
    return out


def _quotes_from_scan_hits(hits: Any) -> dict[str, float]:
    """IBKR lasts already printed on the last screen. Not MDA tape."""
    blob = hits if isinstance(hits, dict) else {}
    out: dict[str, float] = {}
    for row in blob.get("rows") or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else {}
        raw = ibkr.get("last") if ibkr.get("last") is not None else row.get("last")
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if sym and px > 0:
            out[sym] = px
    return out


def _seed_live_quotes_from_last(
    snap: dict[str, Any],
    data: dict[str, Any],
    hits: Any,
) -> None:
    qmap = snap.get("ibkr_live_quotes")
    if not isinstance(qmap, dict):
        qmap = {}
        snap["ibkr_live_quotes"] = qmap
    persisted = data.get("ibkr_live_quotes")
    if isinstance(persisted, dict):
        for key, raw in persisted.items():
            try:
                px = float(raw)
            except (TypeError, ValueError):
                continue
            name = str(key or "").upper().strip()
            if name and px > 0:
                qmap.setdefault(name, px)
    for name, px in _quotes_from_scan_hits(hits).items():
        qmap.setdefault(name, px)


def seed_snap_from_last_turn(snap: dict[str, Any] | None) -> None:
    """Carry last look's tape onto a fresh IBKR snap.

    ``snap()`` always stamps ``candle_source`` (``none`` until bars run). That
    must not skip scan hits or today's session range — a later send needs them.
    Skip overwriting a live this-look bar source. Skip ``none`` as a source.
    """
    if not isinstance(snap, dict):
        return
    data = _read_json(LAST_TURN_PATH)
    if not data:
        return
    existing = str(snap.get("candle_source") or "").strip()
    if existing in ("", "none"):
        src = str(data.get("candle_source") or "").strip()
        if src and src not in ("none",):
            snap["candle_source"] = src
    fresh = not data.get("stale") and last_look_is_fresh(data)
    hits = _compact_scan_hits(data.get("scan_hits"))
    if hits and not snap.get("scan_hits") and fresh:
        snap["scan_hits"] = hits
    if not snap.get("session_range") and fresh:
        rng = _compact_session_range(data.get("session_range"))
        if rng:
            snap["session_range"] = rng
    if fresh:
        _seed_live_quotes_from_last(snap, data, hits or snap.get("scan_hits"))
    scan_at = str(data.get("scan_at") or "").strip()
    if scan_at:
        snap["scan_at"] = scan_at
    age = scan_tape_age_s(data)
    screens = [str(x) for x in (data.get("scan_screens") or []) if str(x).strip()]
    if (
        fresh
        and screens
        and age is not None
        and age <= (
            SCAN_REUSE_MANAGE_S if data.get("flat") is False else SCAN_REUSE_S
        )
        and not snap.get("scan_screens")
    ):
        snap["scan_screens"] = screens
        try:
            calls = int(data.get("scan_calls") or 0)
        except (TypeError, ValueError):
            calls = 0
        snap["scan_calls"] = max(calls, len(screens))
        if hits and not snap.get("scan_hits"):
            snap["scan_hits"] = hits


def last_turn_is_live(payload: dict[str, Any] | None = None) -> bool:
    """True only if last_turn belongs to this live process."""
    data = payload if isinstance(payload, dict) else _read_json(LAST_TURN_PATH)
    if not data or data.get("stale"):
        return False
    run = current_run()
    if not run or data.get("run_id") != run.get("run_id"):
        return False
    try:
        pid = int(run.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and not _pid_alive(pid):
        return False
    return True


def _desk_brief_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_DESK_BRIEF_PATH") or "").strip()
    return Path(raw) if raw else DESK_BRIEF_PATH


def load_desk_brief() -> dict[str, Any]:
    """Last completed look. in_progress last_turn is not memory."""
    p = _desk_brief_path()
    if not p.is_file():
        data = _read_json(LAST_TURN_PATH)
        if data.get("stale") or str(data.get("strat") or "") == "in_progress":
            return {}
        return data
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


# Same-hunt window. Overnight / killed-desk briefs must not resume as next=.
LAST_LOOK_FRESH_S = 45 * 60
# A 30s–3m hunt re-wake must not re-pull every card screen. A 5m hunt rescans.
# A protected manage look can reuse the last tape longer — fill/unprotected still interrupt.
SCAN_REUSE_S = 180.0
SCAN_REUSE_MANAGE_S = 15 * 60.0


def _scan_hits_asof_age_s(
    row: dict[str, Any] | None,
    now: datetime | None = None,
) -> float | None:
    """Newest IBKR quote asof on persisted scan rows. None if the tape has no clock."""
    hits = (row or {}).get("scan_hits") if isinstance(row, dict) else {}
    ages: list[float] = []
    for item in (hits.get("rows") or []) if isinstance(hits, dict) else []:
        if not isinstance(item, dict):
            continue
        ibkr = item.get("ibkr") if isinstance(item.get("ibkr"), dict) else {}
        raw = str(ibkr.get("asof_iso") or item.get("asof_iso") or "").strip()
        if not raw:
            continue
        age = last_look_age_s({"ts": raw}, now=now)
        if age is not None:
            ages.append(age)
    return min(ages) if ages else None


def scan_tape_age_s(
    row: dict[str, Any] | None,
    now: datetime | None = None,
) -> float | None:
    """Age of the last IBKR scan fetch. last_turn ts slides every look."""
    stamped = str((row or {}).get("scan_at") or "").strip()
    if stamped:
        return last_look_age_s({**(row or {}), "ts": stamped}, now=now)
    asof_age = _scan_hits_asof_age_s(row, now=now)
    if asof_age is not None:
        return asof_age
    return last_look_age_s(row, now=now)


def last_look_age_s(
    row: dict[str, Any] | None,
    now: datetime | None = None,
) -> float | None:
    ts = str((row or {}).get("ts") or "").strip()
    if not ts:
        return None
    try:
        stamped = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        return max(0.0, (clock - stamped.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def last_look_is_fresh(
    row: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not isinstance(row, dict) or not row:
        return False
    if "fresh" in row:
        return bool(row["fresh"])
    age = last_look_age_s(row, now)
    if age is None:
        return True
    return age <= LAST_LOOK_FRESH_S


def last_look_for_hunt(brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Last look only when it is still this hunt. Stale tape is not next=."""
    facts = last_look_facts() if brief is None else last_look_facts(brief)
    if not facts or facts.get("fresh") is False:
        return {}
    return facts


def _wake_job_say(row: dict[str, Any] | None) -> str:
    """Last real say on the brief. Clerk markers are not a job."""
    blob = row if isinstance(row, dict) else {}
    text = str(blob.get("last_say") or blob.get("rationale") or "").strip()
    text = " ".join(text.split())
    if not text or text in {"?", "—", "-", ".", "..."}:
        return ""
    low = text.lower()
    if low == "grok_turn" or low.startswith("skipped_grok"):
        return ""
    if low.startswith("wake grok") or "book snap done" in low:
        return ""
    if low.startswith("next look:") or "loser screens" in low:
        return ""
    return text[:240]


def last_look_wake_bit(brief: dict[str, Any] | None = None) -> str:
    """Last real unfinished say if one exists. Not a look diary."""
    row = brief if isinstance(brief, dict) else load_desk_brief()
    if not isinstance(row, dict) or not row:
        return ""
    return _wake_job_say(row)


def last_look_facts(brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Last completed look as structured facts for book(). Empty if none."""
    loaded = brief is None
    row = brief if isinstance(brief, dict) else load_desk_brief()
    if not isinstance(row, dict) or not row:
        return {}
    try:
        n = int(row.get("send_calls") if row.get("send_calls") is not None else row.get("sends") or 0)
    except (TypeError, ValueError):
        n = 0
    tools: list[str] = []
    for raw in list(row.get("tool_trace") or []):
        name = str(raw or "").strip()
        if name and name not in tools:
            tools.append(name)
        if len(tools) >= 8:
            break
    hits = _compact_scan_hits(row.get("scan_hits"))
    quotes = _compact_live_quotes(row.get("ibkr_live_quotes"))
    if not quotes:
        quotes = _quotes_from_scan_hits(hits)
    out: dict[str, Any] = {
        "send_calls": n,
        "tools": tools,
        "scan_hits": hits,
        "session_range": _compact_session_range(row.get("session_range")),
        "ibkr_live_quotes": quotes,
        "fresh": last_look_is_fresh(row),
    }
    if loaded:
        last = _read_json(LAST_TURN_PATH)
        if last.get("stale"):
            out["fresh"] = False
    if out["fresh"] is False:
        return {
            "fresh": False,
            "send_calls": n,
            "tools": [],
            "scan_hits": {},
            "session_range": {},
            "ibkr_live_quotes": {},
        }
    why = str(row.get("rationale") or "").strip()
    if why:
        out["rationale"] = why[:240]
    return out if (tools or hits or n or why) else {}


def last_turn_look_failed(out: dict[str, Any] | None) -> bool:
    """True when this persist payload is a junk/empty/failed look, not a completed turn.

    ``write_desk_brief`` skips ``strat=="in_progress"`` so the last completed
    look stays on the brief. ``write_last_turn`` skips ``look_failed()`` the
    same way so a ``?`` / empty / stream-error look does not blank
    last_turn.json. Overnight / park still write: ``BrainTurn.look_failed``
    is false when parked, and ``_host_think`` stamps ``_failed`` only when
    the look failed and was not parked.
    """
    if not isinstance(out, dict):
        return False
    if str(out.get("strat") or "") == "in_progress":
        return False
    if out.get("_parked") or out.get("parked"):
        return False
    return bool(out.get("_failed") or out.get("failed"))


def write_desk_brief(payload: dict[str, Any]) -> None:
    if str(payload.get("strat") or "") == "in_progress":
        return
    sends = payload.get("sends") or 0
    send_calls = payload.get("send_calls")
    if send_calls is None:
        send_calls = len(
            [t for t in (payload.get("tool_trace") or []) if str(t) == "send"]
        )
    row = {
        "strat": payload.get("strat"),
        "sends": sends,
        "send_calls": send_calls,
        "open_lots": list(payload.get("open_lots") or [])[:16],
        "net_liquidation": payload.get("net_liquidation"),
        "mix": payload.get("mix") if isinstance(payload.get("mix"), dict) else {},
        "rationale": (payload.get("rationale") or "")[:800],
        "tool_trace": list(payload.get("tool_trace") or [])[:16],
        "scan_hits": _compact_scan_hits(payload.get("scan_hits")),
        "session_range": _compact_session_range(payload.get("session_range")),
        "ibkr_live_quotes": _compact_live_quotes(payload.get("ibkr_live_quotes")),
        "ts": payload.get("ts") or datetime.now(timezone.utc).isoformat(),
    }
    p = _desk_brief_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(row, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("desk_brief write failed", exc_info=True)


def _mix_of(out: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    for src in (out.get("mix"), world.get("mix")):
        if isinstance(src, dict) and src:
            return src
    try:
        from abcxauto.world_state import structure_mix

        return structure_mix(out.get("positions") or world.get("positions"))
    except Exception:
        return {}


def write_last_turn_after_send(
    *,
    strat: str,
    sends: int,
    positions: list[dict[str, Any]] | None,
    orders: list[dict[str, Any]] | None = None,
    rationale: str = "",
    tool_trace: list[str] | None = None,
    net_liquidation: float | None = None,
    reality_pulse: dict[str, Any] | None = None,
    ibkr_live_last: Any = None,
    ibkr_live_quotes: dict[str, Any] | None = None,
    scan_hits: dict[str, Any] | None = None,
    session_range: dict[str, Any] | None = None,
) -> None:
    """Stamp last_turn from the live book right after a successful send.

    Do not wait for cycle persist — bounce must not see the pre-send snap.
    """
    from abcxauto.world_state import book_is_flat, lot_labels

    pos = list(positions or [])
    lots = lot_labels(pos)
    write_last_turn({
        "strat": strat,
        "sends": int(sends),
        "positions": pos,
        "open_lots": lots,
        "rationale": rationale,
        "tool_trace": list(tool_trace or []),
        "scan_hits": _compact_scan_hits(scan_hits),
        "session_range": _compact_session_range(session_range),
        "reality_pulse": reality_pulse or {},
        "ibkr_live_last": ibkr_live_last,
        "ibkr_live_quotes": dict(ibkr_live_quotes or {}),
        "world_state": {
            "flat": book_is_flat(pos, orders),
            "open_lots": lots,
            "positions": pos,
            "net_liquidation": net_liquidation,
        },
    })


def write_last_turn(out: dict[str, Any]) -> None:
    """Clerk snapshot of the last Grok turn for the Cursor review loop.

    Operator paint has no sit-loop counter. Journal/logs keep the clerk increment.
    A junk / empty / failed look is not a completed turn — keep the last
    real say/tools until a real look finishes. Overnight / park still write.
    """
    if last_turn_look_failed(out):
        return
    try:
        LAST_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
        pulse = out.get("reality_pulse") or {}
        world = out.get("world_state") or {}
        run = current_run()
        gates = world.get("gates") if isinstance(world.get("gates"), dict) else {}
        fresh = pulse.get("data_freshness") if isinstance(pulse.get("data_freshness"), dict) else {}
        ibkr = pulse.get("ibkr_connected")
        if ibkr is None:
            ibkr = fresh.get("ibkr_connected")
        from abcxauto.world_state import lot_labels

        open_lots = list(out.get("open_lots") or world.get("open_lots") or [])
        if not open_lots:
            open_lots = lot_labels(out.get("positions") or world.get("positions"))
        unreliable = bool(
            out.get("book_unreliable") or gates.get("book_unreliable")
        )
        ibkr_down = ibkr is False or "ibkr_down" in str(out.get("validation") or "")
        prior = _read_json(LAST_TURN_PATH)
        if (unreliable or ibkr_down) and not open_lots:
            open_lots = list(prior.get("open_lots") or [])
        nl = world.get("net_liquidation") or out.get("equity")
        try:
            nl_f = float(nl) if nl is not None else 0.0
        except (TypeError, ValueError):
            nl_f = 0.0
        if (unreliable or ibkr_down) and nl_f <= 0 and prior.get("net_liquidation"):
            nl = prior.get("net_liquidation")
            world = dict(world)
            world["net_liquidation"] = nl
        skip = str(out.get("validation") or out.get("skip_reason") or "")
        if skip.startswith("skipped_grok:"):
            skip = skip.split(":", 1)[-1].strip()
        elif out.get("strat") in ("skipped", "blocked"):
            skip = skip or str(out.get("strat") or "")
        else:
            skip = ""
        payload = {
            "strat": out.get("strat"),
            "rationale": (out.get("rationale") or "")[:1200],
            "validation": (out.get("validation") or "")[:400],
            "tool_trace": list(out.get("tool_trace") or []),
            "stage_error": out.get("stage_error") or "",
            "session": pulse.get("session") or world.get("session") or {},
            "scan_fetched": list(
                out.get("scan_fetched") or world.get("scan_fetched") or []
            ),
            "scan_hits": _compact_scan_hits(
                out.get("scan_hits") or world.get("scan_hits")
            ),
            "ibkr_connected": ibkr,
            "open_lots": open_lots,
            "book_unreliable": bool(
                out.get("book_unreliable") or gates.get("book_unreliable")
            ),
            "skip_reason": skip[:120],
            "flat": world.get("flat"),
            "net_liquidation": world.get("net_liquidation") or out.get("equity") or nl,
            "mix": _mix_of(out, world),
            "sends": (
                int(out["sends"])
                if isinstance(out.get("sends"), int)
                else len([t for t in (out.get("tool_trace") or []) if str(t) == "send"])
            ),
            # sends counts every mutating tool, self_tune included. Only
            # send_calls answers "did a ticket reach the broker path".
            "send_calls": len(
                [t for t in (out.get("tool_trace") or []) if str(t) == "send"]
            ),
            "run_id": run.get("run_id") or "",
            "pid": run.get("pid"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "ibkr_live_last": world.get("ibkr_live_last") or out.get("ibkr_live_last"),
            "ibkr_live_quotes": dict(
                world.get("ibkr_live_quotes") or out.get("ibkr_live_quotes") or {}
            ),
            "candle_source": (
                world.get("candle_source") or out.get("candle_source") or "none"
            ),
            "session_range": _compact_session_range(
                out.get("session_range")
                or world.get("session_range")
            ),
            "scan_screens": [
                str(x) for x in (out.get("scan_screens") or []) if str(x).strip()
            ][:8],
            "scan_calls": int(out.get("scan_calls") or 0),
            "scan_at": str(
                out.get("scan_at")
                or world.get("scan_at")
                or prior.get("scan_at")
                or ""
            ).strip(),
        }
        if str(payload.get("strat") or "") == "in_progress":
            brief = load_desk_brief()
            if brief.get("strat"):
                payload["previous_strat"] = brief.get("strat")
                payload["previous_sends"] = brief.get("sends") or 0
        LAST_TURN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        write_desk_brief(payload)
    except OSError:
        logger.debug("last_turn write failed", exc_info=True)


def stdout_printer(kind: str, text: str) -> None:
    t = ascii_text(text)
    if kind == "stage":
        label = t.strip().upper()
        banner = "GROK" if label in ("", "GROK", "JUDGE", "ACT") else f"GROK {label}"
        print(f"\n--- {banner} ---", flush=True)
        return
    if kind == "stage_end":
        print("", flush=True)
        return
    sys.stdout.write(t)
    sys.stdout.flush()
