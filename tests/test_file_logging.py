"""setup_file_logging writes a repo-absolute logs/app.log, once, INFO+."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from abcxauto.config import default_file_log_path, setup_file_logging

REPO_APP_LOG = (Path(__file__).resolve().parents[1] / "logs" / "app.log").resolve()


def _rotating_handlers_for(target: Path) -> list[RotatingFileHandler]:
    want = target.resolve()
    found: list[RotatingFileHandler] = []
    for handler in logging.getLogger("abcxauto").handlers:
        if not isinstance(handler, RotatingFileHandler):
            continue
        try:
            if Path(getattr(handler, "baseFilename", "")).resolve() == want:
                found.append(handler)
        except OSError:
            continue
    return found


def _flush_abcxauto() -> None:
    for handler in logging.getLogger("abcxauto").handlers:
        handler.flush()


def test_default_log_path_is_absolute(tmp_path, monkeypatch):
    monkeypatch.delenv("ABCXAUTO_LOG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    dest = default_file_log_path()
    repo_root = Path(__file__).resolve().parents[1]
    assert dest.is_absolute()
    assert dest == (repo_root / "logs" / "app.log").resolve()
    assert dest != (tmp_path / "logs" / "app.log").resolve()

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr("abcxauto.config._REPO_ROOT", fake_repo)
    monkeypatch.setattr(
        "abcxauto.config._DEFAULT_FILE_LOG_PATH", fake_repo / "logs" / "app.log"
    )
    attached = setup_file_logging()
    assert attached.is_absolute()
    assert attached == (fake_repo / "logs" / "app.log").resolve()
    assert attached != REPO_APP_LOG
    assert Path(_rotating_handlers_for(attached)[0].baseFilename).is_absolute()


def test_setup_file_logging_does_not_double_attach(tmp_path):
    dest = tmp_path / "nested" / "app.log"
    setup_file_logging(path=dest)
    leftover = _rotating_handlers_for(dest)[0]
    leftover.setLevel(logging.WARNING)
    logging.getLogger("abcxauto").setLevel(logging.WARNING)
    setup_file_logging(path=dest)
    handlers = _rotating_handlers_for(dest)
    assert len(handlers) == 1
    assert handlers[0] is leftover
    assert handlers[0].level == logging.INFO
    assert logging.getLogger("abcxauto").level <= logging.INFO


def test_file_handler_and_logger_accept_info(tmp_path):
    dest = tmp_path / "nested" / "app.log"
    attached = setup_file_logging(path=dest)
    assert attached == dest.resolve()
    assert attached != REPO_APP_LOG
    root = logging.getLogger("abcxauto")
    handlers = _rotating_handlers_for(dest)
    assert len(handlers) == 1
    assert handlers[0].level == logging.INFO
    assert root.level != logging.NOTSET
    assert root.level <= logging.INFO


def test_info_think_send_fill_land_in_file(tmp_path):
    """Clean RTH look is INFO (think / send / fill). WARNING still lands.

    DEBUG cancelMktData-style lines stay off app.log — that spam is desk.out.
    Tests write a tmp dest, never the operator's logs/app.log.
    """
    import abcxauto.agent_loop as agent_loop_mod
    import abcxauto.brain as brain_mod
    import abcxauto.send as send_mod

    dest = tmp_path / "nested" / "app.log"
    live_before = REPO_APP_LOG.stat().st_size if REPO_APP_LOG.is_file() else None
    attached = setup_file_logging(path=dest)
    assert attached == dest.resolve()
    assert attached != REPO_APP_LOG
    assert send_mod.__name__ == "abcxauto.send"
    assert brain_mod.logger.name == "abcxauto.brain"
    assert agent_loop_mod.logger.name == "abcxauto.agent_loop"
    # File handler is on abcxauto, not process root — ib_insync stays off app.log.
    root_file = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")).resolve() == attached
    ]
    assert root_file == []

    tokens = {
        "abcxauto.brain": "abcxauto-file-log-info-think",
        "abcxauto.send": "abcxauto-file-log-info-send",
        "abcxauto.agent_loop": "abcxauto-file-log-info-fill",
    }
    warning_token = "abcxauto-file-log-warning-still-lands"
    debug_token = "cancelMktData reqId=99 debug-must-not-land"

    for name in (*tokens, "abcxauto.broker.connector"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    for name, token in tokens.items():
        logging.getLogger(name).info(token)
    logging.getLogger("abcxauto.brain").warning(warning_token)
    logging.getLogger("abcxauto.broker.connector").debug(debug_token)
    logging.getLogger("ib_insync.client").debug(debug_token)
    logging.getLogger("ib_insync.client").info(debug_token)

    _flush_abcxauto()
    text = dest.read_text(encoding="utf-8")
    for token in tokens.values():
        assert token in text
    assert warning_token in text
    assert debug_token not in text

    live_after = REPO_APP_LOG.stat().st_size if REPO_APP_LOG.is_file() else None
    assert live_after == live_before
