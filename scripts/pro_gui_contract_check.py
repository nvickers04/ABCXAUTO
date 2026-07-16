"""Static Flet Pro Desktop contract check — stdout + exit code only."""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRO = REPO / "abcxauto" / "pro_desktop.py"
REQUIRED = (
    "Dashboard",
    "Positions",
    "Risk",
    "Scorecard",
    "Test Suite",
    "Close All Positions",
    "Connect IBKR",
    "Disconnect IBKR",
    "Start agent",
    "Stop agent",
    "Start",
    "Stop",
    "Re-test",
    "re-test",
    "Reality Pulse",
    "Order suite",
    "Activity",
    "Working",
    "Fills",
    "_refresh_book_tab",
    "_refresh_agent_tab",
    "_refresh_log_tab",
    "What's happening",
    "ABCXAUTO",
    "DASH_TABS",
    "suite_filter",
    "roll_option",
    "Total value",
    "lbl_ret_1w",
    "lbl_ibkr_status",
    "lbl_xai_status",
    "lbl_mda_status",
    "_toggle_connect",
    "_open_disconnect_confirm_dialog",
    "_page_risk",
    "update_risk_config",
    "lbl_account_id",
    "lbl_account_mode",
    "_sync_ibkr_account_label",
    "_toggle_trading_mode",
    "paper-only",
)


def main() -> int:
    text = PRO.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    lines, ok = [], True
    if "flet" not in imports:
        lines.append("FAIL: flet import missing")
        ok = False
    else:
        lines.append("PASS: flet import present")
    for label in REQUIRED:
        if label not in text:
            lines.append(f"FAIL: {label!r} missing")
            ok = False
        else:
            lines.append(f"PASS: {label!r}")
    lines.append(f"RESULT={'PASS' if ok else 'FAIL'}")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
