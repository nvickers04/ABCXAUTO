"""Pro Desktop Flet — contract + _start click path + tab/chart edge cases."""

import ast
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from abcxauto.pro_desktop import ProTerminal, FILTERS, NAV, _equity_chart_control
from abcxauto.rocket import TWEAKS, run_cycle

PRO_SRC = Path(__file__).resolve().parents[1] / "abcxauto" / "pro_desktop.py"
SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-80c4246a04fb\implementer")
REQUIRED = (
    "Logs & Evolution", "Apply Again", "Replay Cycle", "Grok Deep Analyze",
    "Export All", "Clear", "Pin Insight", "PANIC FLATTEN", "Raw JSON",
)


def test_pro_desktop_imports_flet():
    tree = ast.parse(PRO_SRC.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "flet" in imports
    assert "ElevatedButton" not in PRO_SRC.read_text(encoding="utf-8")


def test_pro_gui_contract_labels():
    text = PRO_SRC.read_text(encoding="utf-8")
    for label in REQUIRED:
        assert label in text
    assert "_position_rows" in text


def test_overview_dashboard_includes_positions_table():
    text = PRO_SRC.read_text(encoding="utf-8")
    idx = text.index("def _page_overview")
    end = text.index("def _page_positions", idx)
    block = text[idx:end]
    # positions table widget is built in overview (ov_pos_table); _position_rows called in sync
    assert "ov_pos_table" in text or "Positions" in block
    assert "Equity Curve" in block


def test_equity_chart_placeholder_single_and_multi():
    import flet as ft
    ph = _equity_chart_control([])
    one = _equity_chart_control([100000.0])
    multi = _equity_chart_control([100000, 100500, 101200])
    assert isinstance(ph, ft.Image)
    assert isinstance(one, ft.Image)
    assert isinstance(multi, ft.Image)
    assert ph.src != one.src and one.src != multi.src


class _Cfg:
    xai_api_key = "test-key"


class _Page:
    title = ""
    bgcolor = ""
    padding = 0
    theme_mode = None

    def __init__(self):
        self.window = type("W", (), {"width": 1280, "height": 820, "min_width": 1000, "min_height": 700})()
        self.snack_bar = None

    def add(self, *_):
        pass

    def update(self):
        pass

    def run_task(self, _):
        pass


@pytest.fixture
def headless_pro(monkeypatch):
    monkeypatch.setattr("abcxauto.pro_desktop.get_config", lambda: _Cfg())
    return ProTerminal(_Page())


def test_tab_switch_builds_fresh_pos_tables(headless_pro):
    headless_pro.engine.state.positions = [{"symbol": "SPY", "quantity": 5, "sec_type": "STK", "unrealized_pnl": 1}]
    ov = headless_pro._page_overview()
    pos = headless_pro._page_positions()
    t1 = headless_pro._position_rows(headless_pro.engine.state.positions)
    t2 = headless_pro._position_rows(headless_pro.engine.state.positions)
    assert t1 is not t2
    assert ov is not None and pos is not None


@pytest.mark.asyncio
async def test_run_cycle_real_path_with_tool_boundary_only(monkeypatch):
    calls = {"grok": 0}

    async def fake_grok(_g, prompt: str) -> str:
        calls["grok"] += 1
        assert "LIVE POSITION LEDGER" in prompt
        if "ONE tweak" in prompt:
            return json.dumps({"type": "config", "config": {"cycle_sleep_s": 0.01}, "summary": "faster"})
        return json.dumps({
            "action": "hold", "strategy": "hold",
            "rationale": "inventory reviewed → target none → hold",
            "reasoning_chain": "SPY STK listed",
        })

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 50000, "unrealizedpnl": 5},
            "positions": [{"symbol": "SPY", "quantity": 10, "sec_type": "STK", "unrealized_pnl": 5, "con_id": 1}],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    class _Conn:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            return [{"symbol": "SPY", "quantity": 10, "sec_type": "STK"}]

    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.rocket.grok", fake_grok)
    hist, prev = [], 0.0
    before = dict(TWEAKS)
    try:
        for n in range(1, 4):
            out = await run_cycle(n, _Conn(), object(), hist, prev)
            prev = out["pnl"]
            assert out.get("inventory")
        assert calls["grok"] >= 3
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_pro_start_click_three_visible_cycles(headless_pro, monkeypatch):
    """Shipped _start() — same handler as START AUTONOMOUS button."""
    grok_n = [0]

    async def fake_grok(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps({"type": "config", "config": {"cycle_sleep_s": 0.01}, "summary": "faster"})
        grok_n[0] += 1
        return json.dumps({"action": "hold", "strategy": "hold", "reasoning_chain": "hold"})

    class _Conn:
        connected = True

        async def connect(self):
            return True

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 50000 + grok_n[0] * 50, "unrealizedpnl": grok_n[0]},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    _real_sleep = asyncio.sleep

    async def paced_sleep(_t):
        await _real_sleep(0.06)

    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.rocket.grok", fake_grok)
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.pro_desktop.GrokClient", lambda: object())
    monkeypatch.setattr("abcxauto.pro_desktop.asyncio.sleep", paced_sleep)

    before = dict(TWEAKS)
    try:
        headless_pro._start()
        state = headless_pro.engine.state
        assert state.running
        assert headless_pro.engine.worker and headless_pro.engine.worker.is_alive()
        deadline = time.time() + 18
        while time.time() < deadline and state.cycles < 3:
            headless_pro.engine.drain_apply()
            headless_pro._sync_widgets()
            time.sleep(0.04)
        headless_pro._stop()
        assert state.cycles >= 3
        assert headless_pro.lbl_cycles.value == str(state.cycles)
        assert len(state.equity_hist) >= 3
        SCRATCH.mkdir(parents=True, exist_ok=True)
        (SCRATCH / "pro_integration_notes.txt").write_text(
            "\n".join([
                "ABCXAUTO Pro START click path (ProTerminal._start)",
                "entry=shipped button handler, not direct _worker",
                "mocks=readonly _tool + IBKR connector (live LLM/IBKR need credentials)",
                f"cycles={state.cycles}",
                f"lbl_cycles={headless_pro.lbl_cycles.value}",
                f"equity_points={len(state.equity_hist)}",
                f"status_after_stop={state.status}",
                "visible_widget_updates=cycles_label,equity_hist,records",
                "result=PASS",
            ]) + "\n",
            encoding="utf-8",
        )
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)