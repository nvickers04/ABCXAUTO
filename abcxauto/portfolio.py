"""Thin re-export — prefer ``abcxauto.book`` as the public API."""

from __future__ import annotations

from abcxauto.book import (
    build_book_from_snap,
    build_portfolio_state,
    portfolio_narrative,
)

__all__ = ("build_portfolio_state", "build_book_from_snap", "portfolio_narrative")
