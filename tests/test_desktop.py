"""Desktop GUI contract — structural checks + apply_now behavior."""

import ast
import json

import pytest

from abcxauto.desktop import TITLE, RocketApp
from abcxauto.rocket import TWEAKS, apply_tweak

DESKTOP_SRC = __import__("pathlib").Path(__file__).resolve().parents[1] / "abcxauto" / "desktop.py"
REQUIRED = (
    "START AUTONOMOUS", "STOP", "PANIC FLATTEN ALL",
    "Last Improvement", "Apply Now", TITLE,
)


def test_desktop_uses_tkinter_only():
    tree = ast.parse(DESKTOP_SRC.read_text(encoding="utf-8"))
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
    assert "tkinter" in imports
    assert "customtkinter" not in imports


def test_desktop_contract_labels_in_source():
    text = DESKTOP_SRC.read_text(encoding="utf-8")
    for label in REQUIRED:
        assert label in text


def test_rocket_app_title_headless():
    root = __import__("tkinter").Tk()
    root.withdraw()
    try:
        app = RocketApp(root)
        assert app.root.title() == TITLE
        assert app.status == "Safe"
        assert app.cycles == 0
    finally:
        root.destroy()


def test_apply_now_reapplies_last_tweak_config():
    root = __import__("tkinter").Tk()
    root.withdraw()
    before = dict(TWEAKS)
    try:
        app = RocketApp(root)
        app.last_tweak = {"type": "config", "config": {"cycle_sleep_s": 4}, "summary": "sleep 4s"}
        app.imp_txt.insert("end", "sleep 4s")
        logs = []
        app._log = logs.append
        app.apply_now()
        assert TWEAKS.get("cycle_sleep_s") == 4
        assert any("Apply Now" in x for x in logs)
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)
        root.destroy()