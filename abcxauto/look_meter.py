"""Per-look cost meter. Operator-facing; never added to the model prompt.

``model_usage`` stays the per-call fuse feed (input/output/cached/cost). It
cannot hold look id, the re-billing curve, tool counts, result characters,
or duration — and this module must not edit ``memory/schema.py``. Rows land
in ``look_meter.db`` next to the journal (or ``ABCXAUTO_LOOK_METER_PATH``).

Every write is fail-safe: log and swallow. A dark meter must not break a look.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS look_meter (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    look_id TEXT NOT NULL,
    cycle INTEGER,
    session TEXT,
    model TEXT,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    duration_s REAL NOT NULL DEFAULT 0,
    tool_counts_json TEXT,
    tool_result_chars INTEGER NOT NULL DEFAULT 0,
    call_inputs_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_look_meter_ts ON look_meter(ts);
CREATE INDEX IF NOT EXISTS idx_look_meter_look_id ON look_meter(look_id);
"""

_current: contextvars.ContextVar["LookMeter | None"] = contextvars.ContextVar(
    "abcx_look_meter", default=None
)
_io_lock = threading.RLock()
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def look_meter_path() -> str:
    """Sidecar SQLite path. Prefers explicit env, then the journal's directory."""
    env = (os.environ.get("ABCXAUTO_LOOK_METER_PATH") or "").strip()
    if env:
        return env
    jenv = (os.environ.get("ABCXAUTO_JOURNAL_PATH") or "").strip()
    if jenv:
        return str(Path(jenv).with_name("look_meter.db"))
    try:
        from abcxauto.memory import get_journal

        jp = str(getattr(get_journal(), "path", "") or "").strip()
        if jp:
            return str(Path(jp).with_name("look_meter.db"))
    except Exception:
        logger.debug("look_meter journal path probe failed", exc_info=True)
    return str(Path(__file__).resolve().parents[1] / "look_meter.db")


def _connect(path: str) -> sqlite3.Connection:
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(path: str) -> None:
    with _init_lock:
        if path in _initialized_paths:
            return
        with _connect(path) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
        _initialized_paths.add(path)


def _write_row(row: dict[str, Any]) -> None:
    path = look_meter_path()
    _ensure_schema(path)
    with _io_lock:
        with _connect(path) as conn:
            conn.execute(
                """
                INSERT INTO look_meter (
                    ts, look_id, cycle, session, model, calls,
                    input_tokens, cached_tokens, output_tokens, reasoning_tokens,
                    cost_usd, duration_s, tool_counts_json, tool_result_chars,
                    call_inputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ts"],
                    row["look_id"],
                    row["cycle"],
                    row["session"],
                    row["model"],
                    int(row["calls"] or 0),
                    int(row["input_tokens"] or 0),
                    int(row["cached_tokens"] or 0),
                    int(row["output_tokens"] or 0),
                    int(row["reasoning_tokens"] or 0),
                    float(row["cost_usd"] or 0.0),
                    float(row["duration_s"] or 0.0),
                    row["tool_counts_json"],
                    int(row["tool_result_chars"] or 0),
                    row["call_inputs_json"],
                ),
            )
            conn.commit()


def _row_to_public(raw: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {k: raw[k] for k in raw.keys()}
    tools: dict[str, int] = {}
    try:
        parsed = json.loads(str(raw.get("tool_counts_json") or "{}"))
        if isinstance(parsed, dict):
            tools = {str(k): int(v) for k, v in parsed.items()}
    except (TypeError, ValueError, json.JSONDecodeError):
        tools = {}
    curve: list[int] = []
    try:
        parsed_c = json.loads(str(raw.get("call_inputs_json") or "[]"))
        if isinstance(parsed_c, list):
            curve = [int(x) for x in parsed_c]
    except (TypeError, ValueError, json.JSONDecodeError):
        curve = []
    return {
        "id": raw.get("id"),
        "ts": raw.get("ts"),
        "look_id": raw.get("look_id"),
        "cycle": raw.get("cycle"),
        "session": raw.get("session"),
        "model": raw.get("model"),
        "calls": int(raw.get("calls") or 0),
        "input_tokens": int(raw.get("input_tokens") or 0),
        "cached_tokens": int(raw.get("cached_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "reasoning_tokens": int(raw.get("reasoning_tokens") or 0),
        "cost_usd": float(raw.get("cost_usd") or 0.0),
        "duration_s": float(raw.get("duration_s") or 0.0),
        "tool_counts": tools,
        "tool_result_chars": int(raw.get("tool_result_chars") or 0),
        "call_inputs": curve,
    }


class LookMeter:
    """In-memory accumulator for one look. ``finish`` persists one row."""

    def __init__(
        self,
        *,
        look_id: str,
        cycle: int | None,
        session: str,
        model: str,
    ) -> None:
        self.look_id = look_id
        self.cycle = cycle
        self.session = session
        self.model = model
        self.t0 = time.monotonic()
        self.ts = _utc_now_iso()
        self.calls = 0
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.cost_usd = 0.0
        self.tool_counts: dict[str, int] = {}
        self.tool_result_chars = 0
        self.call_inputs: list[int] = []
        self._finished = False

    def note_call(self, used: dict[str, Any] | None, *, model: str = "") -> None:
        blob = dict(used or {})
        inn = int(blob.get("input_tokens") or 0)
        cached = int(blob.get("cached_tokens") or 0)
        out = int(blob.get("output_tokens") or 0)
        reason = int(blob.get("reasoning_tokens") or 0)
        self.calls += 1
        self.input_tokens += inn
        self.cached_tokens += cached
        self.output_tokens += out
        self.reasoning_tokens += reason
        self.call_inputs.append(inn + cached)
        if model:
            self.model = str(model)
        try:
            from abcxauto.scorecard import estimate_cost_usd

            self.cost_usd += float(estimate_cost_usd(inn, out, cached_tokens=cached))
        except Exception:
            logger.debug("look_meter cost estimate failed", exc_info=True)

    def note_tool(self, name: str, result: str) -> None:
        key = str(name or "").strip() or "?"
        self.tool_counts[key] = int(self.tool_counts.get(key) or 0) + 1
        self.tool_result_chars += len(str(result or ""))

    def note_tools(self, names: list[str] | None) -> None:
        for name in names or []:
            key = str(name or "").strip() or "?"
            if key not in self.tool_counts:
                self.tool_counts[key] = 0

    def as_row(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "look_id": self.look_id,
            "cycle": self.cycle,
            "session": self.session or None,
            "model": self.model or None,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "duration_s": max(0.0, time.monotonic() - self.t0),
            "tool_counts_json": json.dumps(self.tool_counts, default=str),
            "tool_result_chars": self.tool_result_chars,
            "call_inputs_json": json.dumps(self.call_inputs, default=str),
        }

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            _write_row(self.as_row())
        except Exception:
            logger.exception("look_meter write failed")


def current_look_meter() -> LookMeter | None:
    return _current.get()


def note_model_call(used: dict[str, Any] | None, *, model: str = "") -> None:
    meter = _current.get()
    if meter is None:
        return
    try:
        meter.note_call(used, model=model)
    except Exception:
        logger.exception("look_meter note_model_call failed")


def note_tool_result(name: str, result: str) -> None:
    meter = _current.get()
    if meter is None:
        return
    try:
        meter.note_tool(name, result)
    except Exception:
        logger.exception("look_meter note_tool_result failed")


def _look_cycle(world: Any, snap: dict[str, Any] | None) -> int | None:
    for src in (snap,):
        if isinstance(src, dict) and src.get("cycle") is not None:
            try:
                return int(src["cycle"])
            except (TypeError, ValueError):
                pass
    raw = getattr(world, "cycle", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


@contextmanager
def look_meter_scope(
    *,
    world: Any = None,
    snap: dict[str, Any] | None = None,
    model: str = "",
    session: str = "",
) -> Iterator[LookMeter | None]:
    """One look, one row. Enter/exit never raise to the caller."""
    meter: LookMeter | None = None
    token = None
    try:
        sess = str(
            session
            or getattr(world, "session_status", "")
            or (snap or {}).get("session")
            or ""
        )
        if isinstance((snap or {}).get("session"), dict):
            sess = str((snap or {}).get("session", {}).get("session") or sess)
        meter = LookMeter(
            look_id=uuid.uuid4().hex,
            cycle=_look_cycle(world, snap),
            session=sess,
            model=str(model or ""),
        )
        token = _current.set(meter)
    except Exception:
        logger.exception("look_meter begin failed")
        meter = None
    try:
        yield meter
    finally:
        try:
            if meter is not None:
                meter.finish()
        except Exception:
            logger.exception("look_meter finish failed")
        if token is not None:
            try:
                _current.reset(token)
            except Exception:
                logger.debug("look_meter context reset failed", exc_info=True)


def look_meter_for_desk(*, limit: int = 32) -> list[dict[str, Any]]:
    """Recent look-cost rows for the desktop cockpit. Never raises."""
    try:
        cap = max(1, min(int(limit or 32), 500))
        path = look_meter_path()
        if not Path(path).is_file():
            return []
        _ensure_schema(path)
        with _io_lock:
            with _connect(path) as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, look_id, cycle, session, model, calls,
                           input_tokens, cached_tokens, output_tokens,
                           reasoning_tokens, cost_usd, duration_s,
                           tool_counts_json, tool_result_chars, call_inputs_json
                    FROM look_meter
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (cap,),
                ).fetchall()
        return [_row_to_public(r) for r in rows]
    except Exception:
        logger.exception("look_meter_for_desk failed")
        return []


def last_look_meter() -> dict[str, Any] | None:
    rows = look_meter_for_desk(limit=1)
    return rows[0] if rows else None
