"""Public names live on ``abcxauto.lab_playbook`` so tests can monkeypatch them."""

from __future__ import annotations

from typing import Any


def hub() -> Any:
    import abcxauto.lab_playbook as lab

    return lab
