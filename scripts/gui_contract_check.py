"""Static Tkinter contract check — ast/source only, no test counts."""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DESKTOP = REPO / "abcxauto" / "desktop.py"
TITLE = "ABCXAUTO Rocket – Autonomous Portfolio"
REQUIRED = (
    "START AUTONOMOUS", "STOP", "PANIC FLATTEN ALL",
    "Last Improvement", "Apply Now", TITLE,
)


def main() -> int:
    text = DESKTOP.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    lines = []
    ok = True
    if "tkinter" not in imports:
        lines.append("FAIL: tkinter import missing"); ok = False
    else:
        lines.append("PASS: tkinter import present")
    if "customtkinter" in imports:
        lines.append("FAIL: customtkinter found"); ok = False
    if TITLE not in text:
        lines.append(f"FAIL: title {TITLE!r} missing"); ok = False
    else:
        lines.append(f"PASS: title {TITLE!r} in source")
    for label in REQUIRED:
        if label not in text:
            lines.append(f"FAIL: label {label!r} missing"); ok = False
        else:
            lines.append(f"PASS: label {label!r} present")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("gui_contract.txt")
    out.write_text("\n".join(lines) + f"\nRESULT={'PASS' if ok else 'FAIL'}\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())