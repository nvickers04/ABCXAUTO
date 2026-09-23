"""Session work card: open job across looks.

Persists ruled-out names, candidate, undeployed cash, dossier gaps, pace,
and idle hours. Not the W39 research-brief card. No verdict field.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
DEFAULT_PATH = _STATE_DIR / "session_work.json"
_MIN_HOURS = 1.0  # one hour — early-session pace ≈ daily pnl, not 60x


def default_path() -> Path:
    return DEFAULT_PATH


def _resolve(path: Path | str | None) -> Path:
    if path is None:
        return DEFAULT_PATH
    return Path(path)


def empty_card() -> dict[str, Any]:
    return {
        "session": "",
        "asof": "",
        "cash": 0.0,
        "held": [],
        "ruled_out": [],
        "candidate": None,
        "gaps": [],
        "cash_undeployed": False,
        "pace_usd_per_h": 0.0,
        "idle_h": 0.0,
    }


def load(path: Path | str | None = None) -> dict[str, Any]:
    """Load session work JSON. Missing / unreadable → empty card."""
    p = _resolve(path)
    if not p.is_file():
        return empty_card()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.debug("session_work read failed", exc_info=True)
        return empty_card()
    if not isinstance(raw, dict):
        return empty_card()
    return _normalize(raw)


def save(card: dict[str, Any] | None, path: Path | str | None = None) -> dict[str, Any]:
    """Atomic write of a normalized card. Returns what was written."""
    blob = _normalize(card if isinstance(card, dict) else {})
    # Never persist a verdict — this is not the W39 brief card.
    blob.pop("verdict", None)
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        logger.debug("session_work write failed", exc_info=True)
    return blob


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    out = empty_card()
    out["session"] = str(raw.get("session") or "").strip()
    out["asof"] = str(raw.get("asof") or "").strip()
    out["cash"] = _finite_float(raw.get("cash"), 0.0)
    out["held"] = _held_list(raw.get("held"))
    out["ruled_out"] = _str_list(raw.get("ruled_out"))
    cand = raw.get("candidate")
    out["candidate"] = dict(cand) if isinstance(cand, dict) else None
    out["gaps"] = _str_list(raw.get("gaps"))
    out["cash_undeployed"] = bool(raw.get("cash_undeployed"))
    out["pace_usd_per_h"] = _finite_float(raw.get("pace_usd_per_h"), 0.0)
    out["idle_h"] = _finite_float(raw.get("idle_h"), 0.0)
    idle_since = raw.get("idle_since")
    if idle_since not in (None, ""):
        out["idle_since"] = str(idle_since)
    return out


def _finite_float(raw: Any, default: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(val):
        return default
    return val


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip().upper()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _held_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").strip().upper()
        if not sym:
            continue
        qty = _finite_float(item.get("qty"), 0.0)
        rows.append({"symbol": sym, "qty": qty})
    return rows


def _now_et(now: datetime | None) -> datetime:
    clock = now or datetime.now(_ET)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=_ET)
    return clock.astimezone(_ET)


def _parse_iso(raw: str | None) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt.astimezone(_ET)


def session_hours(session: str, *, now: datetime | None = None) -> float:
    """Hours since the session pace clock. Floor at one hour."""
    clock = _now_et(now)
    sess = str(session or "").strip().lower()
    if sess in ("premarket", "closed"):
        anchor = datetime.combine(clock.date(), time(4, 0), tzinfo=_ET)
    else:
        # regular (and postmarket): hours since the RTH open
        anchor = datetime.combine(clock.date(), time(9, 30), tzinfo=_ET)
    hours = (clock - anchor).total_seconds() / 3600.0
    if not math.isfinite(hours) or hours < _MIN_HOURS:
        return _MIN_HOURS
    return hours


def pace_usd_per_h(
    daily_pnl: Any,
    session: str,
    *,
    now: datetime | None = None,
) -> float:
    """daily_pnl / session hours. Negative pnl → negative pace."""
    pnl = _finite_float(daily_pnl, 0.0)
    hours = session_hours(session, now=now)
    return pnl / hours


def idle_h(
    *,
    idle_since: str | None = None,
    now: datetime | None = None,
) -> float:
    """Hours since cash_undeployed became true (idle_since iso)."""
    start = _parse_iso(idle_since)
    if start is None:
        return 0.0
    clock = _now_et(now)
    hours = (clock - start).total_seconds() / 3600.0
    if not math.isfinite(hours) or hours < 0:
        return 0.0
    return hours


def _candidate_sizeable(cand: Any) -> bool:
    if isinstance(cand, dict):
        return bool(cand.get("sizeable"))
    if isinstance(cand, list):
        return any(isinstance(row, dict) and bool(row.get("sizeable")) for row in cand)
    return False


def _gaps_nonempty(card: dict[str, Any]) -> bool:
    gaps = card.get("gaps")
    if not isinstance(gaps, list):
        return False
    return any(str(g or "").strip() for g in gaps)


def card_is_open(card: dict[str, Any] | None) -> bool:
    """Open when undeployed cash has sizeable work or gaps, or sizeable candidate."""
    if not isinstance(card, dict):
        return False
    cand = card.get("candidate")
    if isinstance(cand, dict) and bool(cand.get("sizeable")):
        return True
    if bool(card.get("cash_undeployed")) and (
        _candidate_sizeable(cand) or _gaps_nonempty(card)
    ):
        return True
    # Undeployed cash alone keeps the job open (pace still standing still).
    if bool(card.get("cash_undeployed")):
        return True
    return False


def note_no_ticket(card: dict[str, Any] | None) -> dict[str, Any]:
    """No ticket does not rule out and does not close undeployed cash."""
    blob = _normalize(card if isinstance(card, dict) else {})
    # Explicitly do not touch ruled_out or cash_undeployed.
    return blob


def rule_out(card: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    """Add symbol to ruled_out; clear matching candidate. May close sizeable work."""
    blob = _normalize(card if isinstance(card, dict) else {})
    sym = str(symbol or "").strip().upper()
    if not sym:
        return blob
    ruled = list(blob.get("ruled_out") or [])
    if sym not in [str(x).upper() for x in ruled]:
        ruled.append(sym)
    blob["ruled_out"] = ruled
    cand = blob.get("candidate")
    if isinstance(cand, dict):
        cand_sym = str(cand.get("symbol") or "").strip().upper()
        if cand_sym == sym:
            blob["candidate"] = None
    # Close flag only when nothing sizeable / gaps remain with undeployed cash
    # still set — card_is_open decides; we do not force cash_undeployed False
    # unless there is no remaining open work at all.
    if not _gaps_nonempty(blob) and not _candidate_sizeable(blob.get("candidate")):
        # Sizeable name is gone; leave cash_undeployed as-is so pace/idle stay
        # visible, but card_is_open still true while cash sits. Caller may clear.
        pass
    return blob


def mark_sent(card: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    """Close the candidate that was sent."""
    blob = _normalize(card if isinstance(card, dict) else {})
    sym = str(symbol or "").strip().upper()
    cand = blob.get("candidate")
    if isinstance(cand, dict):
        cand_sym = str(cand.get("symbol") or "").strip().upper()
        if not sym or cand_sym == sym:
            blob["candidate"] = None
    return blob


def _fmt_usd_compact(val: float) -> str:
    """Compact dollars for the wake lead — nearest dollar, no cents."""
    return f"${int(round(float(val)))}"


def _fmt_num(val: float) -> str:
    n = round(float(val), 2)
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    text = f"{n:.2f}".rstrip("0").rstrip(".")
    return text


def _held_bit(held: list[dict[str, Any]]) -> str:
    if not held:
        return "none"
    parts: list[str] = []
    for row in held:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        qty = _finite_float(row.get("qty"), 0.0)
        if abs(qty - round(qty)) < 1e-9:
            qty_s = str(int(round(qty)))
        else:
            qty_s = _fmt_num(qty)
        parts.append(f"{sym}{qty_s}")
    return ",".join(parts) if parts else "none"


def _candidate_bit(cand: Any) -> str:
    if not isinstance(cand, dict):
        return "none"
    sym = str(cand.get("symbol") or "").strip().upper()
    return sym if sym else "none"


def continuing_line(card: dict[str, Any] | None) -> str:
    """Wake lead for the open job. Facts only — no sell/rotate/should/must."""
    blob = card if isinstance(card, dict) else {}
    cash = _finite_float(blob.get("cash"), 0.0)
    held = _held_list(blob.get("held"))
    ruled = _str_list(blob.get("ruled_out"))
    pace = _finite_float(blob.get("pace_usd_per_h"), 0.0)
    idle = _finite_float(blob.get("idle_h"), 0.0)
    ruled_s = ",".join(ruled) if ruled else "none"
    return (
        f"continuing cash={_fmt_usd_compact(cash)} "
        f"held={_held_bit(held)} "
        f"candidate={_candidate_bit(blob.get('candidate'))} "
        f"ruled_out={ruled_s} "
        f"pace={_fmt_usd_compact(pace)}/h "
        f"idle_h={_fmt_num(idle)}"
    )


def fingerprint(
    card: dict[str, Any] | None,
    lots: list[dict[str, Any]] | None = None,
) -> tuple[Any, ...]:
    """Unchanged-print key: cash to the dollar, stops, qtys (and session)."""
    blob = card if isinstance(card, dict) else {}
    cash = int(round(_finite_float(blob.get("cash"), 0.0)))
    session = str(blob.get("session") or "").strip().lower()
    lot_keys: list[tuple[str, float, float | None]] = []
    for lot in lots or []:
        if not isinstance(lot, dict):
            continue
        sym = str(lot.get("symbol") or "").strip().upper()
        if not sym:
            continue
        qty = _finite_float(
            lot.get("qty") if lot.get("qty") is not None else lot.get("position"),
            0.0,
        )
        stop_raw = lot.get("stop")
        if stop_raw is None:
            stop_raw = lot.get("last_stop")
        if stop_raw is None:
            stop_raw = lot.get("stop_price")
        try:
            stop = float(stop_raw) if stop_raw is not None else None
        except (TypeError, ValueError):
            stop = None
        if stop is not None and not math.isfinite(stop):
            stop = None
        lot_keys.append((sym, round(qty, 6), None if stop is None else round(stop, 4)))
    lot_keys.sort()
    return (cash, session, tuple(lot_keys))
