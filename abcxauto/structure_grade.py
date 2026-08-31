"""Structure referee + gradebook — Grok owns prices; code rejects illegal geometry
and records lessons for the next send.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVENTS_PATH = _REPO_ROOT / "structure_events.jsonl"
_SCRAPE_SECONDS = 15.0

# Reason codes (stable for prompts / UI)
GEOMETRY_STOP_WRONG_SIDE = "geometry_stop_wrong_side"
GEOMETRY_STOP_TOO_TIGHT = "geometry_stop_too_tight"
GEOMETRY_STOP_TOO_WIDE = "geometry_stop_too_wide"
GEOMETRY_ENTRY_STALE = "geometry_entry_stale"
GEOMETRY_QUOTE_REQUIRED = "geometry_quote_required"
GEOMETRY_TARGET_WRONG_SIDE = "geometry_target_wrong_side"
SCRAPE_SUSPECT = "scrape_suspect"
GEOMETRY_REJECTED = "geometry_rejected"
STRUCTURE_OK = "ok"

# Invented % codes — leftover lessons only. Never a send or cooldown gate.
_INVENTED_PCT_REASON_CODES = frozenset(
    {
        GEOMETRY_STOP_TOO_TIGHT,
        GEOMETRY_STOP_TOO_WIDE,
        GEOMETRY_ENTRY_STALE,
    }
)


def _path_events() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_STRUCTURE_EVENTS_PATH", "").strip()
    return Path(raw) if raw else _EVENTS_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def posture_stop_bands(posture: str) -> tuple[float, float]:
    """Min/max stop distance as fraction of quote."""
    p = (posture or "").lower()
    if p == "defensive":
        return 0.0025, 0.03  # 0.25% – 3%
    if p == "aggressive":
        return 0.0015, 0.06  # 0.15% – 6%
    return 0.002, 0.05  # balanced 0.2% – 5%


def resolve_entry_proxy(
    strategy: str,
    params: dict[str, Any],
    quote_last: float | None,
) -> float | None:
    """Best available entry proxy for geometry checks."""
    if quote_last is not None:
        try:
            q = float(quote_last)
            if q > 0:
                return q
        except (TypeError, ValueError):
            pass
    for key in ("price_hint", "entry_price"):
        raw = params.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    if strategy == "bracket":
        try:
            v = float(params.get("entry_price") or 0)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def session_usable(session: Any) -> bool:
    """Today's RTH session may pin a stop. Missing or prior-day tape must not."""
    return isinstance(session, dict) and session.get("today") is True


def stop_pins_session(direction: str, stop: float, session: Any) -> bool:
    """True when the stop is at or slightly under/over this session's extreme.

    The live gap card stops under the opening low. That distance is the tape,
    not the generic 0.2–5% posture band.
    """
    if not session_usable(session):
        return False
    side = str(direction or "").upper()
    try:
        if side == "LONG":
            low = float(session.get("low"))
            return low > 0 and low * 0.98 <= float(stop) <= low + 1e-9
        if side == "SHORT":
            high = float(session.get("high"))
            return high > 0 and high - 1e-9 <= float(stop) <= high * 1.02
    except (TypeError, ValueError):
        return False
    return False


def check_live_geometry(
    strategy: str,
    params: dict[str, Any],
    *,
    quote_last: float | None = None,
    posture: str = "balanced",
    session: Any = None,
    require_live: bool = False,
) -> tuple[bool, str, str]:
    """Return (ok, reason_code, human_message).

    Grok's prices are never rewritten — only accepted or rejected.
    Omitted stop/target stay omitted; posture bands are not a fill.
    New risk must use IBKR live last — a Grok price_hint is not that print.
    A stop pinned to this look's session low/high is the tape, not a % gate.
    """
    if strategy not in ("market_bracket", "oca", "bracket"):
        return True, STRUCTURE_OK, "n/a"

    direction = str(params.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT"):
        return True, STRUCTURE_OK, "n/a"  # schema layer will catch

    try:
        stop = float(params["stop_price"])
        target = float(params["target_price"])
    except (KeyError, TypeError, ValueError):
        return False, GEOMETRY_QUOTE_REQUIRED, "stop_price and target_price required"

    if require_live:
        try:
            live = float(quote_last) if quote_last is not None else 0.0
        except (TypeError, ValueError):
            live = 0.0
        if live <= 0:
            return (
                False,
                GEOMETRY_QUOTE_REQUIRED,
                "card needs IBKR live last — quote first",
            )
        proxy = live
    else:
        proxy = resolve_entry_proxy(strategy, params, quote_last)
    if proxy is None or proxy <= 0:
        return (
            False,
            GEOMETRY_QUOTE_REQUIRED,
            f"{strategy} requires live quote or price_hint for geometry check",
        )

    # Wrong-side stop / target vs live (or entry for bracket)
    if direction == "LONG":
        if not (stop < proxy < target):
            if stop >= proxy:
                return (
                    False,
                    GEOMETRY_STOP_WRONG_SIDE,
                    f"LONG stop {stop} must be below live/entry {proxy} "
                    f"(wrong-side stop scrapes immediately)",
                )
            return (
                False,
                GEOMETRY_TARGET_WRONG_SIDE,
                f"LONG target {target} must be above live/entry {proxy}",
            )
    else:
        if not (target < proxy < stop):
            if stop <= proxy:
                return (
                    False,
                    GEOMETRY_STOP_WRONG_SIDE,
                    f"SHORT stop {stop} must be above live/entry {proxy}",
                )
            return (
                False,
                GEOMETRY_TARGET_WRONG_SIDE,
                f"SHORT target {target} must be below live/entry {proxy}",
            )

    _ = posture  # fill bands only; never a % send reject
    if stop_pins_session(direction, stop, session):
        return True, STRUCTURE_OK, "geometry ok (session level)"
    return True, STRUCTURE_OK, "geometry ok"


def append_structure_event(event: dict[str, Any]) -> None:
    """Append one JSONL structure lesson (never raises into caller)."""
    try:
        path = _path_events()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": _utc_now(), **event}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        logger.exception("append_structure_event failed")


def recent_structure_lessons(limit: int = 5) -> list[dict[str, Any]]:
    """Prefer live-look lessons; skip suite/fixture noise (e.g. SPY @ 500)."""
    path = _path_events()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            src = str(ev.get("source") or "")
            if src == "suite":
                continue
            try:
                q = float(ev.get("quote") or 0)
            except (TypeError, ValueError):
                q = 0.0
            # Drop obvious fixture quotes from suite dry-runs that leaked in
            if q == 500.0 and str(ev.get("symbol") or "").upper() == "SPY":
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out
    except Exception:
        logger.exception("recent_structure_lessons failed")
        return []


def structure_cooldown_symbols(lessons: list[dict] | None = None) -> dict[str, str]:
    """Symbols with soft new-entry cooldown from scrape/illegal geometry."""
    cool: dict[str, str] = {}
    for ev in lessons or recent_structure_lessons(8):
        reason = str(ev.get("reason_code") or "")
        if reason in _INVENTED_PCT_REASON_CODES:
            continue
        code = reason or str(ev.get("outcome") or "")
        if code in _INVENTED_PCT_REASON_CODES:
            continue
        if code not in (
            SCRAPE_SUSPECT,
            GEOMETRY_REJECTED,
            GEOMETRY_STOP_WRONG_SIDE,
            GEOMETRY_TARGET_WRONG_SIDE,
            GEOMETRY_QUOTE_REQUIRED,
        ):
            # also accept outcome field
            if str(ev.get("outcome") or "") not in (
                SCRAPE_SUSPECT,
                GEOMETRY_REJECTED,
                "geometry_rejected",
            ):
                continue
        sym = str(ev.get("symbol") or "").upper()
        if sym and sym not in cool:
            cool[sym] = code or str(ev.get("outcome") or "cooldown")
    return cool


def _fill_sec(fill: dict) -> str:
    return str(
        fill.get("secType") or fill.get("sec_type") or fill.get("sec") or ""
    ).upper()


def _fill_is_stk(fill: dict) -> bool:
    """Stock scrape uses STK/ETF fills only — OPT/BAG on the same symbol is not a scrape."""
    sec = _fill_sec(fill)
    if sec in ("OPT", "FOP", "BAG", "CASH", "IND", "FUT"):
        return False
    if sec in ("STK", "ETF"):
        return True
    # Journal / historic fills may omit sec. Option identity is still not stock.
    if (
        fill.get("strike") is not None
        or fill.get("right")
        or fill.get("expiration")
        or fill.get("lastTradeDateOrContractMonth")
        or fill.get("local_symbol")
        or fill.get("localSymbol")
    ):
        return False
    return True


def detect_scrape_from_fills(
    fills: list[dict],
    *,
    symbol: str,
    window_s: float = _SCRAPE_SECONDS,
) -> bool:
    """True if BOT+SLD (or reverse) for symbol STK fills within window_s."""
    sym = symbol.upper()
    relevant = [
        f for f in fills or []
        if str(f.get("symbol") or "").upper() == sym and _fill_is_stk(f)
    ]
    if len(relevant) < 2:
        return False

    def _ts(f: dict) -> Optional[datetime]:
        raw = f.get("ts") or f.get("time") or f.get("timestamp")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    timed = [( _ts(f), f) for f in relevant]
    timed = [(t, f) for t, f in timed if t is not None]
    timed.sort(key=lambda x: x[0])
    for i in range(len(timed) - 1):
        t0, f0 = timed[i]
        t1, f1 = timed[i + 1]
        side0 = str(f0.get("side") or f0.get("action") or "").upper()
        side1 = str(f1.get("side") or f1.get("action") or "").upper()
        bot_sld = ("BOT" in side0 or side0 == "BUY") and ("SLD" in side1 or side1 == "SELL")
        sld_bot = ("SLD" in side0 or side0 == "SELL") and ("BOT" in side1 or side1 == "BUY")
        if (bot_sld or sld_bot) and (t1 - t0).total_seconds() <= window_s:
            return True
    return False


