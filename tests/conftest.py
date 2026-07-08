"""Shared desktop test helpers — mainloop-driven poll, stable Tk teardown."""

import time
from pathlib import Path

import pytest

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")


class _Cfg:
    xai_api_key = "test-key"


def mainloop_until(app, predicate, timeout: float = 8.0) -> bool:
    """Wait for predicate using tk mainloop so after(100, _poll) drives updates."""
    box = {"ok": False}
    deadline = time.time() + timeout

    def tick():
        if predicate():
            box["ok"] = True
            app.shutdown_ui()
            app.root.quit()
        elif time.time() >= deadline:
            app.shutdown_ui()
            app.root.quit()
        else:
            app.root.after(25, tick)

    app.root.after(25, tick)
    app.root.mainloop()
    return box["ok"]


@pytest.fixture
def headless_app(monkeypatch):
    monkeypatch.setattr("abcxauto.desktop.get_config", lambda: _Cfg())
    tk = __import__("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    from abcxauto.desktop import RocketApp

    app = RocketApp(root)
    yield app
    app.shutdown_ui()
    app._invalidate_worker()
    try:
        root.update_idletasks()
        root.destroy()
    except tk.TclError:
        pass