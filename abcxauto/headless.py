"""Headless paper runner — no UI, no approval. Operator = Ctrl+C kill switch.

Prints every cycle so you can see what Grok sent and why.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

from abcxauto.think_stream import ascii_text

logger = logging.getLogger(__name__)

_SKIP_TYPES = frozenset({"monitor_snapshot", "ibkr_account", "trading_mode", "conn"})


def _one_line(text: Any, n: int = 240) -> str:
    t = " ".join(str(text or "").split())
    if len(t) > n:
        return t[: n - 3] + "..."
    return t


def format_cycle_digest(d: dict[str, Any]) -> str:
    """Human look block: send, why, result, next sleep."""
    j = d.get("judgment") or {}
    strat = str(d.get("strat") or d.get("action") or "-")
    stance = str(d.get("stance") or j.get("stance") or "").strip()
    thesis = _one_line(d.get("thesis") or j.get("thesis") or "", 220)
    why = _one_line(
        d.get("rationale")
        or d.get("market_read")
        or j.get("focus")
        or d.get("validation")
        or "",
        280,
    )
    result = d.get("result") if isinstance(d.get("result"), dict) else {}
    status = _one_line(
        result.get("status") or result.get("note") or d.get("validation") or "",
        160,
    )
    err = _one_line(d.get("stage_error") or "", 200)
    if "grok_error" in why or "judge_error" in why or "UNAVAILABLE" in why or "AioRpcError" in why:
        why = "grok_error: Grok API connection dropped"
        if not status:
            status = "blocked"
    nl = d.get("equity")
    pnl = d.get("pnl")
    book = ""
    try:
        if nl is not None:
            book = f"  NL={float(nl):.0f}"
        if pnl is not None:
            book += f" dPnL={float(pnl):+.2f}"
    except (TypeError, ValueError):
        book = ""
    pace = d.get("pace") if isinstance(d.get("pace"), dict) else {}
    sleep = pace.get("sleep_s")
    tier = pace.get("tier") or pace.get("reason") or ""
    lead = f"{stance} -> " if stance else ""
    lines = [f"{lead}{strat}{book}".strip()]
    if thesis:
        lines.append(f"  thesis: {thesis}")
    if why:
        lines.append(f"  why: {why}")
    if err:
        lines.append(f"  error: {err}")
    if status:
        lines.append(f"  result: {status}")
    if sleep is not None:
        try:
            lines.append(f"  next: sleep {float(sleep):.0f}s ({tier})")
        except (TypeError, ValueError):
            pass
    return "\n".join(lines)


def format_record(rec: dict[str, Any]) -> str | None:
    """One operator-visible line/block from an engine record. None = skip noise."""
    kind = str(rec.get("type") or "").lower()
    if kind in _SKIP_TYPES:
        return None
    ts = rec.get("ts") or ""
    prefix = f"[{ts}] " if ts else ""
    if kind == "cycle":
        return prefix + format_cycle_digest(rec)
    msg = _one_line(rec.get("msg") or rec, 320)
    if not msg:
        return None
    if kind in ("error", "err"):
        low = msg.lower()
        if any(
            w in low
            for w in ("started", "linked", "refreshed", "monitor started")
        ) and not any(w in low for w in ("fail", "error", "died", "timeout")):
            return f"{prefix}LOG {msg}"
        return f"{prefix}ERROR {msg}"
    if kind == "log":
        return f"{prefix}LOG {msg}"
    if kind == "pace":
        return f"{prefix}PACE {msg}"
    return f"{prefix}{kind.upper()} {msg}"


def _drain_and_print(engine: Any, seen: int) -> int:
    engine.drain_apply()
    recs = list(engine.state.records or [])
    for rec in recs[seen:]:
        if not isinstance(rec, dict):
            continue
        line = format_record(rec)
        if line:
            print(ascii_text(line), flush=True)
    return len(recs)


def _quiet_ibkr_scanner_noise() -> None:
    """ib_insync prints Error 162 scanner cancels to the console; drop those only."""

    class _Drop162(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "Error 162" in msg:
                return False
            if "scanner subscription cancelled" in msg.lower():
                return False
            return True

    for name in ("ib_insync", "ib_insync.wrapper", "ib_insync.client"):
        logging.getLogger(name).addFilter(_Drop162())


def apply_kill_switch(engine: Any) -> None:
    """Stop the agent and the IBKR link. Positions stay at the broker.

    Never flatten, panic, or send. ``stop_engine`` is the only teardown.
    """
    print("\nKill switch — stopping agent (positions stay at IBKR).", flush=True)
    stop = getattr(engine, "stop_engine", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception:
        logger.exception("headless stop failed")


def run_headless() -> int:
    """Connect paper IBKR and stay up until SIGINT/SIGTERM."""
    from abcxauto.config import get_config, setup_file_logging
    from abcxauto.pro_engine import ProEngine
    from abcxauto.self_tune import ensure_immutable_floor
    from abcxauto.think_stream import stdout_printer, subscribe

    setup_file_logging()
    _quiet_ibkr_scanner_noise()
    subscribe(stdout_printer)
    cfg = get_config()
    if not cfg.is_paper:
        print("Headless refuses live mode. Paper only (TWS 7497 / Gateway 4002).", flush=True)
        return 2
    if not cfg.xai_api_key:
        print("XAI_API_KEY missing — copy .env.template to .env.", flush=True)
        return 2

    ensure_immutable_floor(persist=True)
    try:
        from abcxauto.memory import get_journal

        get_journal().ensure_model_session(str(getattr(cfg, "model", "") or ""))
    except Exception:
        pass
    engine = ProEngine()

    stopping = {"done": False}

    def _stop(*_a: object) -> None:
        if stopping["done"]:
            return
        stopping["done"] = True
        apply_kill_switch(engine)

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    err = engine.start()
    if err:
        print(f"Start failed: {err}", flush=True)
        return 1
    print(
        "ABCXAUTO headless paper lab - Grok owns the book, "
        "scorecard is book return vs model cost. Ctrl+C is the kill switch.",
        flush=True,
    )
    seen = 0
    last_print = time.monotonic()
    think_len = 0
    try:
        while not stopping["done"]:
            worker = engine.worker
            if worker is None or not worker.is_alive():
                seen = _drain_and_print(engine, seen)
                if engine.state.last_error:
                    print(f"Worker died: {ascii_text(engine.state.last_error)}", flush=True)
                    return 1
                time.sleep(0.5)
                if engine.worker is None or not engine.worker.is_alive():
                    print("Worker exited.", flush=True)
                    return 0
            before = seen
            seen = _drain_and_print(engine, seen)
            live_n = len(getattr(engine.state, "think_live", "") or "")
            if seen > before or live_n != think_len:
                last_print = time.monotonic()
                think_len = live_n
            elif time.monotonic() - last_print >= 90:
                st = engine.state
                pace = st.pace or {}
                sleep = pace.get("sleep_s")
                wait = f" next~{float(sleep):.0f}s" if sleep else ""
                last = ascii_text(f"{st.stance or '-'}->{st.brain_strat or '-'}")
                print(
                    ascii_text(
                        f"waiting{wait}  connected={st.connected}  "
                        f"last={last}  cycles={st.cycles}"
                    ),
                    flush=True,
                )
                last_print = time.monotonic()
            time.sleep(0.5)
    except KeyboardInterrupt:
        _stop()
    return 0


if __name__ == "__main__":
    sys.exit(run_headless())
