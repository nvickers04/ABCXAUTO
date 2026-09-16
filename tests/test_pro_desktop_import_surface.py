"""Pin the public names re-exported by abcxauto.pro_desktop.

A later split must not silently drop a name other modules or tests import.
"""

from __future__ import annotations

import abcxauto.pro_desktop as pro

# Names the pre-split module defined (plus get_config, the test patch seam).
# Private _names are not part of the surface.
PUBLIC_NAMES = frozenset(
    {
        "AGENT_FIELD_KEYS",
        "AMBER",
        "ASIDE_W",
        "ASSETS_DIR",
        "BG",
        "BLUE",
        "BORDER",
        "BRAIN_FIELDS",
        "CARD_STATUS_COLOR",
        "CENTER_MIN_W",
        "FLOOR_GATES",
        "GREEN",
        "HOVER",
        "LINK_FIELDS",
        "LOGO_SRC",
        "MUTED",
        "NAV",
        "NAV_SUBTITLES",
        "NAV_TITLES",
        "NOTE_COLOR",
        "PACING_FIELDS",
        "PAGE_REFRESH_S",
        "PRO_TITLE",
        "ProTerminal",
        "RAIL_BTN_W",
        "RAIL_W",
        "RED",
        "RISK_FIELDS",
        "STREAM_ALARM",
        "STREAM_FONT_SIZE",
        "STREAM_POKE",
        "STREAM_TAIL_CHARS",
        "STREAM_WARN",
        "SURFACE",
        "TAIL_LIVE_S",
        "TEXT",
        "TITLE",
        "WHITE",
        "current_look_text",
        "format_token_count",
        "get_config",
        "grok_sub_color",
        "grok_sub_state",
        "last_card_send_label",
        "main",
        "run_app",
        "session_cap_idle_line",
        "stream_line_kind",
        "stream_view_lines",
        "think_tail_in_flight",
        "think_tail_last_marker",
        "think_tail_last_say",
        "think_tail_tool_chips",
        "write_launch_probe",
    }
)


def test_pro_desktop_public_import_surface():
    found = {name for name in dir(pro) if not name.startswith("_")}
    missing = PUBLIC_NAMES - found
    assert missing == set(), f"dropped public names: {sorted(missing)}"
