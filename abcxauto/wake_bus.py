"""Standing IBKR + on-demand Grok. No clerk decision checklist.

Book events are facts. Grok sets the next alarm. Hard interrupts are gates.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO / "data" / "state" / "grok_wake.json"

BOOK_EVENTS = frozenset({
    "fill",
    "order_change",
    "unprotected",
    "session_change",
    "socket",
    "book_move",
})
HARD_INTERRUPTS = frozenset({"unprotected", "halt"})
ALL_WAKES = BOOK_EVENTS | HARD_INTERRUPTS | frozenset({"alarm", "operator", "boot"})
PULSE_S = 10.0
DEFAULT_LOOK_S = 90.0
DEFAULT_LOOK_OPEN_S = 60.0
MIN_LOOK_S = 30.0
PAPER_MAX_LOOK_S = 15 * 60.0
MTM_BUCKET_PCT = 8.0
_last_wake = None


def _path() -> Path:
    raw = (os.environ.get("ABCXAUTO_GROK_WAKE_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class BookEvent:
    kind: str
    detail: str = ""
    ts: float = field(default_factory=time.monotonic)

    def wake_key(self) -> str:
        if self.kind in HARD_INTERRUPTS:
            return self.kind
        if self.kind in BOOK_EVENTS:
            return self.kind
        return self.kind


def note_wake(event: BookEvent | None) -> None:
    global _last_wake
    _last_wake = event


def last_wake() -> BookEvent | None:
    return _last_wake


@dataclass
class GrokAlarm:
    wake_at: str | None = None
    wake_if: list[str] = field(default_factory=list)
    set_at: str = ""

    def due(self, now: datetime | None = None) -> bool:
        at = _parse_iso(self.wake_at or "")
        if at is None:
            return False
        clock = now or _utc_now()
        return clock >= at

    def seconds_until(self, now: datetime | None = None) -> float | None:
        at = _parse_iso(self.wake_at or "")
        if at is None:
            return None
        clock = now or _utc_now()
        return max(0.0, (at - clock).total_seconds())

    def accepts(self, event: BookEvent) -> bool:
        key = event.wake_key()
        if key in HARD_INTERRUPTS:
            return True
        if key in ("operator", "boot", "alarm"):
            return True
        wanted = [str(x).strip().lower() for x in self.wake_if if str(x).strip()]
        if not wanted:
            return key in BOOK_EVENTS
        return key in wanted


def load_alarm() -> GrokAlarm:
    p = _path()
    if not p.is_file():
        return GrokAlarm()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return GrokAlarm()
    if not isinstance(raw, dict):
        return GrokAlarm()
    ifs = raw.get("wake_if") or []
    if isinstance(ifs, str):
        ifs = [ifs]
    return GrokAlarm(
        wake_at=str(raw.get("wake_at") or "") or None,
        wake_if=[str(x).strip().lower() for x in ifs if str(x).strip()],
        set_at=str(raw.get("set_at") or ""),
    )


def save_alarm(alarm: GrokAlarm) -> GrokAlarm:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "wake_at": alarm.wake_at,
            "wake_if": list(alarm.wake_if),
            "set_at": alarm.set_at or _utc_now().isoformat(),
        }
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("grok_wake write failed", exc_info=True)
    return alarm


def clear_alarm() -> None:
    save_alarm(GrokAlarm(set_at=_utc_now().isoformat()))


def _env_float(name: str, default: float, *, lo: float = 1.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, float(raw))
    except ValueError:
        return default


def default_look_s(*, flat: bool | None = None, session: str = "") -> float:
    raw = (os.environ.get("ABCXAUTO_DEFAULT_LOOK_S") or "").strip()
    if raw:
        try:
            return max(MIN_LOOK_S, float(raw))
        except ValueError:
            pass
    if str(session or "").lower() == "regular" and flat is False:
        return DEFAULT_LOOK_OPEN_S
    return DEFAULT_LOOK_S


def min_look_s() -> float:
    return _env_float("ABCXAUTO_MIN_LOOK_S", MIN_LOOK_S, lo=5.0)


def paper_max_look_s() -> float:
    return _env_float("ABCXAUTO_PAPER_MAX_LOOK_S", PAPER_MAX_LOOK_S, lo=min_look_s())


def _paper_sit_ceiling_s(*, session: str) -> float | None:
    """Paper RTH, clerk not halted: cap the nap. Live / halted: none."""
    try:
        from abcxauto.lab_playbook import is_paper

        if not is_paper():
            return None
    except Exception:
        return None
    if str(session or "").lower() != "regular":
        return None
    try:
        from abcxauto.risk_gates import get_risk_gate

        if get_risk_gate().is_halted:
            return None
    except Exception:
        pass
    return paper_max_look_s()


def _floor_look_s(sec: float, *, session: str = "") -> float:
    """Anti-hammer floor. Paper RTH adds a sit ceiling unless the clerk halted."""
    out = max(min_look_s(), float(sec))
    cap = _paper_sit_ceiling_s(session=session)
    if cap is not None:
        out = min(out, cap)
    return out


def ensure_next_look(
    *,
    previous_set_at: str = "",
    flat: bool | None = None,
    session: str = "",
) -> GrokAlarm:
    """If Grok did not set a clock this turn, do not park the desk."""
    alarm = load_alarm()
    if alarm.set_at and alarm.set_at != previous_set_at:
        if alarm.wake_at:
            return alarm
        return set_wake(
            wake_in_s=default_look_s(flat=flat, session=session),
            wake_if=list(alarm.wake_if),
            flat=flat,
            session=session,
        )
    return set_wake(
        wake_in_s=default_look_s(flat=flat, session=session),
        flat=flat,
        session=session,
    )


def set_wake(
    *,
    wake_in_s: float | None = None,
    wake_at: str | None = None,
    wake_if: list[str] | str | None = None,
    flat: bool | None = None,
    session: str = "",
) -> GrokAlarm:
    """Grok-owned next look. Always a clock; book events can come sooner."""
    at = str(wake_at or "").strip() or None
    sec: float | None = None
    if wake_in_s is not None:
        try:
            sec = float(wake_in_s)
        except (TypeError, ValueError):
            sec = None
    if sec is None and at:
        dt = _parse_iso(at)
        if dt is not None:
            sec = (dt - _utc_now()).total_seconds()
    if sec is None:
        sec = default_look_s(flat=flat, session=session)
    sec = _floor_look_s(sec, session=session)
    at = datetime.fromtimestamp(time.time() + sec, tz=timezone.utc).isoformat()
    ifs: list[str] = []
    if isinstance(wake_if, str):
        ifs = [wake_if]
    elif isinstance(wake_if, list):
        ifs = [str(x) for x in wake_if]
    clean = []
    for item in ifs:
        key = str(item or "").strip().lower()
        if key in BOOK_EVENTS or key in HARD_INTERRUPTS:
            clean.append(key)
    alarm = GrokAlarm(
        wake_at=at,
        wake_if=clean,
        set_at=_utc_now().isoformat(),
    )
    return save_alarm(alarm)


def _mtm_bucket_pct() -> float:
    return _env_float("ABCXAUTO_MTM_BUCKET_PCT", MTM_BUCKET_PCT, lo=2.0)


def _first_num(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if row.get(key) is None:
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            continue
    return None


def _lot_mtm_key(pos: dict[str, Any]) -> str:
    ident = str(pos.get("conId") or pos.get("con_id") or pos.get("symbol") or "")
    avg = _first_num(pos, "avg", "avgCost", "avg_cost")
    mkt = _first_num(pos, "mkt", "market_price", "marketPrice", "last")
    qty = _first_num(pos, "quantity", "position") or 0.0
    if not avg or mkt is None:
        return ident
    try:
        if qty < 0:
            pct = (avg - mkt) / abs(avg) * 100.0
        else:
            pct = (mkt - avg) / abs(avg) * 100.0
    except ZeroDivisionError:
        return ident
    step = _mtm_bucket_pct()
    bucket = int(pct // step) * int(step)
    return f"{ident}:{bucket}"


def book_fingerprint(snap: dict[str, Any] | None) -> dict[str, Any]:
    s = snap if isinstance(snap, dict) else {}
    fills = s.get("fills") if isinstance(s.get("fills"), list) else []
    orders = s.get("open_orders") if isinstance(s.get("open_orders"), list) else []
    pos = s.get("positions") if isinstance(s.get("positions"), list) else []
    prot = s.get("protection") if isinstance(s.get("protection"), dict) else {}
    unprot = prot.get("unprotected_symbols") or []
    hours = s.get("market_hours") if isinstance(s.get("market_hours"), dict) else {}
    sess = ""
    block = hours.get("session")
    if isinstance(block, dict):
        sess = str(block.get("status") or "")
    elif isinstance(block, str):
        sess = block
    fill_keys = []
    for f in fills:
        if not isinstance(f, dict):
            continue
        fill_keys.append(
            f"{f.get('exec_id') or f.get('execution_id') or f.get('symbol')}:"
            f"{f.get('quantity') or f.get('shares')}:"
            f"{f.get('price') or f.get('avg_price')}"
        )
    order_keys = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        order_keys.append(str(o.get("order_id") or o.get("orderId") or o.get("perm_id") or ""))
    lot_keys = []
    lot_mtm = []
    for p in pos:
        if not isinstance(p, dict):
            continue
        ident = str(p.get("conId") or p.get("con_id") or p.get("symbol") or "")
        if ident:
            lot_keys.append(ident)
            lot_mtm.append(_lot_mtm_key(p))
    return {
        "fills": tuple(fill_keys),
        "orders": tuple(sorted(x for x in order_keys if x)),
        "lots": tuple(sorted(x for x in lot_keys if x)),
        "lot_mtm": tuple(sorted(x for x in lot_mtm if x)),
        "unprotected": tuple(sorted(str(x) for x in unprot if x)),
        "session": sess.lower(),
        "connected": bool(s.get("ibkr_connected") or (s.get("reality_pulse") or {}).get("ibkr_connected")),
    }


def events_from_diff(
    prev: dict[str, Any] | None,
    cur: dict[str, Any] | None,
) -> list[BookEvent]:
    """Facts only. First snap is not a flood — boot is a separate wake."""
    if not prev:
        return []
    a = prev
    b = cur if isinstance(cur, dict) else {}
    out: list[BookEvent] = []
    if a.get("fills") != b.get("fills"):
        out.append(BookEvent("fill", "fills changed"))
    if a.get("orders") != b.get("orders"):
        out.append(BookEvent("order_change", "working orders changed"))
    if a.get("lot_mtm") != b.get("lot_mtm") and (a.get("lot_mtm") or b.get("lot_mtm")):
        prev_m = set(a.get("lot_mtm") or ())
        now_m = set(b.get("lot_mtm") or ())
        changed = sorted(now_m - prev_m)[:6]
        out.append(BookEvent("book_move", ",".join(changed) or "marks"))
    if a.get("unprotected") != b.get("unprotected") and b.get("unprotected"):
        out.append(BookEvent("unprotected", ",".join(b.get("unprotected") or ())))
    if a.get("session") != b.get("session") and (a.get("session") or b.get("session")):
        out.append(BookEvent(
            "session_change",
            f"{a.get('session') or '?'}->{b.get('session') or '?'}",
        ))
    if bool(a.get("connected")) != bool(b.get("connected")):
        out.append(BookEvent(
            "socket",
            "up" if b.get("connected") else "down",
        ))
    return out


def should_wake_grok(
    events: list[BookEvent],
    *,
    alarm: GrokAlarm | None = None,
    first_boot: bool = False,
    operator: bool = False,
) -> BookEvent | None:
    if first_boot:
        return BookEvent("boot", "first look")
    if operator:
        return BookEvent("operator", "desktop wake")
    al = alarm or load_alarm()
    if al.due():
        return BookEvent("alarm", al.wake_at or "wake_at")
    for ev in events:
        if al.accepts(ev):
            return ev
    return None


def pulse_sleep_s(alarm: GrokAlarm | None = None) -> float:
    al = alarm or load_alarm()
    until = al.seconds_until()
    if until is None:
        return PULSE_S
    return max(1.0, min(PULSE_S, until))
