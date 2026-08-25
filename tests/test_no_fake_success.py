"""Launch must not kill the desk; aio must not treat IBKR errors as a finished wait."""

from __future__ import annotations

import asyncio
import sys

import pytest

from abcxauto.aio import safe_sleep


@pytest.mark.asyncio
async def test_safe_sleep_still_yields():
    await safe_sleep(0)


@pytest.mark.asyncio
async def test_safe_sleep_does_not_swallow_ibkr_index_error(monkeypatch):
    async def boom(_seconds):
        raise IndexError("empty IBKR ticks")

    monkeypatch.setattr(asyncio, "sleep", boom)
    with pytest.raises(IndexError, match="empty IBKR ticks"):
        await safe_sleep(0.1)


def test_cleanup_refuses_to_run_without_the_flag(monkeypatch):
    import abcxauto.__main__ as m

    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    with pytest.raises(RuntimeError, match="pre-launch cleanup"):
        m._cleanup(kill_only=True)
