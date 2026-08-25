"""setup_file_logging writes a repo-absolute logs/app.log, once, WARNING+."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from abcxauto.config import default_file_log_path, setup_file_logging


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
    assert Path(_rotating_handlers_for(attached)[0].baseFilename).is_absolute()


def test_setup_file_logging_does_not_double_attach(tmp_path):
    dest = tmp_path / "nested" / "app.log"
    setup_file_logging(path=dest)
    setup_file_logging(path=dest)
    assert len(_rotating_handlers_for(dest)) == 1


def test_warning_from_abcxauto_lands_in_file(tmp_path):
    dest = tmp_path / "nested" / "app.log"
    setup_file_logging(path=dest)
    probe = logging.getLogger("abcxauto.file_log_probe")
    token = "abcxauto-file-log-warning-probe"
    probe.info("abcxauto-file-log-info-must-not-land")
    probe.warning(token)
    for handler in logging.getLogger("abcxauto").handlers:
        handler.flush()
    text = dest.read_text(encoding="utf-8")
    assert token in text
    assert "abcxauto-file-log-info-must-not-land" not in text
