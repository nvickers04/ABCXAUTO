"""Pro Desktop — one-screen console contract + start path."""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest

from abcxauto.agent_loop import run_cycle
from abcxauto.pro_desktop import ProTerminal

PRO_SRC = Path(__file__).resolve().parents[1] / "abcxauto" / "pro_desktop.py"


def _nav():
    from abcxauto.pro_desktop import NAV

    return NAV


def _walk(control):
    yield control
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from _walk(content)
    for child in getattr(control, "controls", None) or []:
        yield from _walk(child)


def _visible_walk(control):
    if getattr(control, "visible", True) is False:
        return
    yield control
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from _visible_walk(content)
    for child in getattr(control, "controls", None) or []:
        yield from _visible_walk(child)

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
    "lbl_session_score",
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
    assert headless_pro.btn_run.content == "Stop"
    headless_pro.engine.state.running = False
    headless_pro.engine.state.autonomous = False
    headless_pro._refresh_run_btn()
    assert headless_pro.btn_run.text == "Start"


def test_playbook_line_paints_run_next(headless_pro):
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "instructions": "stay flat when the card gate is off",
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "when_on": "mega/large ≥6% earnings-miss gap",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            },
        }
    )
    assert update is not None
    save_lab(update)
    headless_pro.engine.state.flat = True
    headless_pro._sync_widgets()
    line = headless_pro.lbl_playbook.value or ""
    assert "Playbook [" in line
    assert "next=" not in line
    assert "send SYM" not in line
    assert "Nlooks" not in line
    assert "unused=" not in line


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
    assert "last-stop" in (headless_pro.lbl_alert.value or "")
    assert headless_pro.lbl_open_upnl.value != ""
    assert "oca" in (headless_pro.lbl_last_send.value or "")
    assert headless_pro.lbl_banner.visible is True
    assert "blocked" in (headless_pro.lbl_banner.value or "").lower()
    assert (headless_pro.lbl_score.value or "").startswith("Score:")
    from tests.conftest import assert_no_cycle_counter

    assert_no_cycle_counter(headless_pro.page.title or "")
    assert (headless_pro.page.title or "") == "ABCXAUTO"
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


def test_shell_tree_builds_three_columns(headless_pro):
    from abcxauto.pro_desktop import ASIDE_W, RAIL_W

    shell = headless_pro._shell()
    assert shell is not None
    rail, center, aside = shell.controls
    assert (rail.width, aside.width) == (RAIL_W, ASIDE_W)
    # The feed absorbs spare width so maximizing does not leave dead gutters.
    assert center.width is None
    assert center.expand
    assert headless_pro.tab_bodies["lots"] is headless_pro.col_lots


def test_nav_covers_every_surface(headless_pro):
    from abcxauto.pro_desktop import NAV_TITLES

    keys = [k for k, _label, _o, _f in _nav()]
    assert keys == [
        "overview",
        "positions",
        "notebook",
        "scorecard",
        "risk",
        "settings",
    ]
    assert set(keys) == set(NAV_TITLES)
    headless_pro.page.add(headless_pro._shell())
    for key in keys:
        headless_pro._show_tab(key)
        assert headless_pro.tab == key
        assert headless_pro.lbl_center_title.value == NAV_TITLES[key]
        assert headless_pro.content.content is not None
    headless_pro._show_tab("overview")
    assert headless_pro.lbl_center_title.value == "Dashboard"


def test_surfaces_are_reachable_as_tabs(headless_pro):
    """The operator asked for tabs, so every surface has one and it selects."""
    headless_pro.page.add(headless_pro._shell())
    keys = [k for k, _label, _o, _f in _nav()]
    assert list(headless_pro.surface_tabs) == keys
    for key in keys:
        chip = headless_pro.surface_tabs[key]["chip"]
        assert callable(chip.on_click)
        chip.on_click(None)
        assert headless_pro.tab == key
        assert headless_pro.surface_tabs[key]["chip"].bgcolor is not None
        others = [k for k in keys if k != key]
        assert all(headless_pro.surface_tabs[k]["chip"].bgcolor is None for k in others)


def test_navigation_is_tabs_only_no_second_rail_nav(headless_pro):
    """Two navigations doing one job is worse than either alone."""
    text = PRO_SRC.read_text(encoding="utf-8")
    assert "_nav_btn" not in text
    assert "sidebar_btns" not in text
    rail = headless_pro._left_rail()
    labels = {
        getattr(node, "value", None)
        for node in _walk(rail)
        if isinstance(getattr(node, "value", None), str)
    }
    for _key, label, _o, _f in _nav():
        assert label not in labels
    # What the rail is actually for: the action pills and the account block.
    controls = list(_walk(rail))
    for btn in (
        headless_pro.btn_connect,
        headless_pro.btn_run,
        headless_pro.btn_halt,
        headless_pro.btn_refresh,
    ):
        assert btn in controls
    assert headless_pro.lbl_account_id in controls
    assert headless_pro.btn_account_mode in controls
    assert headless_pro.btn_floors in controls


def test_positions_rows_replace_text_blobs(headless_pro):
    s = headless_pro.engine.state
    s.open_orders = [
        {"order_id": 9, "symbol": "SPY", "order_type": "STP", "quantity": 5, "aux_price": 490}
    ]
    s.positions = [
        {"symbol": "SPY", "quantity": 5, "sec_type": "STK", "avgCost": 500.0, "conId": 1}
    ]
    s.recent_fills = [{"symbol": "SPY", "side": "BOT", "quantity": 5, "price": 500.0}]
    s.records = [{"type": "cycle", "ts": "2026-08-20T14:00:00", "strat": "oca",
                  "result": {"status": "ok"}}]
    headless_pro._sync_widgets()
    # Each blotter tab paints one control per record, not a single text blob.
    assert headless_pro.col_orders.controls != [headless_pro.lbl_working_orders]
    assert headless_pro.col_fills.controls != [headless_pro.lbl_recent_fills]
    assert headless_pro.col_activity.controls != [headless_pro.lbl_activity]
    # Empty states fall back to the label.
    s.open_orders, s.recent_fills, s.records = [], [], []
    headless_pro._sync_widgets()
    assert headless_pro.col_orders.controls == [headless_pro.lbl_working_orders]
    assert headless_pro.col_fills.controls == [headless_pro.lbl_recent_fills]
    assert headless_pro.col_activity.controls == [headless_pro.lbl_activity]


def test_lot_rows_name_the_lot_and_put_naked_first(headless_pro):
    positions = [
        {"symbol": "IWM", "quantity": 1, "sec_type": "OPT", "right": "C", "strike": 306.0,
         "expiration": "20260821", "avgCost": 310.0, "market_price": 2.4, "conId": 7},
        {"symbol": "AAPL", "quantity": -2, "sec_type": "OPT", "right": "P", "strike": 150.0,
         "expiration": "20260821", "avgCost": 200.0, "market_price": 1.5, "conId": 8},
        {"symbol": "SPY", "quantity": 5, "sec_type": "STK", "avgCost": 500.0,
         "market_price": 505.0, "conId": 1},
    ]
    rows = headless_pro._lot_view(positions, ["SPY", "IWM 260821C306.0 long 1"])
    assert [r["ident"] for r in rows][0] in ("SPY STK long 5", "IWM 260821C306.0 long 1")
    naked = [r for r in rows if r["unprotected"]]
    assert {r["ident"] for r in naked} == {"SPY STK long 5", "IWM 260821C306.0 long 1"}
    assert rows[0]["unprotected"] is True
    by_ident = {r["ident"]: r for r in rows}
    assert by_ident["IWM 260821C306.0 long 1"]["mtm_pct"] == -23.0
    assert by_ident["AAPL 260821P150.0 short 2"]["mtm_pct"] == 25.0
    assert by_ident["AAPL 260821P150.0 short 2"]["unprotected"] is False
    headless_pro.engine.state.positions = positions
    headless_pro.engine.state.portfolio = {"unprotected_symbols": ["SPY"]}
    headless_pro._sync_lots()
    assert len(headless_pro.col_lots.controls) == 3


def test_pro_desk_operator_paint_omits_cycle(headless_pro):
    from tests.conftest import assert_no_cycle_counter

    s = headless_pro.engine.state
    s.cycles = 7
    s.running = True
    s.autonomous = True
    s.paused = False
    s.status = "Grok"
    s.unprotected_count = 0
    s.think_live = "boot — Grok.\nWake Grok.\n"
    headless_pro._sync_widgets()
    assert_no_cycle_counter(headless_pro.page.title or "")
    assert_no_cycle_counter(headless_pro.lbl_desk_sub.value or "")
    assert_no_cycle_counter(headless_pro.think_live.value or "")
    assert headless_pro.lbl_desk_sub.value == "looking"
    assert (headless_pro.page.title or "") == "ABCXAUTO"
    assert "wakes" not in (headless_pro.page.title or "").lower()
    src = PRO_SRC.read_text(encoding="utf-8")
    assert "lbl_cycles" not in src
    assert 'ft.Text("wakes"' not in src
    assert "c{s.cycles}" not in src
    s.records = [
        {
            "type": "cycle",
            "cycle": 7,
            "ts": "2026-08-19T14:00:00",
            "strat": "hold",
            "result": {"status": "ok"},
        }
    ]
    headless_pro.lbl_activity.value = headless_pro._cycle_log_text(s.records)
    assert_no_cycle_counter(headless_pro.lbl_activity.value or "")
    assert "hold" in (headless_pro.lbl_activity.value or "")


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


def test_window_close_kills_descendant_flet(headless_pro, monkeypatch):
    """Closing the window must not leave an orphan flet.exe."""
    calls: list[str] = []
    monkeypatch.setattr(
        "abcxauto.supervisor.kill_descendant_flet",
        lambda **_k: calls.append("flet") or [],
    )
    monkeypatch.delenv("ABCXAUTO_UI_PROBE", raising=False)
    headless_pro._on_window_event(type("E", (), {"type": "close"})())
    assert calls == ["flet"]


def test_window_close_marks_operator_stop(headless_pro, tmp_path, monkeypatch):
    from abcxauto.supervisor import operator_stopped

    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(tmp_path / "stop.json"))
    assert operator_stopped() is False
    headless_pro._on_window_event(type("E", (), {"type": "resize"})())
    assert operator_stopped() is False
    headless_pro._on_window_event(type("E", (), {"type": "close"})())
    assert operator_stopped() is True


def test_probe_window_close_does_not_latch_the_desk(headless_pro, tmp_path, monkeypatch):
    """A UI preview must not leave a stop that blocks the supervisor's next launch."""
    from abcxauto.supervisor import clear_operator_stop, operator_stopped

    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(tmp_path / "stop.json"))
    monkeypatch.setenv("ABCXAUTO_UI_PROBE", str(tmp_path / "probe.json"))
    clear_operator_stop()
    headless_pro._on_window_event(type("E", (), {"type": "close"})())
    assert operator_stopped() is False


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
        lambda: SimpleNamespace(trading_mode="paper"),
    )
    hist, prev = [], 0.0
    for n in range(1, 4):
        out = await run_cycle(n, _Conn(), object(), hist, prev)
        prev = out["pnl"]
        assert out.get("inventory")
    assert calls["grok"] >= 3


def test_notebook_viewer_reads_lab_not_think(headless_pro, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.lab_playbook.load_lab",
        lambda: {
            "revision": 3,
            "mode": "explore",
            "instructions": "look at options, not a nap",
        },
    )
    head, body = headless_pro._lab_notebook()
    assert "rev=3" in head
    assert "notebook, not law" in head
    assert "look at options" in body
    # Notebook is a nav surface now, not a dialog.
    assert "notebook" in dict((k, v) for k, v, _o, _f in _nav())
    assert headless_pro._hidden_metrics.visible is False
    assert headless_pro.lbl_path in headless_pro._hidden_metrics.controls
    assert headless_pro.lbl_tools in headless_pro._hidden_metrics.controls
    assert "look at options" not in (headless_pro.think_live.value or "")


def test_notebook_paints_nested_lab_cards(headless_pro):
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "instructions": "stay flat when the card gate is off",
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "when_on": "mega/large ≥6% earnings-miss gap",
                            "scan": "most_active + top_losers; mega/large only",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            },
        }
    )
    assert update is not None
    save_lab(update)
    headless_pro.engine.state.flat = True
    headless_pro._sync_notebook_page(force=True)
    text = " ".join(
        str(getattr(ctrl, "value", "") or "")
        for ctrl in _walk(headless_pro.col_notebook_cards)
    )
    assert "flush bounce" in text
    assert "next=scan" not in text
    assert "next=" not in text
    assert "No setup cards yet" not in text
    assert "≥6%" in text or "6%" in text


def test_risk_settings_surface_hidden_metrics_stay_hidden(headless_pro, monkeypatch):
    calls = []

    def _fake_update(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("abcxauto.config.update_risk_config", _fake_update)
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type(
            "C",
            (),
            {
                "risk_posture": "aggressive",
                "trading_mode": "live",
                "max_risk_per_trade_pct": 12.5,
                "daily_loss_limit_pct": 25.0,
                "max_position_pct": 25.0,
                "max_peak_drawdown_pct": 25.0,
                "max_option_premium_pct": 25.0,
                "defined_risk_only": True,
                "cash_only": True,
                "risk_gates_enabled": True,
            },
        )(),
    )
    monkeypatch.setattr("abcxauto.config.load_risk_settings", lambda: {})
    monkeypatch.setattr(
        "abcxauto.config.resolve_effective_posture",
        lambda p, m="paper": "balanced" if p == "aggressive" and m == "live" else p,
    )
    lines = " ".join(headless_pro._risk_settings_lines())
    assert "aggressive → balanced" in lines
    assert "trade 12.5%" in lines
    assert "gates on" in lines
    # Risk is a nav surface now, not a dialog.
    assert "risk" in dict((k, v) for k, v, _o, _f in _nav())
    assert headless_pro._hidden_metrics.visible is False
    assert headless_pro.lbl_path in headless_pro._hidden_metrics.controls
    assert headless_pro.lbl_playbook in headless_pro._hidden_metrics.controls
    assert headless_pro.lbl_tools in headless_pro._hidden_metrics.controls
    headless_pro._set_risk_posture("balanced")
    assert calls and calls[0].get("risk_posture") == "balanced"
    assert calls[0].get("persist") is True
    headless_pro.fields["max_risk_per_trade_pct"].value = "8"
    headless_pro._apply_field("max_risk_per_trade_pct")
    assert any(c.get("max_risk_per_trade_pct") == 8.0 and c.get("persist") is True for c in calls)


@pytest.fixture
def real_cfg_pro(headless_pro, monkeypatch):
    """Pro wired to the real config module (conftest isolates the settings file)."""
    from abcxauto.config import get_config

    monkeypatch.setattr("abcxauto.pro_desktop.get_config", get_config)
    return headless_pro


def test_settings_fields_cover_every_operator_knob(headless_pro):
    from abcxauto.pro_desktop import AGENT_FIELD_KEYS, FLOOR_GATES, RISK_FIELDS

    assert AGENT_FIELD_KEYS == {
        "model",
        "temperature",
        "max_tokens",
        "monitor_poll_s",
        "monitor_review_s",
        "disconnect_halt_s",
        "ibkr_host",
        "ibkr_client_id",
    }
    # Every settings knob is a real field, every floor gate a real switch.
    for key in AGENT_FIELD_KEYS:
        assert key in headless_pro.fields
    for key, _label in FLOOR_GATES:
        assert key in headless_pro.gates
    for key, _label, _hint in RISK_FIELDS:
        assert key in headless_pro.fields
    assert "trading_mode" not in headless_pro.fields
    assert "ibkr_port" not in headless_pro.fields
    assert "live_confirm" not in headless_pro.fields
    # self_tune owns the scan cap; agent_state would overwrite an operator edit.
    assert "scan_fetch_cap" not in headless_pro.fields


def test_settings_apply_persists_and_reports_clamp(real_cfg_pro):
    from abcxauto.config import get_config

    real_cfg_pro.fields["monitor_poll_s"].value = "45"
    real_cfg_pro._apply_field("monitor_poll_s")
    assert get_config().monitor_poll_s == 45
    assert "monitor_poll_s" in (real_cfg_pro.lbl_settings_status.value or "")

    real_cfg_pro.fields["temperature"].value = "9"
    real_cfg_pro._apply_field("temperature")
    assert get_config().temperature == 2.0
    assert "clamped" in (real_cfg_pro.lbl_settings_status.value or "")


def test_settings_refuses_a_blank_model_without_crashing(real_cfg_pro):
    from abcxauto.config import get_config

    before = get_config().model
    real_cfg_pro.fields["model"].value = "   "
    real_cfg_pro._apply_field("model")
    assert get_config().model == before
    assert "refused" in (real_cfg_pro.lbl_settings_status.value or "")


def test_open_edit_survives_the_page_repaint(real_cfg_pro):
    real_cfg_pro._sync_settings_page(force=True)
    typed = real_cfg_pro.fields["monitor_review_s"]
    typed.value = "12"
    real_cfg_pro._dirty.add("monitor_review_s")
    real_cfg_pro._sync_settings_page(force=True)
    assert typed.value == "12"
    # Leaving the page abandons the edit.
    real_cfg_pro._show_tab("overview")
    assert real_cfg_pro._dirty == set()


def test_risk_knobs_are_editable_and_tighten_only(real_cfg_pro):
    from abcxauto.config import get_config

    real_cfg_pro.fields["daily_loss_limit_pct"].value = "3"
    real_cfg_pro._apply_field("daily_loss_limit_pct")
    assert get_config().daily_loss_limit_pct == 3.0

    # Above the walk-away ceiling: clamped, and the operator is told.
    real_cfg_pro.fields["daily_loss_limit_pct"].value = "99"
    real_cfg_pro._apply_field("daily_loss_limit_pct")
    assert get_config().daily_loss_limit_pct == 25.0
    assert "clamped" in (real_cfg_pro.lbl_risk_status.value or "")

    real_cfg_pro.fields["max_open_positions"].value = "6"
    real_cfg_pro._apply_field("max_open_positions")
    assert get_config().max_open_positions == 6


def test_floor_gate_cannot_be_disarmed_from_the_ui(real_cfg_pro):
    from abcxauto.config import get_config

    gate = real_cfg_pro.gates["defined_risk_only"]
    gate.value = False
    real_cfg_pro._toggle_floor_gate("defined_risk_only")
    assert gate.value is True
    assert get_config().defined_risk_only is True
    assert "floor" in (real_cfg_pro.lbl_risk_status.value or "").lower()


def test_paper_can_turn_risk_gates_off(real_cfg_pro):
    from abcxauto.config import get_config

    assert get_config().is_paper is True
    gate = real_cfg_pro.gates["risk_gates_enabled"]
    gate.value = False
    real_cfg_pro._toggle_floor_gate("risk_gates_enabled")
    assert get_config().risk_gates_enabled is False
    assert gate.value is False


def test_the_stream_dominates_its_surface(headless_pro):
    """It was buried under two stacked sections and the operator could not read it."""
    from abcxauto.pro_desktop import STREAM_FONT_SIZE

    page = headless_pro._page_overview()
    painted = [c for c in _visible_walk(page) if c is not page]
    # Exactly one thing on the surface grows: the stream.
    growers = [
        c
        for c in painted
        if getattr(c, "expand", None)
        and (getattr(c, "content", None) is not None or getattr(c, "controls", None))
    ]
    assert headless_pro.think_scroll in growers
    assert all(headless_pro.think_scroll in list(_walk(c)) for c in growers)
    # Nothing else may claim a fixed band of the surface.
    assert not [c for c in painted if getattr(c, "height", None)]
    assert STREAM_FONT_SIZE >= 14
    assert headless_pro.think_live.size == STREAM_FONT_SIZE
    # The controls the operator uses on the stream stay.
    assert headless_pro.btn_copy_stream in painted
    assert headless_pro.lbl_stream_status in painted
    assert headless_pro.btn_stream_follow in painted


def test_the_status_strip_is_a_bar_not_a_hero_panel(headless_pro):
    """74px cards with 18px values ate the top of the Dashboard."""
    strip = headless_pro._status_strip()
    painted = list(_visible_walk(strip))
    # Small type only, and no card boxes reserving height.
    sizes = [
        s for s in (getattr(c, "size", None) for c in painted) if isinstance(s, (int, float))
    ]
    assert sizes and max(sizes) <= 13
    assert not [c for c in painted if getattr(c, "height", None)]
    # Kept: what would change what the operator does right now.
    for kept in (
        headless_pro.lbl_desk,
        headless_pro.lbl_halt,
        headless_pro.lbl_lot_count,
        headless_pro.lbl_unprotected,
        headless_pro.lbl_last_send,
        headless_pro.lbl_result,
        headless_pro.col_book_strip,
    ):
        assert kept in painted
    # Cut: repeats of the rail pill. Open MTM lives on the Account card.
    for cut in (
        headless_pro.lbl_status,
        headless_pro.lbl_pace,
    ):
        assert cut not in painted
        assert cut in list(_walk(headless_pro._hidden_metrics))
    assert headless_pro.lbl_open_upnl not in painted


def test_cut_metrics_still_sync_so_nothing_goes_stale(headless_pro):
    """A hidden label that stops updating is a trap for whoever re-mounts it."""
    s = headless_pro.engine.state
    s.positions = [
        {"symbol": "SPY", "quantity": 5, "sec_type": "STK", "unrealized_pnl": 12.0, "conId": 1}
    ]
    s.status = "Grok"
    headless_pro._sync_widgets()
    assert headless_pro.lbl_status.value == "Grok"
    assert headless_pro.lbl_open_upnl.value not in ("", "—")
    assert headless_pro._hidden_metrics.visible is False


class _RecordingScroll:
    """Stands in for the stream pane's Column and records invoke_method calls."""

    def __init__(self) -> None:
        self.auto_scroll = True
        self.calls: list[dict] = []

    async def scroll_to(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_stream_pane_pins_the_tail_without_an_invoke_method(headless_pro):
    """A tab swap unregisters the pane; an in-flight scroll_to then kills the
    flet receive loop with 'Control with ID N is not registered'."""
    assert headless_pro.think_scroll.auto_scroll is True

    rec = _RecordingScroll()
    headless_pro.think_scroll = rec
    headless_pro.tab = "risk"
    headless_pro.engine.state.think_live = "grok is thinking about SPY"
    headless_pro._sync_think_stream()

    async def _tick() -> None:
        task = asyncio.ensure_future(headless_pro._poll_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_tick())
    assert rec.calls == []
    headless_pro.tab = "overview"
    asyncio.run(_tick())
    assert rec.calls == []


def test_no_invoke_method_calls_on_page_only_controls():
    """Page swapping can unmount any page control, so these stay out of the file."""
    text = PRO_SRC.read_text(encoding="utf-8")
    for call in (".scroll_to(", ".focus(", "invoke_method"):
        offenders = [
            line.strip()
            for line in text.splitlines()
            if call in line and not line.strip().startswith("#")
        ]
        assert offenders == [], offenders
    assert "_think_need_scroll" not in text


def test_window_close_always_lets_go_of_the_window(headless_pro, tmp_path, monkeypatch):
    """prevent_close holds the window open, so a missed destroy traps the operator."""
    from abcxauto.supervisor import clear_operator_stop, operator_stopped

    text = PRO_SRC.read_text(encoding="utf-8")
    assert "prevent_close = True" in text

    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(tmp_path / "stop.json"))
    monkeypatch.delenv("ABCXAUTO_UI_PROBE", raising=False)
    clear_operator_stop()
    seen: list[str] = []
    monkeypatch.setattr(headless_pro, "_destroy_window", lambda: seen.append("gone"))

    headless_pro._on_window_event(type("E", (), {"type": "close"})())
    assert seen == ["gone"]
    assert operator_stopped() is True

    def _boom() -> None:
        raise RuntimeError("stop file unwritable")

    monkeypatch.setattr("abcxauto.supervisor.mark_operator_stop", _boom)
    headless_pro._on_window_event(type("E", (), {"type": "close"})())
    assert seen == ["gone", "gone"]


def test_destroy_window_hands_the_coroutine_to_the_page_loop(headless_pro):
    """flet window destroy/close are coroutines; calling them bare is a no-op."""
    tasks: list[object] = []
    headless_pro.page.run_task = lambda fn, *a, **k: tasks.append(fn)

    async def _destroy() -> None:
        return None

    headless_pro.page.window.destroy = _destroy
    headless_pro._destroy_window()
    assert tasks == [_destroy]


def test_size_floors_switch_is_a_two_way_paper_toggle(real_cfg_pro):
    """The Risk page owns the floors switch; it must not just describe the chip."""
    from abcxauto.config import get_config
    from abcxauto.risk_gates import sizing_floors_active

    real_cfg_pro._sync_risk_page(force=True)
    before = bool(real_cfg_pro.sw_size_floors.value)
    assert before is bool(sizing_floors_active(get_config()))
    assert "rail chip" not in (real_cfg_pro.lbl_risk_floors.value or "")

    real_cfg_pro._toggle_sizing_floors()
    assert bool(sizing_floors_active(get_config())) is (not before)
    assert bool(real_cfg_pro.sw_size_floors.value) is (not before)

    real_cfg_pro._toggle_sizing_floors()
    assert bool(sizing_floors_active(get_config())) is before
    assert bool(real_cfg_pro.sw_size_floors.value) is before


def test_field_hint_ellipsizes_instead_of_clipping_mid_word(headless_pro):
    """A hint longer than its column reads as '…', and the full text is a tooltip."""
    import flet as ft

    row = headless_pro._field_row("temperature", "Temperature", "a hint that is far too long")
    hint = row.content.controls[1]
    assert hint.tooltip == "a hint that is far too long"
    assert hint.content.overflow == ft.TextOverflow.ELLIPSIS


def test_settings_never_offers_a_live_switch_of_its_own():
    text = PRO_SRC.read_text(encoding="utf-8")
    # Mode changes go through the confirm-phrase dialog only.
    assert text.count("switch_trading_mode") == 2
    assert "_open_live_confirm_dialog" in text
    assert "set_trading_mode" not in text


def test_lot_row_follows_live_marks_not_rounded_zero(headless_pro):
    row = {
        "ident": "SPY STK long 11",
        "qty": 11,
        "avg": 769.591,
        "mkt": 765.778,
        "mtm_pct": 0.0,
        "unprotected": False,
    }
    pct = headless_pro._lot_mark_pct(row)
    assert pct is not None and pct < -0.4
    ctrl = headless_pro._lot_control(row)
    pct_lbl = ctrl.content.controls[2].content
    assert "-0.50%" in (pct_lbl.value or "") or "-0.49%" in (pct_lbl.value or "")
    assert pct_lbl.color == "#f4212e"
    assert headless_pro.lbl_session_score in headless_pro._hidden_metrics.controls
    assert headless_pro._hidden_metrics.visible is False
    assert headless_pro.lbl_path in headless_pro._hidden_metrics.controls
    assert headless_pro.lbl_tools in headless_pro._hidden_metrics.controls


def test_status_strip_shows_thinking_and_book_money(headless_pro):
    s = headless_pro.engine.state
    s.running = True
    s.autonomous = True
    s.paused = False
    s.status = "Thinking"
    s.equity = 35298.08
    s.pnl = -36.85
    s.positions = [
        {
            "symbol": "SPY",
            "quantity": 11,
            "sec_type": "STK",
            "avgCost": 769.591,
            "market_price": 765.778,
            "unrealized_pnl": -41.95,
            "conId": 1,
        }
    ]
    s.sends_last_look = 0
    s.looks_since_send = 4
    s.brain_strat = ""
    headless_pro._sync_widgets()
    assert headless_pro.lbl_hs_state.value == "looking"
    assert "looking" in (headless_pro.lbl_desk_sub.value or "").lower()
    assert headless_pro.lbl_open_upnl not in headless_pro._hidden_metrics.controls
    assert "-41.95" in (headless_pro.lbl_open_upnl.value or "")
    assert headless_pro.lbl_open_upnl.color == "#f4212e"
    assert "4 look" not in (headless_pro.lbl_last_send.value or "")
    assert "look(s) since a ticket" not in (headless_pro.lbl_last_send.value or "")
    assert headless_pro.lbl_path in headless_pro._hidden_metrics.controls
    assert headless_pro.lbl_tools in headless_pro._hidden_metrics.controls


def _news_text(pro) -> str:
    bits: list[str] = []
    for ctrl in _walk(pro.news_list):
        val = getattr(ctrl, "value", None)
        if val:
            bits.append(str(val))
    return " ".join(bits)


def test_clock_paints_chicago_ct(headless_pro):
    from abcxauto.reality_pulse import build_reality_pulse

    pulse = build_reality_pulse(
        market_hours={"session": "regular", "is_trading_day": True, "minutes_to_close": 80}
    )
    headless_pro._apply_clock(pulse)
    clock = headless_pro.lbl_clock.value or ""
    assert clock.endswith(" CT")
    assert "EDT" not in clock
    assert "EST" not in clock


def test_flat_book_sync_paints_think_scan_headlines(headless_pro):
    """What's happening must not stay 'No headlines yet' when the think fetched scan names."""
    s = headless_pro.engine.state
    s.positions = []
    s.world_state = {"open_lots": [], "flat": True}
    s.scan_fetched = ["INTU", "FIG"]
    s.scan_hits = {"rows": [{"symbol": "INTU"}, {"symbol": "FIG"}]}
    s.news_items = [
        {"symbol": "INTU", "headline": "Intuit beats estimates"},
        {"symbol": "FIG", "headline": "Figma gap tape"},
    ]
    headless_pro._sync_widgets()
    text = _news_text(headless_pro)
    assert "Intuit beats estimates" in text
    assert "Figma gap tape" in text
    assert "No headlines yet" not in text


def test_flat_book_does_not_invent_headlines(headless_pro):
    s = headless_pro.engine.state
    s.positions = []
    s.scan_fetched = ["INTU"]
    s.news_items = []
    headless_pro._news_cache = []
    headless_pro._render_news_list([])
    text = _news_text(headless_pro)
    assert "No headlines yet" in text
    assert "Intuit" not in text


@pytest.mark.asyncio
async def test_flat_book_news_rail_fetches_scan_names(headless_pro, monkeypatch):
    seen: dict[str, list] = {}

    async def _fake_fetch(positions, **_kw):
        seen["pos"] = list(positions or [])
        return [{"symbol": "INTU", "headline": "Intuit beats estimates"}]

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _fake_fetch)
    s = headless_pro.engine.state
    s.positions = []
    s.world_state = {"open_lots": []}
    s.scan_fetched = ["INTU", "FIG"]
    s.scan_hits = {"rows": [{"symbol": "INTU"}, {"symbol": "FIG"}]}
    s.news_items = []
    await headless_pro._refresh_news(force=True)
    names = [str((p or {}).get("symbol") or "").upper() for p in seen.get("pos") or []]
    assert names[:2] == ["INTU", "FIG"]
    assert "Intuit beats estimates" in _news_text(headless_pro)
    assert "No headlines yet" not in _news_text(headless_pro)


@pytest.mark.asyncio
async def test_flat_book_news_rail_uses_think_items_when_fetch_empty(
    headless_pro, monkeypatch
):
    async def _fake_fetch(positions, **_kw):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _fake_fetch)
    s = headless_pro.engine.state
    s.positions = []
    s.scan_fetched = ["INTU", "FIG"]
    s.news_items = [{"symbol": "FIG", "headline": "Figma gap tape"}]
    await headless_pro._refresh_news(force=True)
    assert "Figma gap tape" in _news_text(headless_pro)
    assert "No headlines yet" not in _news_text(headless_pro)


@pytest.mark.asyncio
async def test_open_book_news_rail_stays_on_positions(headless_pro, monkeypatch):
    seen: dict[str, list] = {}

    async def _fake_fetch(positions, **_kw):
        seen["pos"] = list(positions or [])
        return [{"symbol": "AAPL", "headline": "Apple print"}]

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _fake_fetch)
    s = headless_pro.engine.state
    s.positions = [{"symbol": "AAPL", "quantity": 1, "sec_type": "STK"}]
    s.scan_fetched = ["INTU", "FIG"]
    s.news_items = [{"symbol": "INTU", "headline": "Intuit beats estimates"}]
    await headless_pro._refresh_news(force=True)
    names = [str((p or {}).get("symbol") or "").upper() for p in seen.get("pos") or []]
    assert names == ["AAPL"]
    assert "INTU" not in names
    assert "Apple print" in _news_text(headless_pro)


@pytest.mark.asyncio
async def test_news_rail_timeout_keeps_the_print_already_on_the_rail(
    headless_pro, monkeypatch
):
    """A 2s MDA miss must not wipe HPQ Q3 from What's happening."""
    from abcxauto.news_feed import remember_headlines, reset_news_cache

    reset_news_cache()
    remember_headlines(
        [{"symbol": "HPQ", "headline": "HPQ Q3 earnings miss", "source": "mda"}]
    )

    async def _timeout_fetch(_positions, **_kw):
        return [
            {
                "symbol": "HPQ",
                "headline": "(unavailable - timed out)",
                "error": "timed out",
            }
        ]

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _timeout_fetch)
    s = headless_pro.engine.state
    s.positions = [{"symbol": "HPQ", "quantity": 1, "sec_type": "STK"}]
    s.news_items = []
    headless_pro._news_last_fetch = 0.0
    await headless_pro._refresh_news(force=True)
    text = _news_text(headless_pro)
    assert "HPQ Q3 earnings miss" in text
    assert "unavailable" not in text
    assert "No headlines yet" not in text
    reset_news_cache()

