"""Pro Desktop — one-screen console contract + start path."""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest

from abcxauto.cycle import run_cycle
from abcxauto.pro_desktop import ProTerminal

PRO_SRC = Path(__file__).resolve().parents[1] / "abcxauto" / "pro_desktop.py"
SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-80c4246a04fb\implementer")
REQUIRED = (
    "Connect IBKR",
    "Disconnect IBKR",
    "Start",
    "Stop",
    "Halt",
    "Refresh book",
    "Grok stream",
    "Working orders",
    "Session fills",
    "Activity",
    "Copy stream",
    "_toggle_connect",
    "_toggle_run",
    "_toggle_halt",
    "_open_disconnect_confirm_dialog",
    "think_live",
    "lbl_ibkr_status",
    "lbl_xai_status",
    "lbl_mda_status",
    "lbl_score",
    "ABCXAUTO",
    "_on_window_event",
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


def test_pro_gui_contract_labels():
    text = PRO_SRC.read_text(encoding="utf-8")
    for label in REQUIRED:
        assert label in text
    assert "Close All Positions" not in text
    assert "Judge/Act" not in text
    assert "PANIC FLATTEN" not in text
    assert "btn.text =" in text
    assert 'btn_run.content' not in text


def test_no_validate_execute_chrome():
    text = PRO_SRC.read_text(encoding="utf-8")
    assert "VALIDATE & EXECUTE" not in text
    assert "_validate_execute" not in text


class _Cfg:
    xai_api_key = "test-key"
    model = "grok-4.6"
    trading_mode = "paper"
    ibkr_port = 7497

    @property
    def is_paper(self) -> bool:
        return True


class _Page:
    title = ""
    bgcolor = ""
    padding = 0
    theme_mode = None

    def __init__(self):
        self.window = type(
            "W", (), {"width": 1280, "height": 820, "min_width": 960, "min_height": 640}
        )()
        self.snack_bar = None
        self.overlay = []
        self.controls = []

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


def test_buttons_wire_to_engine(headless_pro):
    assert headless_pro.btn_run.on_click == headless_pro._toggle_run
    assert headless_pro.btn_connect.on_click == headless_pro._toggle_connect
    assert headless_pro.btn_halt.on_click == headless_pro._toggle_halt
    assert headless_pro.btn_refresh.on_click == headless_pro._refresh_book
    assert headless_pro.btn_copy_stream.on_click == headless_pro._copy_stream
    assert headless_pro.btn_run.text == "Start"
    assert headless_pro.btn_connect.text == "Connect IBKR"


def test_run_btn_uses_text_not_content(headless_pro):
    headless_pro.engine.state.running = True
    headless_pro.engine.state.autonomous = True
    headless_pro.engine.state.paused = False
    headless_pro._refresh_run_btn()
    assert headless_pro.btn_run.text == "Stop"
    headless_pro.engine.state.running = False
    headless_pro.engine.state.autonomous = False
    headless_pro._refresh_run_btn()
    assert headless_pro.btn_run.text == "Start"


def test_book_strip_sync(headless_pro):
    s = headless_pro.engine.state
    s.equity = 100_000.0
    s.pnl = -50.0
    s.unprotected_count = 2
    s.halted = True
    s.brain_strat = "oca"
    s.brain_rationale = "cover SPY"
    s.market_read = "unprotected STK"
    s.last_result = {"status": "blocked", "note": "hold_forbidden"}
    s.positions = [{"symbol": "SPY", "quantity": 5, "sec_type": "STK", "unrealized_pnl": 1, "conId": 1}]
    s.open_orders = [{"order_id": 9, "symbol": "SPY", "order_type": "STP", "quantity": 5, "aux_price": 490}]
    headless_pro._sync_widgets()
    assert headless_pro.lbl_equity.value == "$100,000.00"
    assert "-50.00" in (headless_pro.lbl_pnl.value or "")
    assert headless_pro.lbl_unprotected.value == "2"
    assert headless_pro.lbl_halt.value == "HALTED"
    assert "oca" in (headless_pro.lbl_last_send.value or "")
    assert headless_pro.lbl_banner.visible is True
    assert "blocked" in (headless_pro.lbl_banner.value or "").lower()
    assert (headless_pro.lbl_score.value or "").startswith("Score:")
    assert "c0" in (headless_pro.page.title or "")
    assert "unprot=2" in (headless_pro.page.title or "")
    assert "Playbook [" in (headless_pro.lbl_playbook.value or "")
    assert len(headless_pro.col_lots.controls) == 1
    assert headless_pro.lbl_lot_count.value == "1"
    assert "stk:1" in (headless_pro.lbl_mix.value or "")
    assert "1 names" in (headless_pro.lbl_mix.value or "")
    assert "STP" in (headless_pro.lbl_working_orders.value or "")
    assert "exit" in (headless_pro.lbl_working_orders.value or "")
    assert "SPY STK long 5" in (headless_pro.lbl_working_orders.value or "")
    s.tool_trace = ["book", "quote", "send"]
    s.skip_reason = "skipped_grok: ibkr_down"
    headless_pro._sync_widgets()
    assert "quote" in (headless_pro.lbl_tools.value or "")
    assert headless_pro.lbl_banner.visible is True
    assert "ibkr_down" in (headless_pro.lbl_banner.value or "")


def test_shell_tree_builds_with_book_pane(headless_pro):
    shell = headless_pro._shell()
    assert shell is not None
    body = shell.controls[-1]
    book = body.controls[-1]
    assert book.width == 440
    assert headless_pro.tab_bodies["lots"] is headless_pro.col_lots


def test_lot_rows_name_the_lot_and_put_naked_first(headless_pro):
    positions = [
        {"symbol": "IWM", "quantity": 1, "sec_type": "OPT", "right": "C", "strike": 306.0,
         "expiration": "20260821", "avgCost": 310.0, "market_price": 2.4, "conId": 7},
        {"symbol": "AAPL", "quantity": -2, "sec_type": "OPT", "right": "P", "strike": 150.0,
         "expiration": "20260821", "avgCost": 200.0, "market_price": 1.5, "conId": 8},
        {"symbol": "SPY", "quantity": 5, "sec_type": "STK", "avgCost": 500.0,
         "market_price": 505.0, "conId": 1},
    ]
    rows = headless_pro._lot_view(positions, ["SPY"])
    assert [r["ident"] for r in rows][0] == "SPY STK long 5"
    assert rows[0]["unprotected"] is True
    assert rows[1]["ident"] == "IWM 260821C306.0 long 1"
    assert rows[1]["mtm_pct"] == -23.0
    assert rows[2]["ident"] == "AAPL 260821P150.0 short 2"
    assert rows[2]["mtm_pct"] == 25.0
    headless_pro.engine.state.positions = positions
    headless_pro.engine.state.portfolio = {"unprotected_symbols": ["SPY"]}
    headless_pro._sync_lots()
    assert len(headless_pro.col_lots.controls) == 3


def test_lot_rows_empty_book_says_so(headless_pro):
    headless_pro.engine.state.positions = []
    headless_pro._sync_lots()
    assert headless_pro.col_lots.controls == [headless_pro.lbl_positions]
    assert headless_pro.lbl_positions.value == "No open positions"


def test_path_line_is_facts_not_advice(headless_pro):
    assert headless_pro._path_line({"n": 2}) == "Path: 2 closed fills — thin sample"
    line = headless_pro._path_line(
        {"n": 40, "E": 12.5, "p": 0.55, "b": 1.8, "kelly": 0.08, "f": 0.02, "ruin": 0.031}
    )
    assert "n40" in line
    assert "E$+12" in line
    assert "kelly 8.0%" in line
    assert "f 2.0%" in line
    assert "ruin 3.1%" in line


def test_edge_stat_tracks_beating_model(headless_pro):
    headless_pro._sync_edge_stat({"edge_usd": 42.0, "beating_model": True, "model_cost_usd": 1.5})
    assert headless_pro.lbl_edge.value == "$+42"
    assert "1.50" in (headless_pro.lbl_edge_sub.value or "")
    headless_pro._sync_edge_stat({})
    assert headless_pro.lbl_edge.value == "—"


def test_tabs_show_one_body_with_counts(headless_pro):
    s = headless_pro.engine.state
    s.open_orders = [{"order_id": 9, "symbol": "SPY"}]
    headless_pro._sync_tabs()
    assert headless_pro.tab_bodies["lots"].visible is True
    assert headless_pro.tab_bodies["orders"].visible is False
    assert headless_pro.tabs["orders"]["count"].value == "1"
    headless_pro._select_tab("orders")
    assert headless_pro.tab_bodies["orders"].visible is True
    assert headless_pro.tab_bodies["lots"].visible is False


def test_stream_line_widget_follows_tokens(headless_pro):
    widget = headless_pro.think_live
    headless_pro.engine.state.think_live = "alpha\nbeta\ngamma"
    headless_pro._think_sync_key = ""
    headless_pro._sync_think_stream()
    assert headless_pro.think_live is widget
    assert "alpha" in (headless_pro.think_live.value or "")
    assert "gamma" in (headless_pro.think_live.value or "")
    headless_pro.engine.state.think_live = "alpha\nbeta\ngamma\ndelta"
    headless_pro._sync_think_stream()
    assert headless_pro.think_live is widget
    assert "delta" in (headless_pro.think_live.value or "")


def test_stream_paints_tail_not_full_buffer(headless_pro):
    blob = ("head-" * 20) + ("tail-" * 400)
    headless_pro.engine.state.think_live = blob
    headless_pro._think_sync_key = ""
    headless_pro._sync_think_stream()
    shown = headless_pro.think_live.value or ""
    assert len(shown) <= 1800
    assert shown.endswith("tail-" * 4)
    assert str(len(blob)) in (headless_pro.lbl_stream_status.value or "").replace(",", "")


def test_window_close_marks_operator_stop(headless_pro, tmp_path, monkeypatch):
    from abcxauto.supervisor import operator_stopped

    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(tmp_path / "stop.json"))
    assert operator_stopped() is False
    headless_pro._on_window_event(type("E", (), {"type": "resize"})())
    assert operator_stopped() is False
    headless_pro._on_window_event(type("E", (), {"type": "close"})())
    assert operator_stopped() is True


def test_disconnected_book_does_not_score_zero_nl(headless_pro):
    headless_pro.engine.state.equity = 0.0
    headless_pro._refresh_score_line()
    headless_pro._sync_path_line()
    assert "no live book" in (headless_pro.lbl_score.value or "")
    assert headless_pro.lbl_edge.value == "—"
    assert headless_pro.lbl_path.value == "Path: —"


def test_disconnected_uses_desk_brief_lots(headless_pro, tmp_path):
    (tmp_path / "desk_brief.json").write_text(
        json.dumps({
            "strat": "hold",
            "sends": 0,
            "open_lots": ["IWM 260821C306.0 long 1 -26%", "XLF 260828C58.5 long 1 -41%"],
            "net_liquidation": 35674.48,
            "mix": {"long_c": 2, "short_c": 0, "long_p": 0, "short_p": 0, "stk": 0, "vert": 0},
            "rationale": "manage 4 DTE",
            "ts": "2026-08-17T17:26:23.837302+00:00",
        }),
        encoding="utf-8",
    )
    headless_pro._brief_last = 0.0
    headless_pro._brief_row = {}
    headless_pro._lots_key = "x"
    s = headless_pro.engine.state
    s.equity = 0.0
    s.positions = []
    s.brain_strat = "—"
    s.brain_rationale = "—"
    headless_pro._sync_widgets()
    assert headless_pro.lbl_equity.value == "$35,674.48"
    assert "IWM 260821C306.0 long 1" in headless_pro._lots_key
    assert "hold" in (headless_pro.lbl_last_send.value or "")
    assert "manage 4 DTE" in (headless_pro.lbl_why.value or "")
    assert "longC:2" in (headless_pro.lbl_mix.value or "")
    assert headless_pro.tabs["lots"]["count"].value == "2"


def test_stream_empty_waiting_copy(headless_pro):
    headless_pro.engine.state.think_live = ""
    headless_pro._think_sync_key = "stale"
    headless_pro._sync_think_stream()
    assert "waiting" in (headless_pro.think_live.value or "").lower()


def test_toast_does_not_stack_snackbars(headless_pro):
    headless_pro._toast("one")
    headless_pro._toast("two")
    snacks = [c for c in headless_pro.page.overlay if type(c).__name__ == "SnackBar"]
    assert len(snacks) == 1


@pytest.mark.asyncio
async def test_run_cycle_real_path_with_tool_boundary_only(monkeypatch):
    calls = {"grok": 0}

    async def fake_grok(_g, prompt: str, *, stage: str = "act") -> str:
        calls["grok"] += 1
        if stage == "judge" or "JUDGE STAGE" in prompt:
            return json.dumps({
                "stance": "protect",
                "thesis": "Protect naked SPY",
                "focus": "unprotected STK",
                "dismissed": "",
                "intent": {
                    "kind": "protect",
                    "symbol": "SPY",
                    "direction": "LONG",
                    "urgency": "high",
                },
            })
        return json.dumps({
            "action": "oca",
            "strategy": "oca",
            "params": {
                "symbol": "SPY",
                "quantity": 10,
                "direction": "LONG",
                "stop_price": 490.0,
                "target_price": 520.0,
                "conId": 1,
            },
            "rationale": "inventory reviewed -> protect SPY conId=1",
        })

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 50000, "unrealizedpnl": 5},
            "positions": [
                {"symbol": "SPY", "quantity": 10, "sec_type": "STK", "unrealized_pnl": 5, "con_id": 1}
            ],
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

    from types import SimpleNamespace
    from tests.conftest import grok_json_as_turn

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_json_as_turn(fake_grok))

    async def _noop_send(action, conn):
        return {"status": "executed", "strategy": action.get("strategy")}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(signal_only=False, grok_min_interval_s=0, trading_mandate=""),
    )
    hist, prev = [], 0.0
    for n in range(1, 4):
        out = await run_cycle(n, _Conn(), object(), hist, prev)
        prev = out["pnl"]
        assert out.get("inventory")
    assert calls["grok"] >= 3


def test_pro_start_click_three_visible_cycles(headless_pro, monkeypatch):
    grok_n = [0]

    async def fake_grok(_g, prompt: str, **_kwargs) -> str:
        grok_n[0] += 1
        return json.dumps({
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "quantity": 1,
                "direction": "LONG",
                "stop_price": 490.0,
                "target_price": 520.0,
                "price_hint": 500.0,
            },
            "rationale": "active cycle",
        })

    class _Conn:
        connected = True

        async def connect(self):
            return True

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {
                "netliquidation": 50000 + grok_n[0] * 50,
                "unrealizedpnl": grok_n[0],
            },
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    _real_sleep = asyncio.sleep

    async def paced_sleep(_t):
        await _real_sleep(0.06)

    from tests.conftest import grok_json_as_turn

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.pro_engine.GrokClient", lambda: object())
    monkeypatch.setattr("abcxauto.pro_engine.asyncio.sleep", paced_sleep)

    async def _fast_pace(_sleep_s, _wake, **_kw):
        await paced_sleep(0.06)
        return ""

    monkeypatch.setattr("abcxauto.pacing.wait_for_pace", _fast_pace)

    async def _no_scan(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.opportunity_scan.scan_opportunities", _no_scan)
    monkeypatch.setattr("abcxauto.opportunity_scan.fetch_scan_metrics", _no_scan)
    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _no_scan)

    class _FastCfg:
        xai_api_key = "test-key"
        cycle_sleep_s = 0.05
        grok_min_interval_s = 0.01
        pace_protect_s = 0.05
        pace_manage_s = 0.05
        pace_idle_s = 0.05
        signal_only = False
        monitor_enabled = False
        trading_mandate = ""
        risk_gates_enabled = False
        control_deliberation_pct = 50
        control_budget_pct = 50
        control_complexity_pct = 50
        control_frequency_pct = 50
        control_rotation_pct = 50
        max_open_positions = 0
        risk_posture = ""
        trading_mode = "paper"

    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _FastCfg())
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: _FastCfg())

    headless_pro._start()
    state = headless_pro.engine.state
    assert state.running
    assert headless_pro.engine.worker and headless_pro.engine.worker.is_alive()
    deadline = time.time() + 18
    while time.time() < deadline and state.cycles < 1:
        headless_pro.engine.drain_apply()
        headless_pro._sync_widgets()
        time.sleep(0.04)
    seen = state.cycles
    extra = time.time() + 0.8
    while time.time() < extra:
        headless_pro.engine.drain_apply()
        headless_pro._sync_widgets()
        time.sleep(0.04)
    headless_pro._stop()
    assert seen >= 1
    assert state.cycles == seen
    assert headless_pro.lbl_cycles.value == str(state.cycles)
    assert len(state.equity_hist) >= 1
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "pro_integration_notes.txt").write_text(
        f"cycles={state.cycles} result=PASS\n",
        encoding="utf-8",
    )
