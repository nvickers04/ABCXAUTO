"""Overnight / after-close park clock. Clerk is not an RTH runner.

Book events are facts. Hard interrupts poke the open think. Regular hours
have no sit clock — a finished look does not write grok_wake.json.
Premarket stays up on the same process. Closed / postmarket parks until
the last hour to the open.
"""

from __future__ import annotations

import json
import logging
import os
import random
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
# Live poke into the open xAI episode (same chat). Not a sit-clock.
LIVE_POKE_KINDS = frozenset({"fill", "order_change", "unprotected"})
PULSE_S = 10.0
DEFAULT_LOOK_S = 90.0
DEFAULT_LOOK_OPEN_S = 300.0
DEFAULT_LOOK_HUNT_S = 600.0
LAST_HOUR_LOOK_S = 90.0
MIN_LOOK_S = 30.0
NEXT_LOOK_S_MAX = 4 * 3600.0
# Overnight / after-close only. Premarket stay-up is not a park.
PARK_CLOCK_SESSIONS = frozenset({"closed", "postmarket"})
PAPER_STAY_UP_SESSIONS = frozenset({"regular", "premarket"})
# 04:00 ET premarket start is 5.5h before the 09:30 bell.
PREMARKET_MINUTES_TO_OPEN = 5.5 * 60.0
# Pacing class: a 30-minute remaining-to-bell wait is a park, not a look.
REMAINING_TO_BELL_S = 30 * 60.0
STAY_UP_RETRY_MIN_S = 20.0
STAY_UP_RETRY_MAX_S = 45.0
# Consecutive failed looks escalate. A provider capacity error is not a
# 20-second problem — retrying it just re-bills the prompt for nothing.
FAILED_LOOK_BACKOFF_CAP_S = 600.0
PROVIDER_BACKOFF_MIN_S = 90.0
PROVIDER_BACKOFF_CAP_S = 900.0
MTM_BUCKET_PCT = 8.0
_last_wake = None
_pending_interrupt = None  # BookEvent | None — set after BookEvent is defined


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


def note_interrupt(event: BookEvent | None) -> None:
    """Clerk live event → poke the open xAI episode mid-turn when applicable."""
    global _pending_interrupt
    if event is None:
        return
    kind = str(event.kind or "").strip().lower()
    if kind not in LIVE_POKE_KINDS:
        return
    _pending_interrupt = BookEvent(kind, str(event.detail or ""), ts=event.ts)


def peek_interrupt() -> BookEvent | None:
    return _pending_interrupt


def take_interrupt() -> BookEvent | None:
    global _pending_interrupt
    ev = _pending_interrupt
    _pending_interrupt = None
    return ev


def clear_interrupt() -> None:
    global _pending_interrupt
    _pending_interrupt = None


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


def set_wake_offered(*, session: str = "") -> bool:
    """Never. Cadence is clerk + playbook, not a Grok tool."""
    _ = session
    return False


def paper_stay_up(*, session: str = "") -> bool:
    """Paper regular + premarket: keep looking on this process. No sit clock."""
    if str(session or "").lower() not in PAPER_STAY_UP_SESSIONS:
        return False
    try:
        from abcxauto.config import get_config

        if not bool(get_config().is_paper):
            return False
    except Exception:
        pass
    return True


def session_is_park(
    session: str = "",
    *,
    minutes_to_open: float | None = None,
) -> bool:
    """True only for overnight / after-close. RTH and premarket are not parks."""
    _ = minutes_to_open
    sess = str(session or "").lower()
    if sess in PARK_CLOCK_SESSIONS:
        return True
    if sess in ("regular", "premarket"):
        return False
    if not sess:
        inferred, mins = infer_session_before_open()
        if inferred == "closed":
            return True
        if inferred == "premarket":
            return False
        _ = mins
    return False


def clear_park() -> GrokAlarm:
    """Drop grok_wake.json. RTH / premarket stay-up is not a sit clock."""
    return save_alarm(GrokAlarm())


def _retry_base_s() -> tuple[float, bool]:
    """Floor the escalation doubles from, and whether the operator pinned it.

    A pinned cadence is honored exactly — no jitter on top of an explicit ask.
    """
    raw = (os.environ.get("ABCXAUTO_STAY_UP_RETRY_S") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw)), True
        except ValueError:
            pass
    return float(STAY_UP_RETRY_MIN_S), False


def failed_look_backoff_s(streak: int, *, overloaded: bool = False) -> float:
    """Backoff for the Nth consecutive failed look. Doubles, then caps.

    ``overloaded`` is an xAI capacity/rate refusal — start high, cap higher.
    Jitter rides on top of the step so two desks do not retry in lockstep, and
    never enough to make a later strike come back sooner than an earlier one.
    """
    n = max(1, int(streak or 1))
    pinned = False
    if overloaded:
        base, cap = float(PROVIDER_BACKOFF_MIN_S), float(PROVIDER_BACKOFF_CAP_S)
    else:
        base, pinned = _retry_base_s()
        cap = float(FAILED_LOOK_BACKOFF_CAP_S)
    step = min(cap, base * (2 ** min(n - 1, 16)))
    if pinned:
        return float(step)
    return float(min(cap, step + random.random() * step * 0.25))


def _floor_look_s(sec: float, *, session: str = "") -> float:
    """Min look floor for a real park."""
    _ = session
    return max(min_look_s(), float(sec))


def et_minutes_to_rth_open(*, now: datetime | None = None) -> float | None:
    """Minutes to today's 09:30 ET. None when already open or not a weekday."""
    try:
        from zoneinfo import ZoneInfo

        clock = now or datetime.now(ZoneInfo("America/New_York"))
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            clock = clock.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None
    if clock.weekday() >= 5:
        return None
    bell = clock.replace(hour=9, minute=30, second=0, microsecond=0)
    if clock >= bell:
        return None
    return (bell - clock).total_seconds() / 60.0


def infer_session_before_open(*, now: datetime | None = None) -> tuple[str, float | None]:
    """Session label before the RTH bell. Overnight is closed, not premarket."""
    mins = et_minutes_to_rth_open(now=now)
    if mins is None:
        return "", None
    if mins > PREMARKET_MINUTES_TO_OPEN:
        return "closed", mins
    return "premarket", mins


def remaining_to_bell_s(
    until_s: float | None,
    minutes_to_open: float | None = None,
) -> bool:
    """True when this wait sits out until the open, or is a 30-minute park."""
    try:
        until = float(until_s)
    except (TypeError, ValueError):
        return False
    if until != until or until <= 0:
        return False
    if until + 1.0 >= float(REMAINING_TO_BELL_S):
        return True
    try:
        bell = float(minutes_to_open) * 60.0
    except (TypeError, ValueError):
        return False
    if bell != bell or bell <= 0:
        return False
    return until + 1.0 >= bell


def start_looks_now(
    alarm: GrokAlarm | None = None,
    *,
    minutes_to_open: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Operator Start / AUTOSTART. Overnight closed parks stand. Else look now.

    A leftover RTH sit clock is not a park — clerk is not a runner.
    Premarket stay-up looks through to the bell.
    """
    al = alarm or load_alarm()
    if not al.wake_at or al.due(now=now):
        return True
    mins = minutes_to_open
    if mins is None:
        mins = et_minutes_to_rth_open(now=now)
    if mins is None:
        return True
    try:
        mins = float(mins)
    except (TypeError, ValueError):
        return True
    if mins > PREMARKET_MINUTES_TO_OPEN:
        return False
    return True


def minutes_to_open_from_snap(snap: dict[str, Any] | None) -> float | None:
    """Minutes to the RTH open from a snap pulse or market_hours block."""
    s = snap if isinstance(snap, dict) else {}
    pulse = s.get("reality_pulse") if isinstance(s.get("reality_pulse"), dict) else {}
    sess = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    if sess.get("countdown_to") == "open" and sess.get("countdown_s") is not None:
        try:
            return max(0.0, float(sess["countdown_s"]) / 60.0)
        except (TypeError, ValueError):
            pass
    hours = s.get("market_hours") if isinstance(s.get("market_hours"), dict) else {}
    if hours.get("minutes_to_open") is not None:
        try:
            return max(0.0, float(hours["minutes_to_open"]))
        except (TypeError, ValueError):
            pass
    return None


def clamp_next_look_s(raw: Any) -> float | None:
    """Card cadence hint. Floor MIN_LOOK_S, cap NEXT_LOOK_S_MAX."""
    try:
        sec = float(raw)
    except (TypeError, ValueError):
        return None
    if sec != sec or sec <= 0:
        return None
    return max(min_look_s(), min(float(NEXT_LOOK_S_MAX), sec))


def _stay_up_look_cap_s(
    session: str,
    minutes_to_open: float | None,
) -> float | None:
    """Premarket cadence cap. Card hints may tighten this, not stretch to the bell."""
    sess = str(session or "").lower()
    mins = None
    if minutes_to_open is not None:
        try:
            mins = float(minutes_to_open)
        except (TypeError, ValueError):
            mins = None
        if mins is not None and mins != mins:
            mins = None
    last_hour = (
        mins is not None
        and 0 < mins <= 60
        and sess in ("premarket", "closed", "postmarket")
    )
    if last_hour:
        return float(LAST_HOUR_LOOK_S)
    if sess == "premarket":
        return float(DEFAULT_LOOK_HUNT_S)
    return None


def clerk_look_s(
    *,
    flat: bool | None = None,
    session: str = "",
    minutes_to_open: float | None = None,
    next_look_s: float | None = None,
) -> float:
    """Clerk next-look seconds. Playbook card may tighten or stretch inside the floor.

    Session-card opening-print wait is a send gate, not this clock. Premarket
    stays on hunt / last-hour cadence. Overnight closed still parks to the
    last hour before the open.
    """
    sess = str(session or "").lower()
    mins = minutes_to_open
    if mins is not None:
        try:
            mins = float(mins)
        except (TypeError, ValueError):
            mins = None
        if mins is not None and mins != mins:
            mins = None
    last_hour = (
        mins is not None
        and 0 < mins <= 60
        and sess in ("premarket", "closed", "postmarket")
    )
    if sess in ("closed", "postmarket") and mins is not None and mins > 60:
        return max(min_look_s(), (mins - 60.0) * 60.0)
    cap = _stay_up_look_cap_s(sess, mins)
    if next_look_s is None:
        try:
            from abcxauto.lab_playbook import playbook_next_look_s

            next_look_s = playbook_next_look_s()
        except Exception:
            next_look_s = None
    if next_look_s is not None:
        clamped = clamp_next_look_s(next_look_s)
        if clamped is not None:
            if sess == "regular" and flat is False:
                return max(clamped, float(DEFAULT_LOOK_OPEN_S))
            if cap is not None:
                return max(min_look_s(), min(clamped, cap))
            return clamped
    raw = (os.environ.get("ABCXAUTO_DEFAULT_LOOK_S") or "").strip()
    if raw:
        return default_look_s(flat=flat, session=session)
    if last_hour:
        return max(min_look_s(), float(LAST_HOUR_LOOK_S))
    if sess == "regular" and flat is False:
        return max(min_look_s(), float(DEFAULT_LOOK_OPEN_S))
    if sess in ("regular", "premarket") and flat is not False:
        return max(min_look_s(), float(DEFAULT_LOOK_HUNT_S))
    return default_look_s(flat=flat, session=session)


def ensure_next_look(
    *,
    previous_set_at: str = "",
    flat: bool | None = None,
    session: str = "",
    minutes_to_open: float | None = None,
    replace: bool = False,
) -> GrokAlarm:
    """Overnight / after-close park only. RTH and premarket write no sit clock.

    Clerk is not a runner. A finished regular look must not seed grok_wake.json.
    ``previous_set_at`` is unused.
    """
    _ = previous_set_at
    sess = str(session or "").lower()
    mins = minutes_to_open
    if mins is None:
        inferred, mins = infer_session_before_open()
        if not sess:
            sess = inferred
    if not session_is_park(sess, minutes_to_open=mins):
        return clear_park()
    alarm = load_alarm()
    if alarm.wake_at and not alarm.due() and not replace:
        return alarm
    return set_wake(
        wake_in_s=clerk_look_s(
            flat=flat,
            session=sess,
            minutes_to_open=mins,
        ),
        flat=flat,
        session=sess,
    )


def _clean_wake_if(wake_if: list[str] | str | None) -> list[str]:
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
    return clean


def set_wake(
    *,
    wake_in_s: float | None = None,
    wake_at: str | None = None,
    wake_if: list[str] | str | None = None,
    flat: bool | None = None,
    session: str = "",
) -> GrokAlarm:
    """Overnight / after-close park. RTH and premarket write no sit clock."""
    sess = str(session or "").lower()
    if not session_is_park(sess):
        return clear_park()
    clean = _clean_wake_if(wake_if)
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
    return save_alarm(
        GrokAlarm(
            wake_at=at,
            wake_if=clean,
            set_at=_utc_now().isoformat(),
        )
    )


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
