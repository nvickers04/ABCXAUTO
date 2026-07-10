"""Shared pytest fixtures for ABCXAUTO."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_config():
    """Minimal config stand-in for unit tests that patch get_config."""
    return type(
        "Cfg",
        (),
        {
            "xai_api_key": "test-key",
            "model": "test",
            "ibkr_host": "127.0.0.1",
            "ibkr_port": 7497,
        },
    )()
