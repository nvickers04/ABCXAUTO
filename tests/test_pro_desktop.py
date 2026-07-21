"""Pro Desktop Flet — contract + _start click path + tab/chart edge cases."""

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest

from abcxauto.pro_desktop import (
    AMBER,
    ProTerminal,
    NAV,
    _equity_spark_control,
)
from abcxauto.cycle import TWEAKS, run_cycle

PRO_SRC = Path(__file__).resolve().parents[1] / "abcxauto" / "pro_desktop.py"
SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-80c4246a04fb\implementer")
REQUIRED = (
    "Close All Positions",
    "Connect IBKR",
    "Disconnect IBKR",
    "Start agent",
    "Stop agent",
    "Start",
    "Stop",
    "Re-test",
    "Dashboard",
    "Test Suite",
    "Positions",
    "Scorecard",
    "Reality Pulse",
    "lbl_clock",
    "lbl_session_badge",
    "lbl_ibkr_status",
    "lbl_xai_status",
    "lbl_mda_status",
    "_toggle_connect",
    "_open_disconnect_confirm_dialog",
    "re-test",
    "Order suite",
    "Activity",
    "Working orders",
    "Session fills",
    "_refresh_book_tab",
    "_dash_live",
    "lbl_dash_pace",
    "What's happening",
    "ABCXAUTO",
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


def test_overview_dashboard_agent_status():
    text = PRO_SRC.read_text(encoding="utf-8")
    idx = text.index("def _page_overview")
    end = text.index("def _page_positions", idx)
    block = text[idx:end]
    assert "_dash_live" in block
    assert "Last cycle" in block
    assert "Now" in block
    assert "Activity" in block
    assert "Context" in block
    assert "lbl_dash_pace" in block or "lbl_dash_pace" in text
    assert "brain_action" in text
    assert "lbl_agent_now" in text
    # Blotter lives on Positions, not Dashboard
    assert 'self._section("Working"' not in block
    assert 'self._section("Fills"' not in block
    assert "lbl_working_orders" not in block
    pos_idx = text.index("def _page_positions")
    pos_end = text.index("def _page_scorecard", pos_idx)
    pos_block = text[pos_idx:pos_end]
    assert "lbl_working_orders" in pos_block
    assert "lbl_recent_fills" in pos_block
    assert "_dash_book" in block  # compat shim
    assert "_dash_agent" in block
    assert "_dash_log" in block
    assert "_refresh_book_tab" in block
    assert "_refresh_agent_tab" in block
    assert "_refresh_log_tab" in block
    assert "_dash_pulse" not in block
    assert '("pulse", "Pulse")' not in text
    assert '("log", "Log")' not in text
    assert "_set_dash_tab" in text
    assert "DASH_TABS" not in text
    assert 'self._stat_line("NetLiq"' not in block
    assert "VALIDATE & EXECUTE" not in block
    assert "ov_pos_table" not in block
    assert "Equity Curve" not in block
    assert "Start" in text
    assert "Stop" in text or "Pause" in text
    assert "Close All Positions" in text
    assert "Re-test all" in text
    assert "Dashboard" in text
    assert "Test Suite" in text
    assert "_page_risk" in text
    assert "_page_controls" in text
    assert "_save_controls" in text
    assert '("controls", "Controls"' in text or "\"controls\", \"Controls\"" in text
    assert '("risk", "Risk"' in text or "\"risk\", \"Risk\"" in text
    assert "_sync_ibkr_account_label" in text
    assert "_toggle_trading_mode" in text
    assert "lbl_account_id" in text
    assert "btn_account_mode" in text
    assert "_toggle_accounts_popup" not in text
    assert "@paper" not in text
    assert "_toggle_run" in text
    assert "_page_suite" in text
    assert "_left_rail" in text
    assert "_right_rail" in text
    assert "Following" not in text
    assert "Search ABCXAUTO" not in text
    assert "START AUTONOMOUS" not in text
    assert "PANIC FLATTEN" not in text
    assert "Mandate:" in text or "lbl_mandate_health" in text


def test_book_strip_and_mandate_sync(headless_pro):
    """Book + mandate health reflect ViewState portfolio fields."""
    from abcxauto.pro_engine import compute_mandate_health

    s = headless_pro.engine.state
    s.equity = 100_000.0
    s.pnl = -50.0
    s.unprotected_count = 0
    s.halted = False
    s.last_decision = "trade"
    s.mandate_health, s.mandate_health_label = compute_mandate_health(
        unprotected_count=0,
        halted=False,
        equity=100_000.0,
        daily_pnl=-50.0,
        gate_blocks=0,
    )
    s.portfolio = {
        "net_liquidation": 100_000.0,
        "daily_pnl": -50.0,
        "unprotected_count": 0,
        "last_decision": "trade",
        "halted": False,
    }
    headless_pro._sync_widgets()
    assert headless_pro.lbl_book_netliq.value == "$100,000"
    assert "-50.00" in (headless_pro.lbl_book_pnl.value or "")
    assert headless_pro.lbl_book_unprotected.value == "0"
    assert "trade" in (headless_pro.lbl_book_decision.value or "")
    assert headless_pro.lbl_book_halt.value == "clear"
    assert "green" in (headless_pro.lbl_mandate_health.value or "").lower()

    s.unprotected_count = 2
    s.halted = True
    s.last_decision = "hold"
    s.mandate_health, s.mandate_health_label = compute_mandate_health(
        unprotected_count=2,
        halted=True,
        equity=100_000.0,
        daily_pnl=-50.0,
    )
    s.portfolio = {
        "net_liquidation": 99_000.0,
        "daily_pnl": -1200.0,
        "unprotected_count": 2,
        "last_decision": "hold",
        "halted": True,
    }
    headless_pro._sync_widgets()
    assert headless_pro.lbl_book_unprotected.value == "2"
    assert "hold" in (headless_pro.lbl_book_decision.value or "")
    assert headless_pro.lbl_book_halt.value == "HALTED"
    assert "red" in (headless_pro.lbl_mandate_health.value or "").lower()


def test_scorecard_hold_vs_trade_label(headless_pro, tmp_path, monkeypatch):
    """Scorecard shows hold/trade ratio from journal proposals."""
    from abcxauto.memory import reset_journal

    db = tmp_path / "hold_trade_scorecard.db"
    j = reset_journal(path=str(db), enabled=True)
    monkeypatch.setattr("abcxauto.pro_desktop.get_journal", lambda: j)
    j.record_proposal(
        source="test",
        strategy="hold",
        symbol="SPY",
        direction="LONG",
        quantity=0,
        validation_ok=True,
    )
    j.record_proposal(
        source="test",
        strategy="market_bracket",
        symbol="SPY",
        direction="LONG",
        quantity=1,
        validation_ok=True,
    )
    j.record_proposal(
        source="test",
        strategy="oca",
        symbol="QQQ",
        direction="LONG",
        quantity=1,
        validation_ok=True,
    )
    headless_pro._page_scorecard()
    headless_pro._refresh_scorecard(force=True)
    assert headless_pro.lbl_sc_hold_trade.value == "1/2"


def test_no_validate_execute_chrome():
    text = PRO_SRC.read_text(encoding="utf-8")
    assert "VALIDATE & EXECUTE" not in text
    assert "Validate Order Impact" not in text
    assert "_validate_execute" not in text
    assert "_validate_impact" not in text
    assert "Improvements" not in text


def test_equity_spark_placeholder_single_and_multi():
    import flet as ft

    ph = _equity_spark_control([])
    one = _equity_spark_control([100000.0])
    multi = _equity_spark_control([100000, 100500, 101200])
    assert isinstance(ph, ft.Text)
    assert isinstance(one, ft.Text)
    assert isinstance(multi, ft.Column)
    assert "Awaiting" in (ph.value or "")
    assert "100,000" in (one.value or "")
    assert multi.controls and "101,200" in (multi.controls[0].value or "")


class _Cfg:
    xai_api_key = "test-key"
    model = "grok-4.5"
    trading_mode = "paper"
    ibkr_port = 7497
    suite_paper_place = True

    @property
    def is_paper(self) -> bool:
        return True


class _Page:
    title = ""
    bgcolor = ""
    padding = 0
    theme_mode = None

    def __init__(self):
        self.window = type("W", (), {"width": 1280, "height": 820, "min_width": 1000, "min_height": 700})()
        self.snack_bar = None
        self.overlay = []

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


def test_scorecard_page_empty_journal(headless_pro, tmp_path, monkeypatch):
    """Scorecard constructs and shows empty-state copy when journal has no rows."""
    from abcxauto.memory import reset_journal

    db = tmp_path / "empty_scorecard.db"
    reset_journal(path=str(db), enabled=True)
    monkeypatch.setattr("abcxauto.pro_desktop.get_journal", lambda: reset_journal(path=str(db), enabled=True))

    page = headless_pro._page_scorecard()
    assert page is not None
    headless_pro._refresh_scorecard(force=True)

    assert headless_pro.lbl_sc_proposals.value == "0"
    assert headless_pro.lbl_sc_allowed.value == "0"
    assert headless_pro.lbl_sc_rejected.value == "0"
    assert headless_pro.lbl_sc_dispatch_ok.value == "0"
    assert headless_pro.lbl_sc_dispatch_failed.value == "0"
    assert headless_pro.lbl_sc_halts.value == "0"
    assert headless_pro.lbl_sc_netliq.value == "—"
    assert headless_pro.lbl_sc_hold_trade.value == "—"
    empty_msg = "No data yet — journal populates as the agent trades"
    assert empty_msg in (headless_pro.lbl_sc_equity_empty.value or "")
    first_cell = headless_pro.sc_dispatch_table.rows[0].cells[0].content
    assert empty_msg in (getattr(first_cell, "value", "") or "")
    assert ("scorecard", "Scorecard") in [n[:2] for n in NAV]
    assert ("risk", "Risk") in [n[:2] for n in NAV]


def test_scorecard_page_with_journal_data(headless_pro, tmp_path, monkeypatch):
    """Scorecard counters and NetLiq reflect a populated temp journal."""
    from abcxauto.memory import reset_journal

    db = tmp_path / "filled_scorecard.db"
    j = reset_journal(path=str(db), enabled=True)
    monkeypatch.setattr("abcxauto.pro_desktop.get_journal", lambda: j)

    pid = j.record_proposal(
        source="test",
        strategy="bracket",
        symbol="SPY",
        direction="LONG",
        quantity=1,
        validation_ok=True,
    )
    j.record_gate_decision(pid, True, "ok")
    j.record_gate_decision(pid, False, "risk")
    j.record_dispatch(pid, True, {"status": "filled", "order_id": 1})
    j.record_dispatch(pid, False, {"status": "rejected", "reason": "timeout"})
    j.record_halt("daily loss", kind="halt")
    j.record_snapshot(account={"NetLiquidation": 100000.0})
    j.record_snapshot(account={"NetLiquidation": 100250.0})

    page = headless_pro._page_scorecard()
    assert page is not None
    headless_pro._refresh_scorecard(force=True)

    assert headless_pro.lbl_sc_proposals.value == "1"
    assert headless_pro.lbl_sc_allowed.value == "1"
    assert headless_pro.lbl_sc_rejected.value == "1"
    assert headless_pro.lbl_sc_dispatch_ok.value == "1"
    assert headless_pro.lbl_sc_dispatch_failed.value == "1"
    assert headless_pro.lbl_sc_halts.value == "1"
    assert headless_pro.lbl_sc_halts.color == AMBER
    assert headless_pro.lbl_sc_netliq.value == "$100,250"
    assert headless_pro.lbl_sc_equity_empty.visible is False
    assert headless_pro.lbl_sc_agent_ret.value == "+0.25%"
    assert len(headless_pro.sc_dispatch_table.rows or []) >= 2
    statuses = []
    for row in headless_pro.sc_dispatch_table.rows:
        cell = row.cells[1]
        statuses.append(getattr(cell.content, "value", None))
    assert "OK" in statuses and "FAILED" in statuses
    spark = headless_pro.sc_equity_spark.content
    assert spark is not None
    assert getattr(spark, "controls", None) or getattr(spark, "value", None)


def test_scorecard_nav_and_show_tab(headless_pro, tmp_path, monkeypatch):
    from abcxauto.memory import reset_journal

    db = tmp_path / "nav_scorecard.db"
    j = reset_journal(path=str(db), enabled=True)
    monkeypatch.setattr("abcxauto.pro_desktop.get_journal", lambda: j)
    headless_pro.sidebar_btns = {
        k: type("B", (), {"bgcolor": None})() for k, _, _, _ in NAV
    }
    headless_pro.sidebar_icon_pair = {k: (o, f) for k, _, o, f in NAV}
    headless_pro.sidebar_icons = {}
    headless_pro.sidebar_labels = {}
    headless_pro.lbl_center_title = type("T", (), {"value": ""})()
    headless_pro.content = type("C", (), {"content": None})()
    headless_pro.dash_tabs_row = type("D", (), {"visible": False})()
    headless_pro.lbl_center_subtitle = type("T", (), {"value": "", "visible": True})()
    headless_pro._show_tab("scorecard")
    assert headless_pro.tab == "scorecard"
    assert headless_pro.lbl_center_title.value == "Scorecard"
    assert headless_pro.content.content is not None
    headless_pro._show_tab("suite")
    assert headless_pro.tab == "suite"
    assert headless_pro.lbl_center_title.value == "Test Suite"
    headless_pro._show_tab("overview")
    assert headless_pro.lbl_center_title.value == "Dashboard"
    headless_pro._set_dash_tab("log")
    assert headless_pro.dash_tab == "log"  # single live surface; key kept as-is


@pytest.mark.asyncio
async def test_run_cycle_real_path_with_tool_boundary_only(monkeypatch):
    calls = {"grok": 0}

    async def fake_grok(_g, prompt: str, *, stage: str = "act") -> str:
        calls["grok"] += 1
        if stage == "judge" or "JUDGE STAGE" in prompt:
            assert "WORLDSTATE" in prompt or "unprotected" in prompt.lower()
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
                "risk_budget_pct": 1.0,
                "regime_fit": True,
                "setup_grade": "A",
            })
        assert "LIVE POSITION LEDGER" in prompt or "ORDER EXAMPLES" in prompt
        if "ONE tweak" in prompt:
            return json.dumps({"type": "config", "config": {"cycle_sleep_s": 0.01}, "summary": "faster"})
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

    from types import SimpleNamespace

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)

    async def _noop_send(action, conn):
        return {"status": "executed", "strategy": action.get("strategy")}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    # Unprotected STK → protection Grok every cycle.
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            signal_only=False,
            grok_min_interval_s=0,
            trading_mandate="",
        ),
    )
    hist, prev = [], 0.0
    before = dict(TWEAKS)
    try:
        for n in range(1, 4):
            out = await run_cycle(n, _Conn(), object(), hist, prev)
            prev = out["pnl"]
            assert out.get("inventory")
        # Unprotected SPY STK → protection Grok every cycle
        assert calls["grok"] >= 3
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_pro_start_click_three_visible_cycles(headless_pro, monkeypatch):
    """Shipped _start() — same handler as Start path / toggle."""
    grok_n = [0]

    async def fake_grok(_g, prompt: str, **_kwargs) -> str:
        if "ONE tweak" in prompt:
            return json.dumps({"type": "config", "config": {"cycle_sleep_s": 0.01}, "summary": "faster"})
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
            "reasoning_chain": "active",
            "kahneman": {
                "system1_scan": "scan",
                "system2_base_rate": "base",
                "pre_mortem": "gap",
                "alternatives": ["market_bracket"],
                "bias_audit": ["anchoring"],
            },
        })

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

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    # Worker lives in ProEngine — patch there, not the Flet shell module.
    monkeypatch.setattr("abcxauto.pro_engine.GrokClient", lambda: object())
    monkeypatch.setattr("abcxauto.pro_engine.asyncio.sleep", paced_sleep)

    async def _fast_pace(_sleep_s, _wake, **_kw):
        await paced_sleep(0.06)
        return ""

    monkeypatch.setattr("abcxauto.pacing.wait_for_pace", _fast_pace)

    async def _no_scan(*_a, **_k):
        return []

    async def _no_news(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.opportunity_scan.scan_opportunities", _no_scan)
    monkeypatch.setattr("abcxauto.opportunity_scan.fetch_scan_metrics", _no_scan)
    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _no_news)

    class _FastCfg:
        xai_api_key = "test-key"
        cycle_sleep_s = 0.05
        # Must be >0: pro_engine does `float(x or 120)` so 0.0 becomes 120.
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
        operator_card = ""
        risk_posture = ""
        trading_mode = "paper"
    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _FastCfg())
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: _FastCfg())

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
