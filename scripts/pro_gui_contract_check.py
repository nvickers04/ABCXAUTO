"""Static Flet Pro Desktop contract check — stdout + exit code only."""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRO = REPO / "abcxauto" / "pro_desktop.py"
REQUIRED = (
    "ABCXAUTO",
    "Grok stream",
    "Connect IBKR",
    "Disconnect IBKR",
    "Start",
    "Stop",
    "Halt",
    "Refresh book",
    "_toggle_connect",
    "_toggle_run",
    "_toggle_halt",
    "_open_disconnect_confirm_dialog",
    "_toggle_trading_mode",
    "think_live",
    "lbl_ibkr_status",
    "lbl_xai_status",
    "lbl_mda_status",
    "Working orders",
    "Session fills",
    "Activity",
    "Copy stream",
    "lbl_link",
    "lbl_tools",
    "lbl_playbook",
    "lbl_score",
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
    banned = ("Close All Positions", "Judge/Act", "Judgment", "PANIC FLATTEN")
    for label in banned:
        if label in text:
            lines.append(f"FAIL: banned {label!r} present")
            ok = False
        else:
            lines.append(f"PASS: no {label!r}")
    lines.append(f"RESULT={'PASS' if ok else 'FAIL'}")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
