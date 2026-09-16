"""Flatten All on the Risk Halt group — marshalled panic, confirm, honest toast."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from abcxauto.pro_desktop import NAV, ProTerminal

_REPO = Path(__file__).resolve().parents[1]
_DESKTOP = _REPO / "abcxauto" / "desktop"
_FACADE = _REPO / "abcxauto" / "pro_desktop.py"


class _Cfg:
    xai_api_key = "test-key"
    model = "grok-4.6"
    trading_mode = "paper"
    ibkr_port = 7497
    risk_posture = "balanced"
    max_risk_per_trade_pct = 5
    daily_loss_limit_pct = 3
    max_position_pct = 10
    max_peak_drawdown_pct = 8
    max_option_premium_pct = 4
    defined_risk_only = True
    cash_only = True
    risk_gates_enabled = True

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

    def add(self, *_):
        pass

    def update(self):
        pass

    def run_task(self, _):
        pass


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


def _cockpit_text() -> str:
    paths = [_FACADE]
    if _DESKTOP.is_dir():
        paths.extend(sorted(p for p in _DESKTOP.rglob("*.py") if p.is_file()))
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def _toast_text(pro_ui) -> str:
    bars = [c for c in (pro_ui.page.overlay or []) if type(c).__name__ == "SnackBar"]
    if not bars:
        return ""
    content = getattr(bars[-1], "content", None)
    return str(getattr(content, "value", "") or "")


def _dialog(pro_ui):
    for item in reversed(pro_ui.page.overlay or []):
        if type(item).__name__ == "AlertDialog":
            return item
    return None


def test_nav_unchanged():
    keys = [k for k, _label, _o, _f in NAV]
    assert keys == [
        "overview",
        "positions",
        "notebook",
        "scorecard",
        "risk",
        "settings",
    ]


def test_risk_page_puts_flatten_with_halt_group(pro):
    blob = _row_text(pro._page_risk())
    assert "Halt" in blob
    assert "Flatten All" in blob
    assert blob.index("Halt") < blob.index("Flatten All")
    assert pro.btn_flatten.on_click == pro._open_flatten_confirm_dialog
    assert pro.btn_flatten.on_click != pro._flatten_all


def test_flatten_confirm_is_required_and_calls_panic_once(pro):
    calls: list[str] = []
    pro.engine.panic = lambda: calls.append("panic")

    pro.btn_flatten.on_click(None)
    assert calls == []
    dlg = _dialog(pro)
    assert dlg is not None
    assert dlg.open is True
    cancel, confirm = dlg.actions
    cancel.on_click(None)
    assert calls == []
    assert dlg.open is False

    pro._open_flatten_confirm_dialog()
    dlg = _dialog(pro)
    dlg.actions[1].on_click(None)
    assert calls == ["panic"]


def test_flatten_uses_marshalled_panic_not_asyncio_run():
    text = _cockpit_text()
    assert "engine.panic()" in text
    assert "asyncio.run(" not in text
    assert ".flatten_all(" not in text
    assert "_do_panic" not in text
    actions = (_DESKTOP / "actions.py").read_text(encoding="utf-8")
    assert actions.count("self.engine.panic()") == 1


def test_flatten_outcome_reads_honest_fields():
    from abcxauto.pro_desktop import AMBER, GREEN, RED

    # success=True used to lie when lots remained. Green only when the book is flat.
    stale_lie, stale_color = ProTerminal._flatten_outcome(
        {
            "success": True,
            "status": "flat",
            "remaining": [],
            "failed": [],
            "positions_closed": 2,
            "positions_total": 3,
        }
    )
    assert stale_lie == "Flattened — book is flat"
    assert stale_color == GREEN
    assert "2/3" not in stale_lie

    line, color = ProTerminal._flatten_outcome(
        {
            "success": False,
            "status": "partial",
            "remaining": [{"symbol": "SPY"}],
            "failed": [
                {
                    "symbol": "SPY",
                    "reason": "close rejected",
                    "protection": "last_stop",
                }
            ],
        }
    )
    assert line.startswith("Partial flatten")
    assert "1 still open" in line
    assert "SPY last-stop restored" in line
    assert "close rejected" in line
    assert "2/3" not in line
    assert "success" not in line.lower()
    assert color == AMBER

    working, working_color = ProTerminal._flatten_outcome(
        {
            "success": False,
            "status": "failed",
            "remaining": [{"symbol": "SPY"}],
            "failed": [
                {
                    "symbol": "SPY",
                    "reason": "close rejected",
                    "protection": "still_working",
                    "protection_order_id": 11,
                }
            ],
        }
    )
    assert "SPY last-stop still working" in working
    assert working_color == AMBER

    bare, bare_color = ProTerminal._flatten_outcome(
        {
            "success": False,
            "status": "failed",
            "remaining": [{"symbol": "AAPL"}],
            "failed": [
                {
                    "symbol": "AAPL",
                    "reason": "no last-stop price for leftover stock",
                    "protection": "none",
                }
            ],
        }
    )
    assert "AAPL unprotected" in bare
    assert "no last-stop price" in bare
    assert bare_color == AMBER

    failed, fail_color = ProTerminal._flatten_outcome(
        {"success": False, "status": "disconnected", "error": "Not connected"}
    )
    assert failed.startswith("Flatten failed")
    assert "Not connected" in failed
    assert fail_color == RED


def test_partial_flatten_toast_uses_failed_row(pro):
    pro._flatten_waiting = True
    pro.engine.state.records.append(
        {
            "type": "panic",
            "msg": (
                '{"success": false, "status": "partial", '
                '"remaining": [{"symbol": "AAPL"}], '
                '"failed": [{"symbol": "AAPL", "reason": "close rejected", '
                '"protection": "none"}]}'
            ),
        }
    )
    pro._maybe_finish_flatten_report()
    toast = _toast_text(pro)
    assert toast.startswith("Partial flatten")
    assert "1 still open" in toast
    assert "AAPL unprotected" in toast
    assert "close rejected" in toast
    assert "1/2" not in toast
    assert "success" not in toast.lower()
    assert pro._flatten_waiting is False


def test_risk_and_settings_read_facade_get_config(pro, monkeypatch):
    class _Patched:
        risk_posture = "aggressive"
        trading_mode = "paper"
        max_risk_per_trade_pct = 2.5
        daily_loss_limit_pct = 1
        max_position_pct = 6
        max_peak_drawdown_pct = 4
        max_option_premium_pct = 1.5
        defined_risk_only = True
        cash_only = True
        risk_gates_enabled = True

        @property
        def is_paper(self) -> bool:
            return True

    monkeypatch.setattr("abcxauto.pro_desktop.get_config", lambda: _Patched())
    pro._sync_risk_page(force=True)
    glance = pro.lbl_risk_glance.value or ""
    assert "aggressive" in glance
    assert "2.5" in glance
    assert pro.lbl_risk_posture.value.startswith("aggressive")


def test_desktop_get_config_imports_go_through_the_facade():
    offenders: list[str] = []
    for path in sorted(_DESKTOP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "abcxauto.config":
                continue
            names = {alias.name for alias in node.names}
            if "get_config" in names:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
