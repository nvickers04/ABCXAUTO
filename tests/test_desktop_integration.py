"""Single integration — desktop.start → _async_worker → _poll via mainloop."""

import asyncio
import json

from abcxauto.rocket import TWEAKS
from tests.conftest import SCRATCH, mainloop_until
from tests.test_rocket import FakeConnector, _fake_tool


def test_desktop_start_poll_three_cycles(headless_app, monkeypatch):
    grok_n = [0]

    async def fake_grok(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps(
                {"type": "config", "config": {"cycle_sleep_s": 0.01}, "summary": "faster cycles"}
            )
        grok_n[0] += 1
        k2 = {
            "system1_scan": "SPY long setup",
            "system2_base_rate": "defined-risk bracket base case",
            "debias": {
                "anchoring": "ignore prior print",
                "availability": "use pulse ledger",
                "overconfidence": "p_win 0.45",
                "representativeness": "regular session",
                "loss_aversion": "stop set",
                "prospect_theory": "asymmetric exits",
            },
            "pre_mortem": "fill then reverse without protection",
            "alternatives": ["hold", "market_bracket"],
            "bias_audit": ["anchoring"],
        }
        if grok_n[0] == 1:
            return json.dumps({
                "action": "bracket", "strategy": "bracket",
                "params": {
                    "symbol": "SPY", "quantity": 1, "direction": "LONG",
                    "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
                },
                "rationale": "Current reality: integration bracket after System 2",
                "kahneman": k2,
            })
        return json.dumps({
            "action": "hold", "strategy": "hold",
            "rationale": "Current reality: hold",
            "kahneman": k2,
        })

    _real_sleep = asyncio.sleep

    async def paced_sleep(_t):
        await _real_sleep(0.15)

    class _BracketConn(FakeConnector):
        async def place_bracket_order(self, **kwargs):
            return {"status": "logged", "kwargs": kwargs}

    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.rocket.grok", fake_grok)
    monkeypatch.setattr("abcxauto.desktop.get_ibkr_connector", _BracketConn)
    monkeypatch.setattr("abcxauto.desktop.GrokClient", lambda: object())
    monkeypatch.setattr("abcxauto.desktop.asyncio.sleep", paced_sleep)

    before = dict(TWEAKS)
    try:
        headless_app.start()
        assert headless_app.worker is not None and headless_app.worker.is_alive()
        snap: dict = {}

        def ready() -> bool:
            log = headless_app.log.get("1.0", "end")
            if headless_app.cycles == 3 and "cycle=3" in log and "improvement:" in log:
                snap.update(
                    log=log, cycles=headless_app.cycles,
                    imp=headless_app.imp_txt.get("1.0", "end").strip(),
                    status=headless_app.status_var.get(), title=headless_app.root.title(),
                )
                headless_app.stop_loop()
                headless_app.shutdown_ui()
                return True
            return False

        assert mainloop_until(headless_app, ready, timeout=15.0)
        log_text = snap["log"]
        assert "action=bracket" in log_text
        assert "cycle=1" in log_text and "cycle=3" in log_text
        SCRATCH.mkdir(parents=True, exist_ok=True)
        (SCRATCH / "manual_cockpit_notes.txt").write_text(
            "\n".join([
                "ABCXAUTO Desktop v0.1 cockpit evidence",
                "MANUAL: python -m abcxauto → click START AUTONOMOUS → observe ≥3 cycles + improvement",
                f"window_title={snap['title']}",
                f"cycles_widget={snap['cycles']}",
                f"improvement_widget={snap['imp']!r}",
                f"log_tail={log_text.strip()[-500:]}",
                "result=PASS",
            ]) + "\n",
            encoding="utf-8",
        )
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)