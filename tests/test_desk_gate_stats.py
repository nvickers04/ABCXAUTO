"""Cockpit surfaces for gate_rejections / fill_quality / model_spend.

Thin wiring only: the Risk and Scorecard tabs paint cached rollups. The
cockpit never touches SQL, never adds a tab, and never starts a new timer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from abcxauto.pro_desktop import NAV, PAGE_REFRESH_S, ProTerminal

_REPO = Path(__file__).resolve().parents[1]
_DESKTOP = _REPO / "abcxauto" / "desktop"
_FACADE = _REPO / "abcxauto" / "pro_desktop.py"


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
            "W", (), {"width": 1280, "height": 860, "min_width": 960, "min_height": 720}
        )()
        self.snack_bar = None
        self.overlay = []
        self.controls = []
        self.tasks: list = []

    def add(self, *_):
        pass

    def update(self):
        pass

    def run_task(self, fn, *a, **k):
        self.tasks.append(fn)


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr("abcxauto.pro_desktop.get_config", lambda: _Cfg())
    return ProTerminal(_Page())


def _row_text(control) -> str:
    bits: list[str] = []
    val = getattr(control, "value", None)
    if isinstance(val, str) and val:
        bits.append(val)
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        bits.append(_row_text(content))
    for child in getattr(control, "controls", None) or []:
        bits.append(_row_text(child))
    return " ".join(b for b in bits if b)


def _cockpit_paths() -> list[Path]:
    paths = [_FACADE]
    if _DESKTOP.is_dir():
        paths.extend(sorted(p for p in _DESKTOP.rglob("*.py") if p.is_file()))
    return paths


def _cockpit_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _cockpit_paths())


def test_nav_is_unchanged_no_stats_tab():
    keys = [k for k, _label, _o, _f in NAV]
    assert keys == [
        "overview",
        "positions",
        "notebook",
        "scorecard",
        "risk",
        "settings",
    ]


def test_cockpit_never_touches_sql():
    """Stats land through gate_stats only. No journal SQL in the UI."""
    text = _cockpit_text()
    assert "sqlite3" not in text
    assert "SELECT " not in text
    assert "INSERT " not in text
    assert "gate_decisions" not in text
    assert "conn.execute" not in text
    assert text.count("from abcxauto.gate_stats import") == 1
    sync = (_DESKTOP / "sync.py").read_text(encoding="utf-8")
    assert "gate_rejections" in sync
    assert "fill_quality" in sync
    assert "model_spend" in sync
    tree = ast.parse(sync)
    top_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "gate_rejections" not in top_imports
    assert "fill_quality" not in top_imports
    assert "model_spend" not in top_imports


def test_no_new_stats_timer():
    term = (_DESKTOP / "terminal.py").read_text(encoding="utf-8")
    assert term.count("asyncio.sleep") == 2
    assert "asyncio.sleep(0.12)" in term
    assert "asyncio.sleep(1.0)" in term
    sync = (_DESKTOP / "sync.py").read_text(encoding="utf-8")
    assert "asyncio.sleep" not in sync
    assert "PAGE_REFRESH_S" in sync
    assert PAGE_REFRESH_S == 3.0


def test_risk_page_keeps_gate_rejections_near_floor(pro):
    blob = _row_text(pro._page_risk())
    assert "Floor" in blob
    assert "Gate rejections" in blob
    assert blob.index("Floor") < blob.index("Gate rejections")
    assert "No gate rejections this session or today." in blob
    assert "session 0 · day 0" in blob
    assert "error" not in blob.lower()
    assert "Traceback" not in blob


def test_scorecard_page_puts_slip_and_spend_next_to_book(pro):
    blob = _row_text(pro._page_scorecard())
    assert "NetLiq" in blob
    assert "Edge" in blob
    assert "Fill slip" in blob
    assert "Model $" in blob
    assert blob.index("Edge") < blob.index("Fill slip") < blob.index("Model $")
    assert "no fills this session" in blob
    assert "no model spend this session" in blob
    assert "error" not in blob.lower()


def test_empty_cache_is_zero_state_not_blank(pro):
    pro._desk_stats = {}
    pro._paint_gate_rejections()
    pro._paint_fill_quality()
    pro._paint_model_spend()
    assert pro.col_risk_gates.controls
    assert "No gate rejections this session or today." in _row_text(pro.col_risk_gates)
    assert pro.lbl_risk_gates.value == "session 0 · day 0"
    assert pro.lbl_sc_slip.value == "—"
    assert pro.lbl_sc_slip_sub.value == "no fills this session"
    assert pro.lbl_sc_spend.value == "—"
    assert pro.lbl_sc_spend_sub.value == "no model spend this session"


def test_malformed_stats_are_zero_state_not_error(pro):
    pro._desk_stats = {
        "rejections": None,
        "fill_quality": "pre-migration",
        "model_spend": [],
    }
    pro._paint_gate_rejections()
    pro._paint_fill_quality()
    pro._paint_model_spend()
    blob = " ".join(
        [
            _row_text(pro.col_risk_gates),
            pro.lbl_risk_gates.value or "",
            pro.lbl_sc_slip.value or "",
            pro.lbl_sc_slip_sub.value or "",
            pro.lbl_sc_spend.value or "",
            pro.lbl_sc_spend_sub.value or "",
        ]
    )
    assert "No gate rejections this session or today." in blob
    assert pro.lbl_sc_slip.value == "—"
    assert "error" not in blob.lower()
    assert "Traceback" not in blob


def test_stats_exception_is_zero_state(pro, monkeypatch):
    def boom(**_k):
        raise RuntimeError("pre-migration journal")

    monkeypatch.setattr("abcxauto.gate_stats.gate_rejections", boom)
    monkeypatch.setattr("abcxauto.gate_stats.fill_quality", boom)
    monkeypatch.setattr("abcxauto.gate_stats.model_spend", boom)
    risk = ProTerminal._load_desk_stats("risk", None)
    card = ProTerminal._load_desk_stats("scorecard", 10_000.0)
    assert risk == {"rejections": {}}
    assert card == {"fill_quality": {}, "model_spend": {}}
    pro._desk_stats = {**risk, **card}
    pro._paint_gate_rejections()
    pro._paint_fill_quality()
    pro._paint_model_spend()
    assert "No gate rejections this session or today." in _row_text(pro.col_risk_gates)
    assert pro.lbl_sc_slip.value == "—"
    assert pro.lbl_sc_spend.value == "—"


def test_gate_rejection_rows_use_reason_count_example(pro):
    pro._desk_stats = {
        "rejections": {
            "session": {
                "total": 3,
                "by_reason": [
                    {
                        "stage": "size",
                        "reason": "above max_risk_per_trade_pct",
                        "count": 2,
                        "latest": {
                            "symbol": "MSFT",
                            "strategy": "bracket",
                            "ts": "2026-09-16T16:30:00.000Z",
                        },
                    }
                ],
            },
            "day": {"total": 4, "by_reason": []},
        }
    }
    pro._paint_gate_rejections()
    blob = _row_text(pro.col_risk_gates)
    assert "above max_risk_per_trade_pct" in blob
    assert "2" in blob
    assert "MSFT" in blob
    assert "bracket" in blob
    assert pro.lbl_risk_gates.value == "session 3 · day 4"


def test_gate_rejections_fall_back_to_day_when_session_empty(pro):
    pro._desk_stats = {
        "rejections": {
            "session": {"total": 0, "by_reason": []},
            "day": {
                "total": 1,
                "by_reason": [
                    {
                        "stage": "",
                        "reason": "kill_look: F10 spent",
                        "count": 1,
                        "latest": {"symbol": "NVDA", "strategy": "vertical_spread"},
                    }
                ],
            },
        }
    }
    pro._paint_gate_rejections()
    blob = _row_text(pro.col_risk_gates)
    assert "Session is quiet — showing today." in blob
    assert "kill_look: F10 spent" in blob
    assert "NVDA" in blob
    assert pro.lbl_risk_gates.value == "session 0 · day 1"


def test_scorecard_stats_paint_from_cache(pro):
    pro._desk_stats = {
        "fill_quality": {
            "session": {
                "aggregate": {
                    "n": 2,
                    "n_priced": 2,
                    "sum_slippage_usd": 21.0,
                    "mean_slippage_usd": 10.5,
                }
            },
            "day": {"aggregate": {"n": 2, "sum_slippage_usd": 21.0}},
        },
        "model_spend": {
            "session": {"model_cost_usd": 1.25, "model_calls": 4},
            "day": {"model_cost_usd": 2.5, "model_calls": 7},
        },
    }
    from abcxauto.pro_desktop import AMBER, TEXT

    pro._paint_fill_quality()
    pro._paint_model_spend()
    assert pro.lbl_sc_slip.value == "$+21.00"
    assert pro.lbl_sc_slip.color == AMBER
    assert "2 fills" in (pro.lbl_sc_slip_sub.value or "")
    assert "mean $+10.50" in (pro.lbl_sc_slip_sub.value or "")
    assert "day $+21.00" in (pro.lbl_sc_slip_sub.value or "")
    assert pro.lbl_sc_spend.value == "$1.25"
    assert pro.lbl_sc_spend.color == TEXT
    assert "4 calls" in (pro.lbl_sc_spend_sub.value or "")
    assert "day $2.50" in (pro.lbl_sc_spend_sub.value or "")


def test_favorable_slip_is_green_zero_spend_is_muted(pro):
    from abcxauto.pro_desktop import GREEN, MUTED

    pro._desk_stats = {
        "fill_quality": {
            "session": {
                "aggregate": {"n": 1, "sum_slippage_usd": -4.0, "mean_slippage_usd": -4.0}
            },
            "day": {"aggregate": {}},
        },
        "model_spend": {
            "session": {"model_cost_usd": 0.0, "model_calls": 0},
            "day": {"model_cost_usd": 0.0, "model_calls": 0},
        },
    }
    pro._paint_fill_quality()
    pro._paint_model_spend()
    assert pro.lbl_sc_slip.value == "$-4.00"
    assert pro.lbl_sc_slip.color == GREEN
    assert pro.lbl_sc_spend.value == "$0.00"
    assert pro.lbl_sc_spend.color == MUTED
    assert pro.lbl_sc_spend_sub.value == "no model spend this session"


def test_widget_sync_does_not_call_stats(pro, monkeypatch):
    hits: list[str] = []

    def boom(name):
        def _inner(**_k):
            hits.append(name)
            return {}

        return _inner

    monkeypatch.setattr("abcxauto.gate_stats.gate_rejections", boom("rej"))
    monkeypatch.setattr("abcxauto.gate_stats.fill_quality", boom("fill"))
    monkeypatch.setattr("abcxauto.gate_stats.model_spend", boom("spend"))
    pro.tab = "risk"
    pro._page_last = 0.0
    pro._sync_widgets()
    pro.tab = "scorecard"
    pro._page_last = 0.0
    pro._sync_widgets()
    assert hits == []


def test_schedule_uses_existing_page_task(pro):
    pro.page.tasks.clear()
    pro.tab = "risk"
    pro._desk_stats_last = 0.0
    pro._schedule_desk_stats(force=True)
    assert pro.page.tasks == [pro._refresh_desk_stats]
    pro.page.tasks.clear()
    pro.tab = "overview"
    pro._schedule_desk_stats(force=True)
    assert pro.page.tasks == []


@pytest.mark.asyncio
async def test_refresh_calls_stats_functions_only(pro, monkeypatch):
    seen: list[str] = []
    kwargs: list[dict] = []

    def rej(**_k):
        seen.append("rej")
        return {
            "session": {"total": 0, "by_reason": []},
            "day": {"total": 0, "by_reason": []},
        }

    def fill(**_k):
        seen.append("fill")
        return {"session": {"aggregate": {"n": 0}}, "day": {"aggregate": {"n": 0}}}

    def spend(**k):
        seen.append("spend")
        kwargs.append(k)
        return {"session": {"model_cost_usd": 0.0, "model_calls": 0}, "day": {}}

    monkeypatch.setattr("abcxauto.gate_stats.gate_rejections", rej)
    monkeypatch.setattr("abcxauto.gate_stats.fill_quality", fill)
    monkeypatch.setattr("abcxauto.gate_stats.model_spend", spend)
    pro.engine.state.equity = 35_000.0
    pro.tab = "risk"
    pro._desk_stats_last = 0.0
    await pro._refresh_desk_stats()
    assert seen == ["rej"]
    assert "No gate rejections this session or today." in _row_text(pro.col_risk_gates)
    pro.tab = "scorecard"
    pro._desk_stats_last = 0.0
    await pro._refresh_desk_stats()
    assert seen == ["rej", "fill", "spend"]
    assert kwargs and kwargs[0].get("equity") == 35_000.0
    assert pro.lbl_sc_slip.value == "—"
    assert pro.lbl_sc_spend.value == "$0.00"


@pytest.mark.asyncio
async def test_refresh_is_cached_for_page_interval(pro, monkeypatch):
    n = {"g": 0}

    def rej(**_k):
        n["g"] += 1
        return {"session": {"total": 0, "by_reason": []}, "day": {"total": 0, "by_reason": []}}

    monkeypatch.setattr("abcxauto.gate_stats.gate_rejections", rej)
    pro.tab = "risk"
    pro._desk_stats_last = 0.0
    await pro._refresh_desk_stats()
    await pro._refresh_desk_stats()
    assert n["g"] == 1
    pro._desk_stats_force = True
    await pro._refresh_desk_stats()
    assert n["g"] == 2
