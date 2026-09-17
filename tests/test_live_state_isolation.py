"""Live desk files must stay untouched; env seams, not setattr, are the isolation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from abcxauto.config import default_file_log_path, setup_file_logging
from abcxauto.memory.journal import get_journal
from abcxauto.memory.journal_support import _DEFAULT_DB_PATH
from abcxauto.think_stream import (
    DESK_BRIEF_PATH,
    LAST_TURN_PATH,
    RUN_PATH,
    THINK_PREV_PATH,
    THINK_SESSION_DIR,
    THINK_TAIL_PATH,
    last_turn_path,
    run_path,
    think_prev_path,
    think_session_dir,
    think_tail_path,
    write_last_turn,
)
from abcxauto.working_memory import (
    WORKING_MEMORY_PATH,
    clear_working_memory,
    working_memory_path,
)

REPO = Path(__file__).resolve().parents[1]
REPO_APP_LOG = (REPO / "logs" / "app.log").resolve()
REPO_JOURNAL = (REPO / "journal.db").resolve()
REPO_UNIVERSE = (REPO / "universe_allowlist.json").resolve()

_PATH_ENV = (
    "ABCXAUTO_JOURNAL_PATH",
    "ABCXAUTO_LOG_PATH",
    "ABCXAUTO_WORKING_MEMORY_PATH",
    "ABCXAUTO_LAST_TURN_PATH",
    "ABCXAUTO_THINK_TAIL_PATH",
    "ABCXAUTO_THINK_PREV_PATH",
    "ABCXAUTO_THINK_SESSION_DIR",
    "ABCXAUTO_RUN_PATH",
    "ABCXAUTO_DESK_BRIEF_PATH",
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def test_production_paths_unchanged_when_env_absent(monkeypatch):
    """Byte-identical defaults when the ABCXAUTO_* path env vars are empty."""
    for key in _PATH_ENV:
        monkeypatch.delenv(key, raising=False)

    state = REPO / "data" / "state"
    assert LAST_TURN_PATH == state / "last_turn.json"
    assert THINK_TAIL_PATH == state / "think_tail.txt"
    assert THINK_PREV_PATH == state / "think_prev.txt"
    assert THINK_SESSION_DIR == state / "think_session"
    assert RUN_PATH == state / "run.json"
    assert DESK_BRIEF_PATH == state / "desk_brief.json"
    assert last_turn_path() == LAST_TURN_PATH
    assert think_tail_path() == THINK_TAIL_PATH
    assert think_prev_path() == THINK_PREV_PATH
    assert think_session_dir() == THINK_SESSION_DIR
    assert run_path() == RUN_PATH

    assert WORKING_MEMORY_PATH == state / "working_memory.json"
    assert working_memory_path() == WORKING_MEMORY_PATH

    # Watchlist is gone. No env seam, no default path helper.
    import abcxauto.universe as universe

    assert not hasattr(universe, "_DEFAULT_PATH")
    assert not hasattr(universe, "_path")
    assert not hasattr(universe, "save_allowlist")

    assert Path(_DEFAULT_DB_PATH).resolve() == REPO_JOURNAL
    assert default_file_log_path() == REPO_APP_LOG


def test_env_redirects_writes_away_from_live_paths(tmp_path, monkeypatch):
    before = {
        "log": _sha256(REPO_APP_LOG),
        "journal": _sha256(REPO_JOURNAL),
        "universe": _sha256(REPO_UNIVERSE),
        "wm": _sha256(REPO / "data" / "state" / "working_memory.json"),
        "last_turn": _sha256(REPO / "data" / "state" / "last_turn.json"),
    }

    wm = tmp_path / "working_memory.json"
    last = tmp_path / "last_turn.json"
    monkeypatch.setenv("ABCXAUTO_WORKING_MEMORY_PATH", str(wm))
    monkeypatch.setenv("ABCXAUTO_LAST_TURN_PATH", str(last))
    monkeypatch.setenv("ABCXAUTO_DESK_BRIEF_PATH", str(tmp_path / "desk_brief.json"))
    monkeypatch.setenv("ABCXAUTO_LOG_PATH", str(tmp_path / "app.log"))

    # Watchlist writer is gone: this test used to save_allowlist() here.
    # The live universe_allowlist.json hash below must stay byte-identical.
    wm.write_text(json.dumps({"lines": ["parked"]}) + "\n", encoding="utf-8")
    clear_working_memory()
    write_last_turn(
        {
            "strat": "",
            "rationale": "isolation probe",
            "tool_trace": ["book"],
            "world_state": {"flat": True, "net_liquidation": 1000},
            "_failed": False,
        }
    )
    setup_file_logging()
    logging.getLogger("abcxauto.iso").critical("RISK GATE HALTED (halt): isolation")
    for handler in logging.getLogger("abcxauto").handlers:
        handler.flush()

    assert not (tmp_path / "universe_allowlist.json").is_file()
    assert not wm.is_file()
    assert last.is_file()
    assert "isolation probe" in last.read_text(encoding="utf-8")
    assert (tmp_path / "app.log").is_file()

    after = {
        "log": _sha256(REPO_APP_LOG),
        "journal": _sha256(REPO_JOURNAL),
        "universe": _sha256(REPO_UNIVERSE),
        "wm": _sha256(REPO / "data" / "state" / "working_memory.json"),
        "last_turn": _sha256(REPO / "data" / "state" / "last_turn.json"),
    }
    assert after == before


def test_setup_file_logging_without_path_cannot_open_repo_app_log(tmp_path, monkeypatch):
    dest = tmp_path / "suite-app.log"
    monkeypatch.setenv("ABCXAUTO_LOG_PATH", str(dest))
    attached = setup_file_logging()
    assert attached.resolve() == dest.resolve()
    assert attached.resolve() != REPO_APP_LOG
    logging.getLogger("abcxauto.iso").critical("AUTO-PANIC isolation")
    for handler in logging.getLogger("abcxauto").handlers:
        handler.flush()
    assert dest.is_file()
    assert "AUTO-PANIC isolation" in dest.read_text(encoding="utf-8")
    if REPO_APP_LOG.is_file():
        assert "AUTO-PANIC isolation" not in REPO_APP_LOG.read_text(
            encoding="utf-8", errors="replace"
        )


def test_get_journal_after_singleton_clear_opens_redirected_path(tmp_path, monkeypatch):
    from abcxauto.memory import journal as jmod

    dest = tmp_path / "redirected-journal.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(dest))
    jmod._journal = None
    journal = get_journal()
    assert Path(journal.path).resolve() == dest.resolve()
    assert Path(journal.path).resolve() != REPO_JOURNAL
    assert Path(journal.path).resolve() != Path(_DEFAULT_DB_PATH).resolve() or dest.resolve() == Path(journal.path).resolve()


def test_clear_working_memory_cannot_unlink_live_file(tmp_path, monkeypatch):
    decoy = tmp_path / "decoy-live" / "working_memory.json"
    decoy.parent.mkdir()
    decoy.write_text(json.dumps({"lines": ["keep-me"]}) + "\n", encoding="utf-8")
    redirected = tmp_path / "wm.json"
    redirected.write_text(json.dumps({"lines": ["tmp"]}) + "\n", encoding="utf-8")
    monkeypatch.setattr("abcxauto.working_memory.WORKING_MEMORY_PATH", decoy)
    monkeypatch.setenv("ABCXAUTO_WORKING_MEMORY_PATH", str(redirected))
    clear_working_memory()
    assert decoy.is_file()
    assert json.loads(decoy.read_text(encoding="utf-8"))["lines"] == ["keep-me"]
    assert not redirected.is_file()
