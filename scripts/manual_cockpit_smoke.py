"""Headless cockpit smoke — documents START → 3 cycles + improvement (plan step 6)."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abcxauto.desktop import TITLE, RocketApp
from abcxauto.rocket import TWEAKS

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")


async def _fake_tool(_c, name: str, _a=None):
    return {
        "account_summary": {"netliquidation": 50000, "unrealizedpnl": 12.5},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "regular"},
        "quote": {"symbol": "SPY", "last": 500},
    }.get(name, {})


async def _fake_grok(_g, prompt: str) -> str:
    if "ONE tweak" in prompt:
        return json.dumps(
            {"type": "config", "config": {"cycle_sleep_s": 0.05}, "summary": "reduce cycle sleep"}
        )
    return json.dumps({"action": "hold", "strategy": "hold", "rationale": "paper safe"})


class _Conn:
    connected = True

    async def connect(self):
        return True


def main() -> int:
    import abcxauto.desktop as desk
    import abcxauto.rocket as rocket

    rocket._tool = _fake_tool  # type: ignore[attr-defined]
    rocket.grok = _fake_grok  # type: ignore[attr-defined]
    desk.get_ibkr_connector = lambda: _Conn()  # type: ignore[attr-defined]
    desk.GrokClient = lambda: object()  # type: ignore[attr-defined]
    desk.get_config = lambda: type("C", (), {"xai_api_key": "smoke"})()  # type: ignore[attr-defined]

    _real = asyncio.sleep

    async def fast_sleep(t):
        await _real(min(float(t), 0.05))

    desk.asyncio.sleep = fast_sleep  # type: ignore[attr-defined]

    root = __import__("tkinter").Tk()
    root.withdraw()
    app = RocketApp(root)
    before = dict(TWEAKS)
    notes: list[str] = [
        "ABCXAUTO Desktop v0.1 manual cockpit smoke (headless)",
        f"window_title={app.root.title()}",
        f"expected_title={TITLE}",
        "action=click START AUTONOMOUS (programmatic app.start())",
    ]
    try:
        app.start()
        deadline = __import__("time").time() + 12

        def done():
            log = app.log.get("1.0", "end")
            return app.cycles >= 3 and "improvement:" in log

        def tick():
            if done() or __import__("time").time() >= deadline:
                app.shutdown_ui()
                root.quit()
            else:
                root.after(50, tick)

        root.after(50, tick)
        root.mainloop()
        log_text = app.log.get("1.0", "end")
        imp = app.imp_txt.get("1.0", "end").strip()
        notes.extend(
            [
                f"cycles_completed={app.cycles}",
                f"status_line={app.status_var.get()}",
                f"improvement_box={imp!r}",
                f"log_excerpt={log_text[-800:]}",
                f"observed_cycle_lines={sum(1 for line in log_text.splitlines() if line.startswith('cycle='))}",
                f"observed_improvement={'improvement:' in log_text}",
            ]
        )
        ok = app.cycles >= 3 and "improvement:" in log_text and bool(imp)
        notes.append(f"result={'PASS' if ok else 'FAIL'}")
        SCRATCH.mkdir(parents=True, exist_ok=True)
        (SCRATCH / "manual_cockpit_notes.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
        return 0 if ok else 1
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)
        app._invalidate_worker()
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())