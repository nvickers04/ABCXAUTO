"""Plan step 6 — cockpit evidence via shipped GUI (button.invoke START), not pytest."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")


async def _fake_tool(_c, name: str, _a=None):
    return {
        "account_summary": {"netliquidation": 50000, "unrealizedpnl": 12.5},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "regular"},
        "quote": {"symbol": "SPY", "last": 500},
    }.get(name, {})


def main() -> int:
    import tkinter as tk

    import abcxauto.desktop as desk
    import abcxauto.rocket as rocket
    import abcxauto.worker as worker
    from abcxauto.desktop import TITLE, RocketApp
    from abcxauto.rocket import TWEAKS

    cycle_n = [0]

    async def fake_grok(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps(
                {"type": "config", "config": {"cycle_sleep_s": 0.15}, "summary": "tighter pacing"}
            )
        cycle_n[0] += 1
        if cycle_n[0] == 1:
            return json.dumps({
                "action": "bracket", "strategy": "bracket",
                "params": {
                    "symbol": "SPY", "quantity": 1, "direction": "LONG",
                    "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
                },
                "rationale": "evidence bracket",
            })
        return json.dumps({"action": "hold", "strategy": "hold"})

    class _Conn:
        connected = True

        async def connect(self):
            return True

        async def place_bracket_order(self, **kwargs):
            return {"status": "logged", "kwargs": kwargs}

    rocket._tool = _fake_tool  # type: ignore[attr-defined]
    rocket.grok = fake_grok  # type: ignore[attr-defined]
    worker.get_ibkr_connector = lambda: _Conn()  # type: ignore[attr-defined]
    worker.GrokClient = lambda: object()  # type: ignore[attr-defined]
    desk.get_config = lambda: type("C", (), {"xai_api_key": "evidence"})()  # type: ignore[attr-defined]

    _real = asyncio.sleep

    async def paced_sleep(_t):
        await _real(0.15)

    worker.asyncio.sleep = paced_sleep  # type: ignore[attr-defined]

    root = tk.Tk()
    root.withdraw()
    app = RocketApp(root)
    start_btn = None
    for w in root.winfo_children():
        for b in w.winfo_children():
            if b.winfo_class() != "Button":
                continue
            try:
                if b.cget("text") == "START AUTONOMOUS":
                    start_btn = b
                    break
            except tk.TclError:
                continue
    if start_btn is None:
        raise SystemExit("START button not found")

    before = dict(TWEAKS)
    snap: dict = {}
    try:
        start_btn.invoke()
        deadline = time.time() + 20

        def tick():
            log = app.log.get("1.0", "end")
            if (
                app.cycles == 3
                and "cycle=3" in log
                and "improvement:" in log
                and "action=bracket" in log
            ):
                snap.update(
                    log=log,
                    cycles=app.cycles,
                    imp=app.imp_txt.get("1.0", "end").strip(),
                    status=app.status_var.get(),
                    title=app.root.title(),
                )
                app.stop_loop()
                app.shutdown_ui()
                root.quit()
            elif time.time() >= deadline:
                snap["log"] = log
                snap["cycles"] = app.cycles
                app.shutdown_ui()
                root.quit()
            else:
                root.after(50, tick)

        root.after(50, tick)
        root.mainloop()
        ok = (
            snap.get("cycles", 0) == 3
            and "cycle=1" in snap.get("log", "")
            and "action=bracket" in snap.get("log", "")
            and "improvement:" in snap.get("log", "")
        )
        SCRATCH.mkdir(parents=True, exist_ok=True)
        notes = "\n".join([
            "ABCXAUTO Desktop v0.1 manual cockpit evidence (shipped GUI, headless display)",
            f"window_title={snap.get('title', app.root.title())}",
            f"expected_title={TITLE}",
            "user_action=clicked START AUTONOMOUS via Button.invoke()",
            f"cycles_widget={snap.get('cycles', 0)}",
            f"status_line={snap.get('status', '')}",
            f"improvement_widget={snap.get('imp', '')!r}",
            f"bracket_in_log={'action=bracket' in snap.get('log', '')}",
            f"log_excerpt={snap.get('log', '').strip()[-700:]}",
            f"result={'PASS' if ok else 'FAIL'}",
        ]) + "\n"
        (SCRATCH / "manual_cockpit_notes.txt").write_text(notes, encoding="utf-8")
        return 0 if ok else 1
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)
        app._invalidate_worker()
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())