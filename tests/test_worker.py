"""RocketApp worker lifecycle — shipped desktop._async_worker, no Tk poll."""

import asyncio
import queue
import threading
import time

from abcxauto.desktop import RocketApp
from tests.test_rocket import FakeConnector


def _wait_worker_alive(app: RocketApp, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app.worker is not None and app.worker.is_alive():
            return
        time.sleep(0.02)
    raise AssertionError("worker thread did not start")


def _drain_cycles(ui: queue.Queue, n: int, timeout: float = 10.0) -> list[dict]:
    out: list[dict] = []
    deadline = time.time() + timeout
    while len(out) < n and time.time() < deadline:
        try:
            kind, data = ui.get(timeout=0.1)
        except queue.Empty:
            continue
        if kind == "cycle":
            out.append(data)
    return out


def test_worker_three_cycles_then_stop(monkeypatch):
    monkeypatch.setattr("abcxauto.desktop.get_config", lambda: type("C", (), {"xai_api_key": "k"})())
    tk = __import__("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        import pytest

        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    app = RocketApp(root)
    run_calls: list[int] = []

    async def fake_run_cycle(n, conn, grok, hist, prev):
        run_calls.append(n)
        return {
            "cycle": n, "pnl": float(n), "pnl_chg": 1.0, "equity": 1000.0, "strat": "hold",
            "result": {"status": "hold"}, "portfolio": "0", "risk": "COMPLIANT",
            "tweak": "none", "tweak_obj": {},
        }

    _real_sleep = asyncio.sleep

    async def fast_sleep(_t):
        await _real_sleep(0.02)

    monkeypatch.setattr("abcxauto.desktop.run_cycle", fake_run_cycle)
    monkeypatch.setattr("abcxauto.desktop.asyncio.sleep", fast_sleep)
    monkeypatch.setattr("abcxauto.desktop.get_ibkr_connector", lambda: FakeConnector())
    monkeypatch.setattr("abcxauto.desktop.GrokClient", lambda: object())

    try:
        app.start()
        _wait_worker_alive(app)
        cycles = _drain_cycles(app.ui, 3)
        assert [c["cycle"] for c in cycles] == [1, 2, 3]
        calls_before = len(run_calls)
        app.stop_loop()
        time.sleep(0.25)
        assert len(run_calls) == calls_before
    finally:
        app.shutdown_ui()
        app._invalidate_worker()
        root.destroy()


def test_worker_stop_prevents_fourth_cycle_after_sleep_gate(monkeypatch):
    monkeypatch.setattr("abcxauto.desktop.get_config", lambda: type("C", (), {"xai_api_key": "k"})())
    root = __import__("tkinter").Tk()
    root.withdraw()
    app = RocketApp(root)
    sleep_gate = threading.Event()
    release_sleep = threading.Event()
    run_calls: list[int] = []

    async def fake_run_cycle(n, conn, grok, hist, prev):
        run_calls.append(n)
        return {
            "cycle": n, "pnl": 1.0, "pnl_chg": 0.0, "equity": 1.0, "strat": "hold",
            "result": {"status": "hold"}, "portfolio": "0", "risk": "COMPLIANT",
            "tweak": "none", "tweak_obj": {},
        }

    async def gated_sleep(_t):
        sleep_gate.set()
        await asyncio.to_thread(release_sleep.wait, 5)

    monkeypatch.setattr("abcxauto.desktop.run_cycle", fake_run_cycle)
    monkeypatch.setattr("abcxauto.desktop.asyncio.sleep", gated_sleep)
    monkeypatch.setattr("abcxauto.desktop.get_ibkr_connector", lambda: FakeConnector())
    monkeypatch.setattr("abcxauto.desktop.GrokClient", lambda: object())

    try:
        app.start()
        _wait_worker_alive(app)
        assert sleep_gate.wait(timeout=5)
        assert run_calls == [1]
        app.stop_loop()
        release_sleep.set()
        time.sleep(0.3)
        assert run_calls == [1]
    finally:
        app.shutdown_ui()
        app._invalidate_worker()
        root.destroy()