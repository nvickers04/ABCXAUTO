"""Desktop GUI contract — structural checks + apply_now behavior."""

import ast

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


def test_desktop_runs_async_worker_loop():
    text = DESKTOP_SRC.read_text(encoding="utf-8")
    assert "_async_worker" in text
    assert "run_cycle" in text
    assert "threading.Thread" in text


def test_rocket_app_title_headless(headless_app):
    assert headless_app.root.title() == TITLE
    assert headless_app.status == "Safe"
    assert headless_app.cycles == 0


def test_poll_survives_bad_payload(headless_app):
    headless_app.shutdown_ui()
    headless_app.ui.put(("cycle", {"cycle": 1}))
    headless_app._poll()
    headless_app.ui.put(("log", "after bad"))
    headless_app._poll()
    log_text = headless_app.log.get("1.0", "end")
    assert "UI ERROR" in log_text
    assert "after bad" in log_text


def test_apply_now_reapplies_last_tweak_config(headless_app):
    before = dict(TWEAKS)
    try:
        headless_app.last_tweak = {"type": "config", "config": {"cycle_sleep_s": 4}, "summary": "sleep 4s"}
        headless_app.imp_txt.insert("end", "sleep 4s")
        logs = []
        headless_app._log = logs.append
        headless_app.apply_now()
        assert TWEAKS.get("cycle_sleep_s") == 4
        assert any("Apply Now" in x for x in logs)
        assert headless_app.imp_txt.get("1.0", "end").strip().startswith("applied:")
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)