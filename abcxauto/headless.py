"""Headless paper runner — no UI, no approval. Operator = Ctrl+C kill switch."""

from __future__ import annotations

import logging
import signal
import sys
import time

logger = logging.getLogger(__name__)


def run_headless() -> int:
    """Connect paper IBKR and run autonomous cycles until SIGINT/SIGTERM."""
    from abcxauto.config import get_config, setup_file_logging
    from abcxauto.pro_engine import ProEngine
    from abcxauto.self_tune import ensure_immutable_floor

    setup_file_logging()
    cfg = get_config()
    if not cfg.is_paper:
        print("Headless refuses live mode. Paper only (TWS 7497 / Gateway 4002).", flush=True)
        return 2
    if not cfg.xai_api_key:
        print("XAI_API_KEY missing — copy .env.template to .env.", flush=True)
        return 2

    ensure_immutable_floor(persist=True)
    engine = ProEngine()

    stopping = {"done": False}

    def _stop(*_a: object) -> None:
        if stopping["done"]:
            return
        stopping["done"] = True
        print("\nKill switch — stopping agent (positions stay at IBKR).", flush=True)
        try:
            engine.stop_engine()
        except Exception:
            logger.exception("headless stop failed")

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    err = engine.start()
    if err:
        print(f"Start failed: {err}", flush=True)
        return 1
    print(
        "ABCXAUTO headless paper — autonomous. "
        "Immutable floor locked. Ctrl+C is the kill switch.",
        flush=True,
    )
    try:
        while not stopping["done"]:
            worker = engine.worker
            if worker is None or not worker.is_alive():
                if engine.state.last_error:
                    print(f"Worker died: {engine.state.last_error}", flush=True)
                    return 1
                time.sleep(0.5)
                if engine.worker is None or not engine.worker.is_alive():
                    print("Worker exited.", flush=True)
                    return 0
            time.sleep(1.0)
    except KeyboardInterrupt:
        _stop()
    return 0


if __name__ == "__main__":
    sys.exit(run_headless())
