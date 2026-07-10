"""Pro desktop — import/contract + cycle UI apply without Flet window."""

import ast
from pathlib import Path
from unittest.mock import MagicMock

from abcxauto.rocket import TWEAKS
from abcxauto.ui import TITLE, ProTerminal, write_launch_probe

UI_ROOT = Path(__file__).resolve().parents[1] / "abcxauto" / "ui"
REQUIRED = (
    "START AUTONOMOUS",
    "STOP",
    "PANIC FLATTEN",
    "AI Brain",
    "Equity Curve",
    "Open Positions",
    "ABCXAUTO Pro",
)


def _ui_sources() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(UI_ROOT.glob("*.py"))
    )


def test_pro_desktop_imports_flet():
    tree = ast.parse((UI_ROOT / "terminal.py").read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, ast.Import) else [])
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "flet" in imports
    assert "tkinter" not in imports


def test_pro_desktop_contract_labels():
    text = _ui_sources()
    for label in REQUIRED:
        assert label in text


def test_pro_desktop_uses_run_cycle():
    text = _ui_sources()
    assert "run_cycle" in text
    assert "positions" in text
    assert "protection" in text
    assert "unprotected" in text


def test_write_launch_probe(tmp_path):
    path = tmp_path / "probe.txt"
    write_launch_probe(path)
    text = path.read_text(encoding="utf-8")
    assert f"title={TITLE}" in text
    assert "mainloop_ready=True" in text


def test_on_cycle_updates_situational_awareness(monkeypatch, sample_config):
    monkeypatch.setattr("abcxauto.ui.terminal.get_config", lambda: sample_config)
    page = MagicMock()
    page.overlay = []
    term = ProTerminal(page)
    before = dict(TWEAKS)
    try:
        term._on_cycle({
            "cycle": 2,
            "pnl": 12.5,
            "pnl_chg": 1.5,
            "equity": 50100.0,
            "strat": "bracket",
            "result": {"status": "executed"},
            "tweak": "faster",
            "tweak_obj": {"type": "config", "config": {"cycle_sleep_s": 3}, "summary": "faster"},
            "risk": "UNPROTECTED: AAPL",
            "portfolio": "1 positions | 0 orders",
            "positions": [{"symbol": "AAPL", "quantity": 10, "unrealized_pnl": 5.0, "sec_type": "STK"}],
            "open_orders": [],
            "protection": {
                "positions": [{"symbol": "AAPL", "protected": False}],
                "unprotected_symbols": ["AAPL"],
            },
            "unprotected": ["AAPL"],
            "action_obj": {"action": "bracket", "strategy": "bracket", "rationale": "buy dip"},
            "rationale": "buy dip",
            "taken_at": "2026-07-08T00:00:00+00:00",
        })
        assert term.cycles == 2
        assert term.equity == 50100.0
        assert term.lbl_risk.value == "UNPROTECTED: AAPL"
        assert term.lbl_unprotected.value == "AAPL"
        assert term.brain_action.value == "bracket"
        assert term.brain_rationale.value == "buy dip"
        assert term.lbl_last_decision.value == "bracket"
        assert len(term.equity_hist) == 1
        assert len(term.pos_table.rows) == 1
        assert term.tweaks and term.tweaks[-1]["summary"] == "faster"
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)
