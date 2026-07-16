"""Structure referee + gradebook — Grok owns prices; code rejects illegal geometry
and records lessons for the next cycle.
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
_VOCAB_PATH = _REPO_ROOT / "structure_vocab.json"
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


def _path_events() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_STRUCTURE_EVENTS_PATH", "").strip()
    return Path(raw) if raw else _EVENTS_PATH


def _path_vocab() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_STRUCTURE_VOCAB_PATH", "").strip()
    return Path(raw) if raw else _VOCAB_PATH


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


def check_live_geometry(
    strategy: str,
    params: dict[str, Any],
    *,
    quote_last: float | None = None,
    posture: str = "balanced",
) -> tuple[bool, str, str]:
    """Return (ok, reason_code, human_message).

    Grok's prices are never rewritten — only accepted or rejected.
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

    # Bracket entry must be near live quote when both present
    if strategy == "bracket" and quote_last is not None:
        try:
            entry = float(params.get("entry_price") or 0)
            q = float(quote_last)
            if entry > 0 and q > 0 and abs(entry - q) / q > 0.02:
                return (
                    False,
                    GEOMETRY_ENTRY_STALE,
                    f"bracket entry {entry} >2% from live quote {q}",
                )
        except (TypeError, ValueError):
            pass

    lo, hi = posture_stop_bands(posture)
    stop_pct = abs(proxy - stop) / proxy
    if stop_pct < lo:
        return (
            False,
            GEOMETRY_STOP_TOO_TIGHT,
            f"stop distance {stop_pct*100:.3f}% below min {lo*100:.2f}% for {posture or 'balanced'}",
        )
    if stop_pct > hi:
        return (
            False,
            GEOMETRY_STOP_TOO_WIDE,
            f"stop distance {stop_pct*100:.2f}% above max {hi*100:.2f}% for {posture or 'balanced'}",
        )

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
    """Prefer live-cycle lessons; skip suite/fixture noise (e.g. SPY @ 500)."""
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
    """Symbols with soft hunt cooldown from scrape/geometry reject."""
    cool: dict[str, str] = {}
    for ev in lessons or recent_structure_lessons(8):
        code = str(ev.get("reason_code") or ev.get("outcome") or "")
        if code not in (
            SCRAPE_SUSPECT,
            GEOMETRY_REJECTED,
            GEOMETRY_STOP_WRONG_SIDE,
            GEOMETRY_STOP_TOO_TIGHT,
            GEOMETRY_STOP_TOO_WIDE,
            GEOMETRY_ENTRY_STALE,
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


def detect_scrape_from_fills(
    fills: list[dict],
    *,
    symbol: str,
    window_s: float = _SCRAPE_SECONDS,
) -> bool:
    """True if BOT+SLD (or reverse) for symbol within window_s."""
    sym = symbol.upper()
    relevant = [
        f for f in fills or []
        if str(f.get("symbol") or "").upper() == sym
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


def save_structure_vocab(report: dict[str, Any]) -> Path:
    """Persist slim suite trainer memory from an order_suite report."""
    path = _path_vocab()
    failed = sorted(
        {
            str(r.get("strategy"))
            for r in (report.get("results") or [])
            if r.get("strategy") and not r.get("pass")
        }
    )
    passed = sorted(
        {
            str(r.get("strategy"))
            for r in (report.get("results") or [])
            if r.get("strategy") and r.get("pass")
        }
    )
    data = {
        "ts": report.get("taken_at") or _utc_now(),
        "source": report.get("source") or "suite",
        "pass_rate": report.get("pass_rate"),
        "passed": passed,
        "failed": failed,
        "failed_details": [
            {
                "strategy": r.get("strategy"),
                "detail": str(r.get("detail") or r.get("error") or "")[:200],
            }
            for r in (report.get("results") or [])
            if r.get("strategy") and not r.get("pass")
        ][:20],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def load_structure_vocab() -> dict[str, Any]:
    path = _path_vocab()
    if not path.is_file():
        # fall back to in-memory suite cache
        try:
            from abcxauto.order_suite import get_cached_suite

            cached = get_cached_suite()
            if cached:
                return {
                    "ts": cached.get("taken_at"),
                    "source": cached.get("source") or "cache",
                    "pass_rate": cached.get("pass_rate"),
                    "passed": [],
                    "failed": list(cached.get("failed_strategies") or []),
                    "failed_details": [],
                }
        except Exception:
            pass
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("load_structure_vocab failed")
        return {}


def format_structure_lessons_for_prompt(lessons: list[dict] | None = None) -> str:
    items = lessons if lessons is not None else recent_structure_lessons(5)
    if not items:
        return "STRUCTURE LESSONS: (none yet — suite + live rejects will appear here)\n"
    lines = ["STRUCTURE LESSONS (code gradebook — learn these facts):"]
    for ev in items:
        lines.append(
            f"- {ev.get('ts', '')[:19]} {ev.get('strategy')} {ev.get('symbol')} "
            f"outcome={ev.get('outcome')} code={ev.get('reason_code')} "
            f"quote={ev.get('quote')} "
            f"msg={(str(ev.get('message') or '')[:120])}"
        )
    return "\n".join(lines) + "\n"
