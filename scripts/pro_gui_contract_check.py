"""Static Flet Pro Desktop contract check."""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRO = REPO / "abcxauto" / "pro_desktop.py"
REQUIRED = (
    "Overview", "Positions Ledger", "AI Brain", "Logs & Evolution", "Settings",
    "Apply Again", "Replay Cycle", "Grok Deep Analyze", "Export All", "Clear",
    "Pin Insight", "PANIC FLATTEN", "Raw JSON", "START AUTONOMOUS",
    "PAUSE", "FORCE TWEAK", "VALIDATE & EXECUTE",
    "Reality Pulse",
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
        lines.append("FAIL: flet import missing"); ok = False
    else:
        lines.append("PASS: flet import present")
    for label in REQUIRED:
        if label not in text:
            lines.append(f"FAIL: {label!r} missing"); ok = False
        else:
            lines.append(f"PASS: {label!r}")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pro_gui_contract.txt")
    out.write_text("\n".join(lines) + f"\nRESULT={'PASS' if ok else 'FAIL'}\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())