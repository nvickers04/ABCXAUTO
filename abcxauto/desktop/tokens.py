"""Look tokens and field defs for the Pro cockpit. No engine deps."""
from __future__ import annotations

from pathlib import Path

import flet as ft

# X Lights Out palette — look only; nav/controls are product labels
BG = "#000000"
SURFACE = "#16181c"
HOVER = "#181818"
BORDER = "#2f3336"
TEXT = "#e7e9ea"
MUTED = "#71767b"
GREEN = "#00ba7c"
RED = "#f4212e"
BLUE = "#1d9bf0"
AMBER = "#ffd400"
WHITE = "#ffffff"

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
LOGO_SRC = "abcxauto_logo.png"
TITLE = "ABCXAUTO Pro"
PRO_TITLE = TITLE

# Tabs are the primary navigation, so the rail only carries the action pills and
# the account block — the width it used to spend on nav goes to the stream.
RAIL_W = 200
ASIDE_W = 320
CENTER_MIN_W = 520
RAIL_BTN_W = 168

# key, label, outlined icon, filled icon
NAV = [
    ("overview", "Dashboard", ft.Icons.DASHBOARD_OUTLINED, ft.Icons.DASHBOARD),
    (
        "positions",
        "Positions",
        ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
        ft.Icons.ACCOUNT_BALANCE_WALLET,
    ),
    ("notebook", "Playbook", ft.Icons.MENU_BOOK_OUTLINED, ft.Icons.MENU_BOOK),
    ("scorecard", "Scorecard", ft.Icons.BAR_CHART_OUTLINED, ft.Icons.BAR_CHART),
    ("risk", "Risk", ft.Icons.SHIELD_OUTLINED, ft.Icons.SHIELD),
    ("settings", "Settings", ft.Icons.TUNE_OUTLINED, ft.Icons.TUNE),
]
NAV_TITLES = {
    "overview": "Dashboard",
    "positions": "Positions",
    "notebook": "Playbook",
    "scorecard": "Scorecard",
    "risk": "Risk",
    "settings": "Settings",
}
NAV_SUBTITLES = {
    "overview": "Grok thinking, live. Looks, tools, tickets as they happen.",
    "positions": "Broker book — lots, working orders, fills, activity.",
    "notebook": "Grok's setup cards. Playbook, not law.",
    "scorecard": "Are the setups beating the model bill?",
    "risk": "The walk-away floor. Grok self_tunes inside it.",
    "settings": "Brain, pacing and link. Applies without a restart.",
}
CARD_STATUS_COLOR = {"working": GREEN, "testing": AMBER, "retired": MUTED}
# Settings fields, grouped the way the page shows them. label, hint.
BRAIN_FIELDS = (
    ("model", "Model", "real id (grok-4.6). leftover -xhigh becomes reasoning_effort"),
    ("model_rth", "RTH model", "empty = Model — thin sender. leftover suffix rewritten"),
    ("model_research", "Research model", "empty = Model — premarket/AH, no send"),
    ("temperature", "Temperature", "0.0 – 2.0"),
    ("max_tokens", "Max tokens", "1024 – 131072 per turn"),
    (
        "model_params",
        "Model params",
        "JSON object — reasoning_effort: low|medium|high|xhigh (effort alias). Empty = SDK default high. Next look rebuilds",
    ),
    (
        "model_params_rth",
        "RTH params",
        "JSON object — empty = Model params. xhigh stripped when RTH thin is on",
    ),
    (
        "model_params_research",
        "Research params",
        "JSON object — empty = Model params. Premarket/AH only",
    ),
)
PACING_FIELDS = (
    ("monitor_poll_s", "Monitor poll", "seconds, 5 – 900"),
    ("monitor_review_s", "Monitor review", "seconds, 30 – 21600"),
    ("disconnect_halt_s", "Disconnect halt", "seconds down before halt, 1 – 900"),
    (
        "session_look_cap",
        "Session look cap",
        "1 – 400 looks this session (premarket / RTH). Hit stays idle",
    ),
    (
        "session_token_cap",
        "Session token cap",
        "50000 – 10000000 billed tokens this session. Hit stays idle",
    ),
)
LINK_FIELDS = (
    ("ibkr_host", "IBKR host", "TWS host — disconnected only"),
    ("ibkr_client_id", "IBKR client id", "one per process — disconnected only"),
)
AGENT_FIELD_KEYS = frozenset(
    k for k, _l, _h in (*BRAIN_FIELDS, *PACING_FIELDS, *LINK_FIELDS)
)
# The walk-away floor. Operator may re-arm; nothing in the UI may disarm.
FLOOR_GATES = (
    ("defined_risk_only", "Defined-risk only"),
    ("cash_only", "Cash only"),
    ("risk_gates_enabled", "Pre-trade gates"),
    ("auto_panic_on_breach", "Auto-panic on breach"),
)
RISK_FIELDS = (
    ("max_risk_per_trade_pct", "Max risk / trade", "% of NetLiq, 0 = off, else 0.25 – 25"),
    ("daily_loss_limit_pct", "Daily loss limit", "% of NetLiq, 0.5 – 25"),
    ("max_position_pct", "Max position", "% of NetLiq, 0 = off, else 5 – 25"),
    ("max_symbol_concentration_pct", "Max per name", "% of NetLiq, all lots, 5 – 25"),
    ("max_arena_concentration_pct", "Max per arena", "% of NetLiq, one sector/theme arena, 5 – 25"),
    ("max_peak_drawdown_pct", "Peak drawdown", "% of NetLiq, 2 – 25"),
    ("max_option_premium_pct", "Max option premium", "% of NetLiq, 0 = off, else 1 – 25"),
    ("max_open_positions", "Max open lots", "0 = off — Grok may set N for this book"),
    ("portfolio_cap_usd", "Portfolio max-loss $", "Display only. Not a place refuse. Default 800 is not a gate"),
)
# ProEngine._note kinds. Anything not listed still paints its message in MUTED,
# so a new note kind is visible the day it is added.
NOTE_COLOR = {
    "err": RED,
    "error": RED,
    "retry": AMBER,
    "park": AMBER,
    "pause": AMBER,
    "cap": AMBER,
}
PAGE_REFRESH_S = 3.0
# Length-growth hold. Tool waits and the post-look snap are longer than this;
# think_tail_in_flight is what keeps those ticks looking.
TAIL_LIVE_S = 12.0
# Markers that mean the tail is still inside a look, not an idle desk.
_IN_FLIGHT_MARKERS = frozenset({"think", "tool", "banner", "cached", "send", "warn"})
# Tool-chip window on the live RAM buffer — not the pane. The pane reads
# today's think_session file. think_tail.txt stays an 8kb overwrite stub.
STREAM_TAIL_CHARS = 8000
# Wrapped tool output at 13px Consolas in a narrow column is hard to read; the
# stream is the surface the operator actually sits and reads.
STREAM_FONT_SIZE = 14
# Markers think_stream/brain emit. The text is Grok's — we colour it, never
# rewrite it. Anything unlisted paints as prose, so a new marker still shows.
STREAM_ALARM = (
    "[stream failed",
    "[stream stalled]",
    "[stream loop]",
    "timed out",
)
STREAM_WARN = ("[think stopped:", "[truncated: max_tokens]")
STREAM_POKE = ("[fill]", "[order_change]", "[unprotected]", "[stop_dist]")
