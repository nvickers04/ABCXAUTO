"""Pro desktop visual theme, nav, and Flet layout helpers."""

from __future__ import annotations

import flet as ft

TITLE = "ABCXAUTO Pro v0.1.1"
BG = "#0b0e14"
CARD = "#151b26"
CARD2 = "#1c2433"
BORDER = "#2a3548"
TEXT = "#e8edf4"
MUTED = "#8b9bb4"
GREEN = "#00d47e"
RED = "#ff4d6d"
BLUE = "#4dabf7"
AMBER = "#ffc857"
NAV = [
    ("overview", "Overview", ft.Icons.DASHBOARD_OUTLINED),
    ("positions", "Positions", ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED),
    ("brain", "AI Brain", ft.Icons.PSYCHOLOGY_OUTLINED),
    ("logs", "Logs & Evolution", ft.Icons.TIMELINE_OUTLINED),
    ("settings", "Settings", ft.Icons.SETTINGS_OUTLINED),
]
FILTERS = ["All", "Trades", "Decisions", "Improvements", "Errors"]


def pad(h: int = 0, v: int = 0) -> ft.padding.Padding:
    return ft.padding.Padding.symmetric(horizontal=h, vertical=v)


def margin(**kwargs: int) -> ft.margin.Margin:
    return ft.margin.Margin.only(**kwargs)


def border(color: str, width: float = 1) -> ft.border.Border:
    return ft.border.Border.all(width, color)
