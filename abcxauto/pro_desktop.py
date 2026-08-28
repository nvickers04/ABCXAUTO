"""ABCXAUTO Pro — three-column Flet cockpit over ProEngine.

One surface per thing the project actually has: the live look (Dashboard),
Grok's think stream (Stream), the screen Grok pulled (Scan), the broker book
(Positions), Grok's setup cards (Playbook), how that book is scoring against
the model bill (Scorecard), and the walk-away floor (Risk).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import flet as ft

from abcxauto.broker.connection import LIVE_CONFIRM_PHRASE
from abcxauto.config import get_config, setup_file_logging
from abcxauto.pro_engine import ProEngine
from abcxauto.reality_pulse import build_reality_pulse, format_desk_clock, pulse_clock_view

logger = logging.getLogger(__name__)

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

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
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
    ("model", "Model", "beats ABCXAUTO_MODEL — next look rebuilds"),
    ("temperature", "Temperature", "0.0 – 2.0"),
    ("max_tokens", "Max tokens", "1024 – 131072 per turn"),
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
    ("max_risk_per_trade_pct", "Max risk / trade", "% of NetLiq, 0.25 – 25"),
    ("daily_loss_limit_pct", "Daily loss limit", "% of NetLiq, 0.5 – 25"),
    ("max_position_pct", "Max position", "% of NetLiq, 5 – 25"),
    ("max_symbol_concentration_pct", "Max per name", "% of NetLiq, all lots, 5 – 25"),
    ("max_peak_drawdown_pct", "Peak drawdown", "% of NetLiq, 2 – 25"),
    ("max_option_premium_pct", "Max option premium", "% of NetLiq, 1 – 25"),
    ("max_open_positions", "Max open lots", "slots — Grok picks N for this book"),
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
# The spine paints the readable tail — same size as the think_tail.txt mirror.
# The raw fallback label stays short so an off-screen buffer is a small diff.
STREAM_TAIL_CHARS = 8000
# Wrapped tool output at 13px Consolas in a narrow column is hard to read; the
# stream is the surface the operator actually sits and reads.
STREAM_FONT_SIZE = 14
STREAM_RAW_TAIL_CHARS = 1800
STREAM_MAX_LINES = 180
# Markers think_stream/brain emit. The text is Grok's — we colour it, never
# rewrite it. Anything unlisted paints as prose, so a new marker still shows.
STREAM_ALARM = (
    "[stream failed",
    "[stream stalled]",
    "[stream loop]",
    "timed out",
)
STREAM_WARN = ("[think stopped:", "[truncated: max_tokens]")
STREAM_POKE = ("[fill]", "[order_change]", "[unprotected]")


def stream_line_kind(line: str) -> str:
    """Marker class for one raw stream line. Reads the text, never edits it."""
    s = (line or "").strip()
    if not s:
        return "blank"
    if s.startswith("--- GROK") or s.startswith("--- CLERK"):
        return "banner"
    if s == "[think]":
        return "think"
    if s == "[say]":
        return "say"
    if s == "[clerk]":
        return "clerk"
    if s in STREAM_POKE:
        return "poke"
    if any(frag in s for frag in STREAM_ALARM):
        return "alarm"
    if any(frag in s for frag in STREAM_WARN):
        return "warn"
    if s.startswith("[") and s.endswith("]"):
        if "= already have it" in s:
            return "cached"
        return "send" if s == "[send]" else "tool"
    if s.startswith("hits=") and " src=" in s:
        return "scan"
    return "prose"


def current_look_text(buf: str) -> str:
    """The live look: from the last GROK banner, not a clerk speaker."""
    text = buf or ""
    grok = text.rfind("--- GROK")
    return text[grok:] if grok >= 0 else text


def think_tail_tool_chips(buf: str) -> list[str]:
    """Tool names from the readable tail's [chip] lines, not last_turn.tool_trace.

    Each model step paints ``--- GROK ---``, so the last banner is often an
    open [think] with [book]/[scan]/… still above it. Counting only
    current_look_text then paints 0 tools on a live look.
    """
    names: list[str] = []
    for raw in (buf or "")[-STREAM_TAIL_CHARS:].splitlines():
        if stream_line_kind(raw) != "tool":
            continue
        inner = raw.strip()[1:-1].strip()
        if inner:
            names.append(inner.split()[0])
    return names


def think_tail_last_marker(buf: str) -> str:
    """Last stream marker in the tail. Prose keeps the marker it follows."""
    last = ""
    for raw in (buf or "").splitlines():
        kind = stream_line_kind(raw)
        if kind in (
            "think",
            "say",
            "tool",
            "banner",
            "cached",
            "send",
            "warn",
            "alarm",
            "poke",
        ):
            last = kind
    return last


def think_tail_in_flight(buf: str) -> bool:
    """True when the tail is still inside a look (open think, tool, or new banner)."""
    return think_tail_last_marker(current_look_text(buf)) in _IN_FLIGHT_MARKERS


def _say_is_real(text: str) -> bool:
    t = " ".join((text or "").split())
    return bool(t) and t not in {"?", "—", "-", ".", "…"}


def think_tail_last_say(buf: str) -> str:
    """Last real assistant [say] in the tail. Junk '?' does not wipe an earlier say."""
    found: list[str] = []
    lines = (buf or "").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() != "[say]":
            i += 1
            continue
        i += 1
        parts: list[str] = []
        while i < len(lines):
            kind = stream_line_kind(lines[i])
            if kind not in ("prose", "blank"):
                break
            bit = lines[i].strip()
            if bit:
                parts.append(bit)
            i += 1
        text = " ".join(parts).strip()
        if _say_is_real(text):
            found.append(text)
    return found[-1] if found else ""


def last_card_send_label(rows: list[dict[str, Any]] | None = None) -> str:
    """Last real card_sends.jsonl row. Does not invent a card."""
    if rows is None:
        try:
            from abcxauto.lab_playbook import _card_sends

            rows = _card_sends(limit=40)
        except Exception:
            return ""
    if not rows:
        return ""
    row = rows[-1] if isinstance(rows[-1], dict) else {}
    card = str(row.get("card") or "").strip()
    if not card:
        return ""
    symbol = str(row.get("symbol") or "").strip()
    return f"{symbol} · {card}" if symbol else card


def grok_sub_state(
    *,
    running: bool,
    status: str = "",
    fail_streak: int = 0,
    parked: bool = False,
    tail_moved: bool = False,
    tail_live: bool = False,
) -> str:
    """Grok sub: looking | sat | look failed. fail_streak cannot hide a live think."""
    if not running:
        return "off"
    st = (status or "").lower()
    idle = parked or st in ("parked", "idle")
    looking = (not idle) and (
        st.startswith("thinking")
        or st.startswith("grok")
        or bool(tail_moved)
        or bool(tail_live)
    )
    if looking:
        return "looking"
    if int(fail_streak or 0) > 0:
        return "look failed"
    return "sat"


def grok_sub_color(state: str) -> str:
    if state == "looking":
        return GREEN
    if state == "look failed":
        return AMBER
    if state == "sat":
        return TEXT
    return MUTED


class ProTerminal:
    def __init__(self, page: ft.Page):
        self.page = page
        self.engine = ProEngine()
        self.tab = "overview"
        self._think_sync_key: str | None = None  # None = never painted, "" = painted empty
        self._build_refs()
        self._sync_widgets()

    def _build_refs(self) -> None:
        self.lbl_title = ft.Text("ABCXAUTO", size=18, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_model = ft.Text("Grok —", size=11, color=MUTED)
        self.lbl_status = ft.Text("Safe", size=18, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_clock = ft.Text("—", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_session_badge = ft.Text("—", size=11, weight=ft.FontWeight.W_600, color=AMBER)
        self.lbl_countdown_title = ft.Text("Close time", size=11, color=MUTED)
        self.lbl_countdown = ft.Text("—", size=11, color=TEXT)
        self.lbl_data_age = ft.Text("n/a", size=11, color=TEXT)
        self.lbl_mandate_health = ft.Text(
            "green — protected", size=11, weight=ft.FontWeight.W_600, color=GREEN
        )
        self.lbl_pulse_narrative = ft.Text(
            "Reality Pulse idle — Start for live awareness.",
            size=12,
            color=MUTED,
            selectable=True,
        )
        self.dot_conn = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.dot_xai = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.dot_mda = ft.Container(width=10, height=10, border_radius=5, bgcolor=RED)
        self.lbl_ibkr_status = ft.Text("Disconnected", size=12, color=MUTED)
        self.lbl_xai_status = ft.Text("Missing key", size=12, color=MUTED)
        self.lbl_mda_status = ft.Text("Not configured", size=12, color=MUTED)
        self.lbl_link = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_banner = ft.Text("", size=12, color=AMBER, selectable=True, visible=False)
        self.lbl_tools = ft.Text("Tools: —", size=12, color=MUTED, selectable=True)
        self.lbl_playbook = ft.Text("Playbook: —", size=12, color=MUTED, selectable=True)
        self.lbl_score = ft.Text("Score: —", size=13, color=MUTED, selectable=True)
        self.lbl_session_score = ft.Text("sess —", size=12, color=MUTED, selectable=True)
        self._score_last = 0.0
        self.lbl_account_name = ft.Text("IBKR", size=14, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_account_id = ft.Text("Not connected", size=12, color=MUTED)
        self.lbl_account_mode = ft.Text("Paper", size=12, weight=ft.FontWeight.W_600, color=GREEN)
        self.btn_account_mode = ft.Container(
            content=self.lbl_account_mode,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, GREEN),
            border_radius=999,
            ink=True,
            tooltip="Switch Paper / Live",
            on_click=self._toggle_trading_mode,
        )
        self.lbl_floors = ft.Text("Floors off", size=12, weight=ft.FontWeight.W_600, color=AMBER)
        self.btn_floors = ft.Container(
            content=self.lbl_floors,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, AMBER),
            border_radius=999,
            ink=True,
            tooltip="Paper: toggle % size floors. Live: always on.",
            on_click=self._toggle_sizing_floors,
        )
        self.tf_live_confirm = ft.TextField(
            label="Type live confirm phrase",
            password=True,
            can_reveal_password=True,
            dense=True,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            width=320,
        )
        self.lbl_equity = ft.Text("$0", size=28, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_equity_sub = ft.Text("", size=11, color=MUTED)
        self.lbl_pnl = ft.Text("$+0.00", size=14, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_pnl_pct = ft.Text("", size=11, color=MUTED)
        self.lbl_ret_1w = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_3m = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_1y = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_ret_source = ft.Text("IBKR NAV — building history…", size=10, color=MUTED)
        self._ret_cache: dict | None = None
        self._ret_last_fetch = 0.0
        self.news_list = ft.Column(spacing=0, tight=True)
        self._news_last_fetch = 0.0
        self._news_cache: list[dict] = []
        # Status-strip values: small type. This band is a status bar, not a panel.
        self.lbl_unprotected = ft.Text("0", size=13, weight=ft.FontWeight.BOLD, color=GREEN)
        self.lbl_halt = ft.Text("clear", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_edge = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=MUTED)
        self.lbl_edge_sub = ft.Text("vs model", size=11, color=MUTED)
        self.lbl_open_upnl = ft.Text("—", size=14, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_open_upnl_sub = ft.Text("open marks", size=11, color=MUTED)
        self.lbl_desk = ft.Text("Grok off", size=13, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_desk_sub = ft.Text("", size=11, color=MUTED)
        self.lbl_lot_count = ft.Text("0", size=13, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_mix = ft.Text("Mix: —", size=12, color=MUTED, selectable=True)
        self.lbl_path = ft.Text("Path: —", size=12, color=MUTED, selectable=True)
        self._path_last = 0.0
        self._page_last = 0.0
        self._brief_last = 0.0
        self._brief_row: dict = {}
        self._prev_text: str | None = None
        self.lbl_risk = ft.Text("—", size=12, color=MUTED, selectable=True)
        self.lbl_pace = ft.Text("Pace: —", size=12, color=MUTED, selectable=True)
        self.lbl_last_send = ft.Text("—", size=12, color=MUTED, selectable=True)
        self.lbl_result = ft.Text("Result: —", size=12, color=MUTED, selectable=True)
        self.lbl_why = ft.Text("Why: —", size=12, color=MUTED, selectable=True)
        self.lbl_focus = ft.Text("Focus: —", size=12, color=MUTED, selectable=True)
        self.lbl_lessons = ft.Text("", size=11, color=AMBER, selectable=True, visible=False)
        # ---- Scan tape: the screen Grok pulled, in IBKR's own order.
        self.lbl_scan_head = ft.Text(
            "No screen this session — Grok runs the scanner.",
            size=11,
            color=MUTED,
            selectable=True,
        )
        self.col_scan = ft.Column(spacing=3, tight=True)
        self._scan_key = ""
        # ---- Health strip: the three things that make the operator step in —
        # silence, burn, and a link that explains a quiet desk. Not risk numbers.
        self.lbl_hs_state = ft.Text("off", size=12, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_hs_age = ft.Text("no look yet", size=11, color=MUTED, selectable=True)
        self.lbl_hs_next = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_hs_burn = ft.Text("no looks yet", size=12, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_hs_look = ft.Text("this look: —", size=11, color=MUTED, selectable=True)
        self.lbl_hs_link = ft.Text("link —", size=11, color=MUTED, selectable=True)
        self.health_box = ft.Container(padding=0)
        self._sc_last: dict = {}
        self._tail_len: int | None = None
        self._tail_fp: str | None = None
        self._tail_moved_mono = 0.0
        # ---- Book strip: context for reading the stream, not a risk panel.
        self.lbl_book_strip = ft.Text("No open lots", size=12, color=MUTED, selectable=True)
        self.col_book_strip = ft.Column(spacing=2, tight=True)
        self._book_strip_key = ""
        self.lbl_stream_status = ft.Text("Grok stream", size=12, color=MUTED)
        self.think_live = ft.Text(
            "Grok stream: waiting for tools...",
            size=STREAM_FONT_SIZE,
            color=MUTED,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        self.btn_copy_stream = self._btn("Copy stream", outlined=True, on_click=self._copy_stream)
        # The spine: one control per stream line so markers can be styled. The
        # raw label above stays the plain-text fallback and the empty state.
        self.col_stream = ft.Column(spacing=0, tight=True)
        self._stream_lines_key = ""
        self._stream_follow = True
        self.lbl_stream_follow = ft.Text("live", size=11, weight=ft.FontWeight.W_600, color=GREEN)
        self.btn_stream_follow = ft.Container(
            content=self.lbl_stream_follow,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border.all(1, GREEN),
            border_radius=999,
            ink=True,
            tooltip="Following the tail. Click to hold position / jump back to live.",
            on_click=self._toggle_stream_follow,
        )
        # auto_scroll pins the tail on the client. A server-side scroll_to here is
        # an invoke_method, and its result can land after a tab swap has already
        # unregistered this control — which kills flet's receive loop.
        self.think_scroll = ft.Column(
            [self.col_stream, self.think_live],
            scroll=ft.ScrollMode.AUTO,
            auto_scroll=True,
            expand=True,
            spacing=0,
        )
        # ---- Positions blotter: one row control per record, text form kept as fallback.
        self.lbl_positions = ft.Text("No open positions", size=12, color=MUTED, selectable=True)
        self.lbl_working_orders = ft.Text(
            "No working orders", size=12, color=MUTED, selectable=True
        )
        self.lbl_recent_fills = ft.Text(
            "No fills this session", size=12, color=MUTED, selectable=True
        )
        self.lbl_activity = ft.Text("Connect IBKR.", size=12, color=MUTED, selectable=True)
        self.col_lots = ft.Column(
            [self.lbl_positions], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self.col_orders = ft.Column(
            [self.lbl_working_orders], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self.col_fills = ft.Column(
            [self.lbl_recent_fills], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self.col_activity = ft.Column(
            [self.lbl_activity], spacing=3, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self._lots_key = ""
        self._orders_key = ""
        self._fills_key = ""
        self._activity_key = ""
        self._tab = "lots"
        self.tabs: dict[str, dict[str, Any]] = {}
        for key, label in (
            ("lots", "Lots"),
            ("orders", "Working orders"),
            ("fills", "Session fills"),
            ("log", "Activity"),
        ):
            self.tabs[key] = self._tab_chip(key, label)
        self.tab_bodies: dict[str, ft.Control] = {
            "lots": self.col_lots,
            "orders": self.col_orders,
            "fills": self.col_fills,
            "log": self.col_activity,
        }
        self.lbl_risk_status = ft.Text("", size=12, color=MUTED, selectable=True)
        # One line each — a long rationale must not reserve blank rows.
        for lbl in (
            self.lbl_banner,
            self.lbl_mix,
            self.lbl_score,
            self.lbl_path,
            self.lbl_pace,
            self.lbl_playbook,
            self.lbl_risk,
            self.lbl_tools,
            self.lbl_last_send,
            self.lbl_result,
            self.lbl_why,
            self.lbl_focus,
            self.lbl_lessons,
            self.lbl_scan_head,
        ):
            lbl.max_lines = 1
            lbl.overflow = ft.TextOverflow.ELLIPSIS
        self.btn_connect = self._btn(
            "Connect IBKR", outlined=True, on_click=self._toggle_connect, width=RAIL_BTN_W
        )
        self.btn_run = self._btn("Start", filled=True, on_click=self._toggle_run, width=RAIL_BTN_W)
        self.btn_halt = self._btn(
            "Halt", outlined=True, on_click=self._toggle_halt, width=RAIL_BTN_W
        )
        self.btn_refresh = self._btn(
            "Refresh book", outlined=True, on_click=self._refresh_book, width=RAIL_BTN_W
        )
        self.lbl_run_state = ft.Text("Grok off", size=12, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_alert = ft.Text("", size=12, color=RED, selectable=True, visible=False)
        # ---- Notebook page
        self.lbl_notebook_head = ft.Text("", size=13, color=TEXT, selectable=True)
        self.lbl_notebook_meta = ft.Text("", size=12, color=MUTED, selectable=True)
        self.lbl_notebook_lots = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_notebook_body = ft.Text(
            "",
            size=12,
            color=TEXT,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        self.col_notebook_cards = ft.Column(spacing=8, tight=True)
        self.col_notebook_types = ft.Column(spacing=2, tight=True)
        self.lbl_notebook_types = ft.Text("", size=12, color=MUTED, selectable=True)
        self.notebook_raw_panel = ft.Container(visible=False)
        self._notebook_key = ""
        # ---- Scorecard page
        self.lbl_sc_netliq = ft.Text("—", size=22, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_sc_verdict = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=MUTED)
        self.lbl_sc_score = ft.Text("Score: —", size=12, color=MUTED, selectable=True)
        self.lbl_sc_session = ft.Text("sess —", size=12, color=MUTED, selectable=True)
        self.lbl_sc_path = ft.Text("Path: —", size=12, color=MUTED, selectable=True)
        self.lbl_sc_mix = ft.Text("Mix: —", size=12, color=MUTED, selectable=True)
        self.lbl_sc_strats = ft.Text("", size=12, color=MUTED, selectable=True)
        self.col_sc_windows = ft.Column(spacing=3, tight=True)
        self.col_sc_cards = ft.Column(spacing=3, tight=True)
        self.col_sc_ledger = ft.Column(spacing=3, tight=True)
        # ---- Risk page
        self.lbl_risk_glance = ft.Text("", size=12, color=MUTED, selectable=True, no_wrap=False)
        self.col_risk_knobs = ft.Column(spacing=3, tight=True)
        self.lbl_risk_posture = ft.Text("—", size=13, weight=ft.FontWeight.W_600, color=TEXT)
        self.lbl_risk_floors = ft.Text("", size=12, color=MUTED, selectable=True)
        self.sw_size_floors = ft.Switch(
            value=False,
            active_color=GREEN,
            on_change=self._toggle_sizing_floors,
        )
        self.lbl_risk_halt_state = ft.Text("", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_risk_halt_math = ft.Text("", size=12, color=MUTED, selectable=True)
        # An open edit must survive the 3s page repaint, so a touched field is
        # dirty until it is applied or the page is refreshed.
        self.fields: dict[str, ft.TextField] = {}
        self.gates: dict[str, ft.Switch] = {}
        self._dirty: set[str] = set()
        # Derived, not restated: a knob added to RISK_FIELDS must get a widget or
        # the Risk page raises on the row it cannot build.
        for key, _label, _hint in RISK_FIELDS:
            self._num_field(key)
        for key in (
            "defined_risk_only",
            "cash_only",
            "risk_gates_enabled",
            "auto_panic_on_breach",
        ):
            self.gates[key] = ft.Switch(
                value=True,
                active_color=GREEN,
                on_change=lambda e, k=key: self._toggle_floor_gate(k),
            )
        self._risk_key = ""
        # ---- Settings page
        for key in (
            "model",
            "temperature",
            "max_tokens",
            "monitor_poll_s",
            "monitor_review_s",
            "disconnect_halt_s",
            "session_look_cap",
            "session_token_cap",
            "ibkr_host",
            "ibkr_client_id",
        ):
            self._num_field(
                key,
                width=150
                if key in ("model", "ibkr_host", "session_token_cap")
                else 110,
            )
        for key in ("monitor_enabled", "monitor_extended_hours"):
            self.gates[key] = ft.Switch(
                value=True,
                active_color=BLUE,
                on_change=lambda e, k=key: self._apply_agent_switch(k),
            )
        self.lbl_settings_status = ft.Text("", size=12, color=MUTED, selectable=True)
        self.lbl_settings_link = ft.Text("", size=12, color=TEXT, selectable=True)
        self.lbl_settings_mode = ft.Text("Paper", size=13, weight=ft.FontWeight.W_600, color=GREEN)
        self.lbl_settings_brain = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_settings_path = ft.Text("", size=11, color=MUTED, selectable=True)
        self.lbl_dash_tools = ft.Text("Tools: —", size=12, color=MUTED, selectable=True)
        self.lbl_nb_playbook = ft.Text("Playbook: —", size=12, color=MUTED, selectable=True)
        for lbl in (self.lbl_dash_tools, self.lbl_nb_playbook):
            lbl.max_lines = 1
            lbl.overflow = ft.TextOverflow.ELLIPSIS
        # Facts the Cockpit computes; the Dashboard stays a live look, not a report.
        # Mode repeats the rail's Paper/Live pill. tools / focus / pace live in
        # the stream, the playbook and the next-look line. Open MTM is on the
        # Account card next to Today — not hidden.
        self._hidden_metrics = ft.Column(
            [
                self.lbl_session_score,
                self.lbl_path,
                self.lbl_playbook,
                self.lbl_tools,
                self.lbl_status,
                self.lbl_focus,
                self.lbl_dash_tools,
                self.lbl_pulse_narrative,
                self.lbl_pace,
                self.lbl_risk,
            ],
            visible=False,
            spacing=0,
        )
        self.content = ft.Container(expand=True, padding=0)
        self.surface_tabs: dict[str, dict[str, Any]] = {}
        self.lbl_center_title = ft.Text("Dashboard", size=20, weight=ft.FontWeight.BOLD, color=TEXT)
        self.lbl_center_subtitle = ft.Text(NAV_SUBTITLES["overview"], size=12, color=MUTED)

    # ---------------------------------------------------------------- widgets

    def _num_field(self, key: str, *, width: int = 120) -> ft.TextField:
        """One editable config field. Enter applies it; typing marks it dirty."""
        tf = ft.TextField(
            dense=True,
            width=width,
            color=TEXT,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=BLUE,
            text_size=12,
            content_padding=8,
            on_change=lambda _e, k=key: self._dirty.add(k),
            on_submit=lambda _e, k=key: self._apply_field(k),
        )
        self.fields[key] = tf
        return tf

    def _set_field(self, key: str, value: Any) -> None:
        tf = self.fields.get(key)
        if tf is None or key in self._dirty:
            return
        if isinstance(value, bool) or value is None:
            tf.value = ""
        elif isinstance(value, float):
            tf.value = f"{value:g}"
        else:
            tf.value = str(value)

    def _field_row(
        self, key: str, label: str, hint: str, *, control: ft.Control | None = None
    ) -> ft.Control:
        body: list[ft.Control] = [
            ft.Container(expand=True, content=ft.Text(label, size=12, color=TEXT)),
            ft.Container(
                width=232,
                tooltip=hint,
                content=ft.Text(
                    hint,
                    size=11,
                    color=MUTED,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            ),
            control if control is not None else self.fields[key],
        ]
        if control is None:
            body.append(
                ft.Container(
                    width=30,
                    height=30,
                    border_radius=15,
                    border=ft.Border.all(1, BORDER),
                    alignment=ft.Alignment.CENTER,
                    ink=True,
                    tooltip="Apply",
                    on_click=lambda _e, k=key: self._apply_field(k),
                    content=ft.Icon(ft.Icons.CHECK, size=15, color=TEXT),
                )
            )
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            border=ft.Border.all(1, BORDER),
            content=ft.Row(body, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def _tab_chip(self, key: str, label: str) -> dict[str, Any]:
        text = ft.Text(label, size=12, weight=ft.FontWeight.W_600, color=MUTED)
        count = ft.Text("", size=11, color=MUTED)
        chip = ft.Container(
            content=ft.Row([text, count], spacing=5, tight=True),
            padding=ft.Padding.symmetric(horizontal=10, vertical=5),
            border_radius=999,
            border=ft.Border.all(1, BORDER),
            ink=True,
            on_click=lambda _e, k=key: self._select_tab(k),
        )
        return {"chip": chip, "text": text, "count": count, "label": label}

    def _select_tab(self, key: str) -> None:
        self._tab = key
        self._sync_tabs()
        self._safe_update()

    def _btn(
        self,
        text: str,
        *,
        on_click,
        filled: bool = False,
        outlined: bool = False,
        width: int | None = None,
    ) -> ft.Button:
        style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=999),
            padding=ft.Padding.symmetric(horizontal=18, vertical=11),
            side=ft.BorderSide(1, BORDER) if outlined or not filled else None,
        )
        btn = ft.Button(
            content=text,
            bgcolor=WHITE if filled else BG,
            color="#0f1419" if filled else TEXT,
            style=style,
            on_click=on_click,
        )
        btn.text = text
        if width:
            btn.width = width
        return btn

    def _avatar(self, letter: str = "A", size: int = 40) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=size // 2,
            bgcolor=SURFACE,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(letter, size=size // 2, weight=ft.FontWeight.BOLD, color=TEXT),
        )

    @staticmethod
    def _chip(text: str, color: str) -> ft.Container:
        return ft.Container(
            content=ft.Text(text, size=11, weight=ft.FontWeight.W_600, color=color),
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
            border=ft.Border.all(1, color),
            border_radius=999,
        )

    @staticmethod
    def _cell(
        text: str,
        *,
        width: int | None = None,
        expand: bool = False,
        color: str = TEXT,
        size: int = 12,
        mono: bool = False,
        right: bool = False,
        weight: Any = None,
    ) -> ft.Control:
        label = ft.Text(
            text,
            size=size,
            color=color,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            font_family="Consolas" if mono else None,
            text_align=ft.TextAlign.RIGHT if right else None,
            weight=weight,
        )
        if expand:
            return ft.Container(expand=True, content=label)
        return ft.Container(width=width, content=label)

    def _blotter_row(self, cells: list[ft.Control], *, alert: bool = False) -> ft.Container:
        return ft.Container(
            content=ft.Row(cells, spacing=6),
            padding=ft.Padding.symmetric(horizontal=8, vertical=5),
            border_radius=6,
            bgcolor=SURFACE if alert else BG,
            border=ft.Border.all(1, RED if alert else BORDER),
        )

    def _head_row(self, cols: list[tuple[str, int | None]]) -> ft.Container:
        cells = [
            self._cell(
                name,
                width=w,
                expand=w is None,
                color=MUTED,
                size=11,
                weight=ft.FontWeight.W_600,
            )
            for name, w in cols
        ]
        return ft.Container(
            content=ft.Row(cells, spacing=6),
            padding=ft.Padding.only(left=8, right=8, top=2, bottom=2),
        )

    # ------------------------------------------------------------------ build

    def build(self) -> None:
        p, cfg = self.page, get_config()
        p.title = PRO_TITLE
        p.bgcolor = BG
        p.padding = 0
        p.theme_mode = ft.ThemeMode.DARK
        try:
            p.window.visible = True
            p.window.width = 1280
            p.window.height = 860
            p.window.min_width = RAIL_W + CENTER_MIN_W + ASIDE_W
            p.window.min_height = 720
            # Without prevent_close flet never delivers the "close" event, so the
            # stop-on-close below was dead. We latch, then destroy the window.
            p.window.prevent_close = True
            p.window.on_event = self._on_window_event
        except Exception:
            pass
        self.lbl_model.value = f"Grok {getattr(cfg, 'model', '—')}"
        try:
            from abcxauto.memory import get_journal

            get_journal().ensure_model_session(str(getattr(cfg, "model", "") or ""))
        except Exception:
            pass
        try:
            p.controls.clear()
        except Exception:
            pass
        p.add(self._shell())
        self._show_tab("overview")
        self._sync_widgets()
        p.update()
        p.run_task(self._poll_loop)
        p.run_task(self._clock_loop)
        p.run_task(self._reveal_window)
        from abcxauto.cursor_env import should_autostart

        if should_autostart():
            self._start()
        if probe := os.environ.get("ABCXAUTO_UI_PROBE"):
            Path(probe).write_text(
                json.dumps(
                    {
                        "title": p.title,
                        "tab": self.tab,
                        "ui_built": True,
                        "engine": "ProEngine",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def _on_window_event(self, e: Any = None) -> None:
        """Closing the window is the operator saying stop — the supervisor must obey."""
        kind = str(getattr(e, "type", "") or getattr(e, "data", "") or "").lower()
        if "close" not in kind:
            return
        try:
            if os.environ.get("ABCXAUTO_UI_PROBE"):
                # A preview window is not the desk. Latching here silently blocks
                # the supervisor's next launch of the real Pro.
                logger.info("probe window closed — operator stop not latched")
            else:
                from abcxauto.supervisor import kill_descendant_flet, mark_operator_stop

                mark_operator_stop()
                kill_descendant_flet()
        except Exception:
            logger.debug("operator stop on window close failed", exc_info=True)
        finally:
            # prevent_close holds the window open, so the operator only gets out
            # if this runs no matter what happened above.
            self._destroy_window()

    def _destroy_window(self) -> None:
        """flet's window destroy/close are coroutines — they need the page loop."""
        win = getattr(self.page, "window", None)
        for step in ("destroy", "close"):
            fn = getattr(win, step, None)
            if fn is None:
                continue
            try:
                self.page.run_task(fn)
                return
            except Exception:
                logger.debug("window %s failed", step, exc_info=True)

    def _shell(self) -> ft.Control:
        """Left rail · center column · right rail. Rails are fixed; the feed absorbs width."""
        return ft.Row(
            [self._left_rail(), self._center_column(), self._right_rail()],
            expand=True,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _left_rail(self) -> ft.Container:
        logo = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            content=ft.Row(
                [
                    ft.Image(
                        src=LOGO_SRC,
                        width=36,
                        height=36,
                        fit=ft.BoxFit.CONTAIN,
                        error_content=ft.Text(
                            "A", size=22, weight=ft.FontWeight.BOLD, color=BLUE
                        ),
                    ),
                    ft.Column([self.lbl_title, self.lbl_model], spacing=0, tight=True),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._sync_ibkr_account_label()
        self.account_bar = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border=ft.Border(top=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._avatar("I", 36),
                            ft.Column(
                                [self.lbl_account_name, self.lbl_account_id],
                                spacing=1,
                                tight=True,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row([self.btn_account_mode, self.btn_floors], spacing=6, wrap=True),
                    self.lbl_link,
                ],
                spacing=6,
                tight=True,
            ),
        )
        rail_body = ft.Column(
            [
                logo,
                ft.Container(height=6),
                self.btn_connect,
                ft.Container(height=6),
                self.btn_run,
                ft.Container(height=6),
                self.btn_halt,
                ft.Container(height=6),
                self.btn_refresh,
                ft.Container(height=6),
                ft.Row([self.lbl_run_state], spacing=6),
                ft.Container(expand=True),
                self.account_bar,
            ],
            spacing=2,
            expand=True,
        )
        return ft.Container(
            width=RAIL_W,
            bgcolor=BG,
            padding=ft.Padding.only(left=8, right=12, top=4, bottom=8),
            content=rail_body,
        )

    def _surface_tab(self, key: str, label: str) -> ft.Container:
        """Primary navigation. Same pill language as the blotter tabs."""
        text = ft.Text(label, size=12, weight=ft.FontWeight.W_600, color=MUTED)
        chip = ft.Container(
            content=text,
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            border_radius=999,
            border=ft.Border.all(1, BORDER),
            ink=True,
            tooltip=NAV_SUBTITLES.get(key, ""),
            on_click=lambda _e, k=key: self._show_tab(k),
        )
        self.surface_tabs[key] = {"chip": chip, "text": text, "label": label}
        return chip

    def _sync_surface_tabs(self) -> None:
        for key, parts in self.surface_tabs.items():
            active = key == self.tab
            parts["chip"].bgcolor = HOVER if active else None
            parts["chip"].border = ft.Border.all(1, TEXT if active else BORDER)
            parts["text"].color = TEXT if active else MUTED

    def _center_column(self) -> ft.Container:
        self.surface_tabs = {}
        header = ft.Container(
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=16, top=10, bottom=8),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [self.lbl_center_title, self.lbl_center_subtitle],
                                spacing=1,
                                tight=True,
                                expand=True,
                            ),
                            self._refresh_icon(self._refresh_agent_tab),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [self._surface_tab(key, label) for key, label, _o, _f in NAV],
                        spacing=6,
                        wrap=True,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )
        self._sync_surface_tabs()
        return ft.Container(
            expand=True,
            width=None,
            bgcolor=BG,
            border=ft.Border(
                left=ft.BorderSide(1, BORDER),
                right=ft.BorderSide(1, BORDER),
            ),
            content=ft.Column(
                [header, self._status_strip(), self.content],
                spacing=0,
                expand=True,
            ),
        )

    def _status_strip(self) -> ft.Control:
        """One thin pinned band on every surface, small type.

        Only facts that change what the operator does now: is Grok looking, is the
        desk halted, what is exposed, the last say or card, and whether looks are
        burning with no ticket. Model/edge lives on Scorecard, not here.
        """
        self.health_box.content = ft.Row(
            [
                self.lbl_hs_state,
                self.lbl_hs_age,
                self.lbl_hs_next,
                self.lbl_hs_burn,
                self.lbl_hs_look,
                self.lbl_hs_link,
            ],
            spacing=12,
            wrap=True,
        )
        self.health_box.padding = ft.Padding.only(top=1, bottom=1)
        self.health_box.border_radius = 6
        return ft.Container(
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=16, top=5, bottom=5),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._stat("Grok", self.lbl_desk, self.lbl_desk_sub),
                            self._stat("Halt", self.lbl_halt),
                            self._stat("Lots", self.lbl_lot_count),
                            self._stat("Naked", self.lbl_unprotected),
                            self.lbl_last_send,
                            self.lbl_result,
                        ],
                        spacing=14,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.health_box,
                    self.col_book_strip,
                    self.lbl_alert,
                    self.lbl_banner,
                    self.lbl_lessons,
                ],
                spacing=2,
                tight=True,
            ),
        )

    def _stat(self, label: str, value: ft.Control, sub: ft.Control | None = None) -> ft.Control:
        body: list[ft.Control] = [
            ft.Text(label, size=10, color=MUTED, weight=ft.FontWeight.W_600),
            value,
        ]
        if sub is not None:
            body.append(sub)
        return ft.Row(
            body, spacing=5, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER
        )

    def _right_rail(self) -> ft.Container:
        def _ret_col(label: str, value: ft.Text, *, visible: bool = True) -> ft.Column:
            return ft.Column(
                [ft.Text(label, size=11, color=MUTED), value],
                spacing=2,
                tight=True,
                visible=visible,
            )

        self.col_ret_1w = _ret_col("1W", self.lbl_ret_1w, visible=False)
        self.col_ret_3m = _ret_col("3M", self.lbl_ret_3m, visible=False)
        self.col_ret_1y = _ret_col("1Y", self.lbl_ret_1y, visible=False)
        account_card = self._aside_card(
            "Account",
            ft.Column(
                [
                    ft.Text("Total value", size=12, color=MUTED),
                    self.lbl_equity,
                    self.lbl_equity_sub,
                    ft.Row(
                        [
                            _ret_col("Today", self.lbl_pnl),
                            _ret_col("Open", self.lbl_open_upnl),
                            self.col_ret_1w,
                            self.col_ret_3m,
                            self.col_ret_1y,
                        ],
                        spacing=16,
                        wrap=True,
                    ),
                    self.lbl_pnl_pct,
                    self.lbl_open_upnl_sub,
                    self.lbl_ret_source,
                    ft.Container(height=4),
                    ft.Row(
                        [
                            self.lbl_clock,
                            ft.Container(
                                bgcolor=BG,
                                border_radius=999,
                                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                                content=self.lbl_session_badge,
                            ),
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            self.dot_conn,
                            ft.Text("IBKR", size=12, color=TEXT, weight=ft.FontWeight.W_600),
                            self.lbl_ibkr_status,
                        ],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            self.dot_xai,
                            ft.Text("xAI", size=12, color=TEXT, weight=ft.FontWeight.W_600),
                            self.lbl_xai_status,
                        ],
                        spacing=8,
                    ),
                    ft.Row(
                        [
                            self.dot_mda,
                            ft.Text("MDA", size=12, color=TEXT, weight=ft.FontWeight.W_600),
                            self.lbl_mda_status,
                        ],
                        spacing=8,
                    ),
                    ft.Row([self.lbl_countdown_title, self.lbl_countdown], spacing=8),
                    ft.Row(
                        [ft.Text("IBKR refresh", size=11, color=MUTED), self.lbl_data_age],
                        spacing=8,
                    ),
                    ft.Row(
                        [ft.Text("Risk posture", size=11, color=MUTED), self.lbl_mandate_health],
                        spacing=8,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
        )
        news_card = self._aside_card(
            "What's happening",
            ft.Column(
                [
                    ft.Text(
                        "Headlines for your book and the broader market.",
                        size=11,
                        color=MUTED,
                    ),
                    self.news_list,
                ],
                spacing=8,
                tight=True,
            ),
        )
        return ft.Container(
            width=ASIDE_W,
            bgcolor=BG,
            padding=ft.Padding.only(left=16, right=8, top=8, bottom=12),
            content=ft.Column(
                [account_card, ft.Container(height=12), news_card],
                spacing=0,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
        )

    def _aside_card(self, title: str, body: ft.Control) -> ft.Container:
        return ft.Container(
            bgcolor=SURFACE,
            border_radius=16,
            padding=16,
            content=ft.Column(
                [ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT), body],
                spacing=10,
            ),
        )

    # -------------------------------------------------------------- nav / pages

    def _show_tab(self, key: str) -> None:
        self.tab = key if key in NAV_TITLES else "overview"
        key = self.tab
        self._sync_surface_tabs()
        self.lbl_center_title.value = NAV_TITLES.get(key, "Dashboard")
        self.lbl_center_subtitle.value = NAV_SUBTITLES.get(key, "")
        builders = {
            "overview": self._page_overview,
            "positions": self._page_positions,
            "notebook": self._page_notebook,
            "scorecard": self._page_scorecard,
            "risk": self._page_risk,
            "settings": self._page_settings,
        }
        # Leaving a page abandons whatever was half-typed on it.
        self._dirty.clear()
        self._sync_active_page(force=True)
        self.content.content = builders.get(key, self._page_overview)()
        self._safe_update()

    def _section(self, title: str, *body: ft.Control) -> ft.Container:
        """Clean center panel — hairline separator, no social-post chrome."""
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT), *body],
                spacing=8,
                tight=True,
            ),
        )

    @staticmethod
    def _refresh_icon(on_refresh, size: int = 32) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=size // 2,
            border=ft.Border.all(1, BORDER),
            bgcolor=SURFACE,
            alignment=ft.Alignment.CENTER,
            ink=True,
            tooltip="Refresh",
            on_click=on_refresh,
            content=ft.Icon(ft.Icons.REFRESH, size=16, color=TEXT),
        )

    def _section_header(self, title: str, on_refresh) -> ft.Row:
        return ft.Row(
            [
                ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=TEXT, expand=True),
                self._refresh_icon(on_refresh),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _section_refresh(self, title: str, on_refresh, *body: ft.Control) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Column(
                [self._section_header(title, on_refresh), *body],
                spacing=8,
                tight=True,
            ),
        )

    def _page_overview(self) -> ft.Control:
        """The watch surface. The stream owns it — the reason is one line under it."""
        return ft.Column(
            [
                self._stream_pane(),
                ft.Container(
                    padding=ft.Padding.only(left=16, right=16, top=5, bottom=5),
                    border=ft.Border(top=ft.BorderSide(1, BORDER)),
                    content=self.lbl_why,
                ),
                self._hidden_metrics,
            ],
            spacing=0,
            expand=True,
        )

    def _stream_pane(self) -> ft.Container:
        return ft.Container(
            expand=True,
            bgcolor="#0a0a0a",
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                "Grok stream", size=15, weight=ft.FontWeight.BOLD, color=TEXT
                            ),
                            ft.Container(expand=True),
                            self.lbl_stream_status,
                            self.btn_stream_follow,
                            self.btn_copy_stream,
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(content=self.think_scroll, expand=True),
                ],
                expand=True,
                spacing=8,
            ),
        )

    def _page_positions(self) -> ft.Control:
        heads = {
            "lots": [("lot", None), ("basis", 110), ("mtm", 58)],
            "orders": [
                ("oid", 46),
                ("name", None),
                ("side / type", 104),
                ("qty", 38),
                ("price", 104),
                ("role", 58),
            ],
            "fills": [("name", None), ("side", 56), ("qty", 44), ("price", 76)],
            "log": [("time", 60), ("what", 116), ("result", None)],
        }
        self.tab_heads = {k: self._head_row(v) for k, v in heads.items()}
        bodies: list[ft.Control] = []
        for key in ("lots", "orders", "fills", "log"):
            bodies.append(
                ft.Column(
                    [self.tab_heads[key], self.tab_bodies[key]],
                    spacing=4,
                    expand=True,
                )
            )
        self.tab_panes = dict(zip(("lots", "orders", "fills", "log"), bodies))
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            self._section_header("Book", self._refresh_book_tab),
                            ft.Row(
                                [
                                    self.tabs[k]["chip"]
                                    for k in ("lots", "orders", "fills", "log")
                                ],
                                spacing=6,
                                wrap=True,
                            ),
                        ],
                        spacing=10,
                        tight=True,
                    ),
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                    content=ft.Column(bodies, spacing=0, expand=True),
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _page_notebook(self) -> ft.Control:
        self.notebook_raw_panel = ft.Container(
            visible=False,
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            padding=10,
            content=self.lbl_notebook_body,
        )
        self._sync_notebook_page(force=True)
        return ft.Column(
            [
                self._section_refresh(
                    "Lab playbook",
                    self._refresh_notebook_tab,
                    self.lbl_notebook_head,
                    self.lbl_notebook_meta,
                    self.lbl_notebook_lots,
                    self.lbl_nb_playbook,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    content=ft.Column(
                        [
                            ft.Text(
                                "Setup cards", size=15, weight=ft.FontWeight.BOLD, color=TEXT
                            ),
                            self.col_notebook_cards,
                            self.notebook_raw_panel,
                            ft.Text(
                                "Order-type coverage",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT,
                            ),
                            self.lbl_notebook_types,
                            self.col_notebook_types,
                        ],
                        spacing=10,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _page_scorecard(self) -> ft.Control:
        self._sync_scorecard_page(force=True)
        return ft.Column(
            [
                self._section_refresh(
                    "Book vs model",
                    self._refresh_scorecard_tab,
                    ft.Row(
                        [
                            ft.Column(
                                [ft.Text("NetLiq", size=12, color=MUTED), self.lbl_sc_netliq],
                                spacing=2,
                                tight=True,
                            ),
                            ft.Container(width=28),
                            ft.Column(
                                [
                                    ft.Text("Edge", size=12, color=MUTED),
                                    self.lbl_edge,
                                    self.lbl_edge_sub,
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                    self.lbl_sc_verdict,
                    self.lbl_sc_score,
                    self.lbl_sc_session,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.only(bottom=12),
                    content=ft.Column(
                        [
                            self._section("Windows", self.col_sc_windows),
                            self._section(
                                "Setup card scores",
                                ft.Text(
                                    "Sends tied to the card that called them, "
                                    "with realized P&L from fills.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.col_sc_cards,
                            ),
                            self._section(
                                "Playbook revisions",
                                ft.Text(
                                    "Edge stamped when the card was written → edge when "
                                    "it was replaced.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.col_sc_ledger,
                            ),
                            self._section(
                                "Path",
                                self.lbl_sc_path,
                                self.lbl_sc_mix,
                                self.lbl_sc_strats,
                            ),
                        ],
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _page_risk(self) -> ft.Control:
        self._sync_risk_page(force=True)

        def _posture(name: str) -> ft.Control:
            return ft.Container(
                content=ft.Text(name, size=12, weight=ft.FontWeight.W_600, color=TEXT),
                padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                border=ft.Border.all(1, BORDER),
                border_radius=999,
                ink=True,
                on_click=lambda _e, n=name: self._set_risk_posture(n),
            )

        return ft.Column(
            [
                self._section_refresh(
                    "Posture",
                    self._refresh_risk_tab,
                    self.lbl_risk_posture,
                    ft.Row(
                        [_posture("defensive"), _posture("balanced"), _posture("aggressive")],
                        spacing=6,
                        wrap=True,
                    ),
                    self.lbl_risk_status,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.only(bottom=12),
                    content=ft.Column(
                        [
                            self._section(
                                "Floor",
                                ft.Text(
                                    "Editable and persisted to risk_settings.json. Enter or the "
                                    "check applies. Tighten only — a value that would weaken the "
                                    "walk-away floor is clamped and reported.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.col_risk_knobs,
                            ),
                            self._section(
                                "Size floors",
                                self._field_row(
                                    "sizing_floors",
                                    "Percent size floors",
                                    "paper may size freely — live forces on",
                                    control=self.sw_size_floors,
                                ),
                                self.lbl_risk_floors,
                            ),
                            self._section(
                                "Halt",
                                self.lbl_risk_halt_state,
                                self.lbl_risk_halt_math,
                                ft.Text(
                                    "Halt / Resume is the rail button. Exits always bypass it.",
                                    size=11,
                                    color=MUTED,
                                ),
                            ),
                            self._section(
                                "As persisted",
                                ft.Text(
                                    "What the floor accepted after clamping.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.lbl_risk_glance,
                            ),
                        ],
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _page_settings(self) -> ft.Control:
        """Agent settings the operator owns. Mode/port stay on the gated path."""
        self._sync_settings_page(force=True)
        return ft.Column(
            [
                self._section_refresh(
                    "Brain",
                    self._refresh_settings_tab,
                    ft.Text(
                        "Which Grok takes the look. Strategy stays Grok's — this does "
                        "not touch the prompt or the playbook.",
                        size=11,
                        color=MUTED,
                    ),
                    *[self._field_row(k, label, hint) for k, label, hint in BRAIN_FIELDS],
                    self.lbl_settings_brain,
                    self.lbl_settings_status,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.only(bottom=12),
                    content=ft.Column(
                        [
                            self._section(
                                "Data and pacing",
                                ft.Text(
                                    "How often the background monitor polls, reviews "
                                    "and halts on a dead broker link. Session look/"
                                    "token caps stop a flat grind — hit stays idle, "
                                    "chat kept. Scan depth is self_tune. Stay-up has "
                                    "no sit clock.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self._field_row(
                                    "monitor_enabled",
                                    "Portfolio monitor",
                                    "off stops the background poll loop",
                                    control=self.gates["monitor_enabled"],
                                ),
                                *[
                                    self._field_row(k, label, hint)
                                    for k, label, hint in PACING_FIELDS
                                ],
                                self._field_row(
                                    "monitor_extended_hours",
                                    "Review premarket / postmarket",
                                    "monitor reviews outside RTH",
                                    control=self.gates["monitor_extended_hours"],
                                ),
                            ),
                            self._section(
                                "Connection",
                                ft.Text(
                                    "Paper / live and the port are one decision behind the "
                                    "confirm phrase — they are not fields here.",
                                    size=11,
                                    color=MUTED,
                                ),
                                ft.Row(
                                    [
                                        ft.Text("Mode", size=12, color=TEXT, expand=True),
                                        self.lbl_settings_mode,
                                        self._btn(
                                            "Switch paper / live",
                                            outlined=True,
                                            on_click=self._toggle_trading_mode,
                                        ),
                                    ],
                                    spacing=8,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                self.lbl_settings_link,
                                *[self._field_row(k, label, hint) for k, label, hint in LINK_FIELDS],
                            ),
                            self._section("Persisted to", self.lbl_settings_path),
                        ],
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )

    # ----------------------------------------------------------------- helpers

    def _safe_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass

    def _toast(self, msg: str, *, color: str = BLUE) -> None:
        keep = [c for c in (self.page.overlay or []) if not isinstance(c, ft.SnackBar)]
        bar = ft.SnackBar(ft.Text(msg), bgcolor=color, open=True)
        try:
            self.page.overlay = keep + [bar]
        except Exception:
            self.page.overlay.append(bar)

    def _set_btn_text(
        self,
        btn: ft.Button,
        text: str,
        *,
        filled: bool = False,
        danger: bool = False,
        outlined: bool = False,
    ) -> None:
        btn.text = text
        btn.content = text
        if danger:
            btn.bgcolor = BG
            btn.color = RED
            btn.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                padding=ft.Padding.symmetric(horizontal=18, vertical=11),
                side=ft.BorderSide(1, RED),
            )
            return
        btn.bgcolor = WHITE if filled else BG
        btn.color = "#0f1419" if filled else TEXT
        btn.style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=999),
            padding=ft.Padding.symmetric(horizontal=18, vertical=11),
            side=None if filled else ft.BorderSide(1, BORDER),
        )

    def _toggle_run(self, _=None) -> None:
        s = self.engine.state
        running = bool(s.running) and getattr(s, "autonomous", False) and not getattr(s, "paused", False)
        if running:
            self.engine.pause_engine()
            self._toast("Agent stopped — IBKR stays connected", color=AMBER)
        else:
            err = self.engine.start()
            self._toast(err or "Starting Grok", color=RED if err else BLUE)
        self._sync_widgets()
        self._safe_update()

    def _toggle_connect(self, _=None) -> None:
        logger.info("Connect IBKR clicked")
        s = self.engine.state
        linked = bool(s.connected) or (
            self.engine.worker is not None and self.engine.worker.is_alive()
        )
        if linked:
            self._open_disconnect_confirm_dialog()
            return
        err = self.engine.connect_broker()
        self._toast(err or "Connecting to IBKR…", color=RED if err else BLUE)
        self._sync_widgets()
        self._safe_update()

    def _lab_notebook(self) -> tuple[str, str]:
        """Current lab notebook. Read-only. Notebook, not law."""
        try:
            from abcxauto.lab_playbook import load_lab

            pb = load_lab()
        except Exception:
            return "Lab notebook — unreadable", "(could not load playbook_lab.json)"
        pb = pb if isinstance(pb, dict) else {}
        inst = str(pb.get("instructions") or "").strip()
        rev = pb.get("revision") if pb.get("revision") not in (None, "") else "—"
        mode = str(pb.get("mode") or "explore").strip() or "explore"
        head = f"rev={rev} mode={mode} — notebook, not law"
        return head, inst or "(empty)"

    def _open_disconnect_confirm_dialog(self) -> None:
        s = self.engine.state

        def _cancel(_=None) -> None:
            dlg.open = False
            self._safe_update()

        def _confirm(_=None) -> None:
            dlg.open = False
            if getattr(s, "autonomous", False) and s.running:
                self.engine.pause_engine()
            self.engine.stop_engine()
            self._toast("Disconnected from IBKR", color=AMBER)
            self._sync_widgets()
            self._safe_update()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            title=ft.Text("Disconnect IBKR?", color=TEXT),
            content=ft.Text(
                "Stops the agent and the IBKR link. Positions and orders stay at the broker.",
                size=13,
                color=MUTED,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_cancel),
                ft.TextButton("Disconnect", on_click=_confirm, style=ft.ButtonStyle(color=RED)),
            ],
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _toggle_halt(self, _=None) -> None:
        try:
            from abcxauto.risk_gates import get_risk_gate

            gate = get_risk_gate()
            if gate.is_halted:
                gate.resume()
                self._toast("Halt cleared — new risk allowed", color=GREEN)
            else:
                gate.halt("manual halt from console", kind="halt")
                self._toast("New risk halted", color=AMBER)
        except Exception as exc:
            self._toast(f"Halt failed: {exc}", color=RED)
        self._sync_widgets()
        self._sync_risk_page(force=True)
        self._safe_update()

    def _refresh_score_line(self) -> None:
        if not self.engine.state.equity:
            # Scoring a zero book returns -100% and a nonsense edge. Say nothing instead.
            self.lbl_score.value = "Score: — no live book"
            self.lbl_score.color = MUTED
            self.lbl_session_score.value = "sess —"
            self.lbl_session_score.color = MUTED
            self._sc_last = {}
            self._sync_edge_stat({})
            return
        try:
            from abcxauto.scorecard import compute_scorecard

            sc = compute_scorecard(equity=self.engine.state.equity)
        except Exception:
            self.lbl_score.value = "Score: —"
            self.lbl_score.color = MUTED
            self.lbl_session_score.value = "sess —"
            self.lbl_session_score.color = MUTED
            self._sc_last = {}
            self._sync_edge_stat({})
            return
        self._sc_last = sc if isinstance(sc, dict) else {}
        self._sync_edge_stat(sc)
        self._sync_session_score(sc)

        def _bit(tag: str, ret: Any, edge: Any, beat: Any) -> str:
            ret_s = f"{ret:+.2f}%" if ret is not None else "—"
            edge_s = f"{edge:+.2f}" if edge is not None else "—"
            if beat is True:
                mark = "BEAT"
            elif beat is False:
                mark = "behind"
            else:
                mark = "…"
            return f"{tag} {ret_s} e{edge_s} {mark}"

        focus = sc.get("fastest_beating") or sc.get("best_pace")
        win = (sc.get("windows") or {}).get(focus) if focus else None
        all_bit = _bit(
            "all",
            sc.get("book_return_pct"),
            sc.get("edge_usd"),
            sc.get("beating_model"),
        )
        if (
            isinstance(win, dict)
            and focus
            and focus != "inception"
            and win.get("book_return_pct") is not None
        ):
            short_bit = _bit(
                str(focus),
                win.get("book_return_pct"),
                win.get("edge_usd"),
                win.get("beating_model"),
            )
            self.lbl_score.value = f"Score: {short_bit}  ·  {all_bit}"
            beat = win.get("beating_model")
        else:
            self.lbl_score.value = f"Score: {all_bit}"
            beat = sc.get("beating_model")
        if beat is True or sc.get("beating_model") is True:
            self.lbl_score.color = GREEN
        elif beat is False or sc.get("beating_model") is False:
            self.lbl_score.color = AMBER
        else:
            self.lbl_score.color = MUTED

    def _brief(self) -> dict:
        """Last completed look. Shown only until the live book arrives."""
        now = time.monotonic()
        if now - float(getattr(self, "_brief_last", 0) or 0) >= 5.0 or not self._brief_row:
            self._brief_last = now
            try:
                from abcxauto.think_stream import load_desk_brief

                row = load_desk_brief()
                self._brief_row = row if isinstance(row, dict) else {}
            except Exception:
                self._brief_row = {}
        return self._brief_row

    @staticmethod
    def _brief_age(row: dict) -> str:
        ts = str((row or {}).get("ts") or "")
        if not ts:
            return "last look"
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            return "last look"
        try:
            return f"last look {when.astimezone().strftime('%H:%M')}"
        except (OSError, OverflowError, ValueError):
            return "last look"

    @staticmethod
    def _brief_lot_rows(labels: list | None) -> list[dict]:
        """``IWM 260821C306.0 long 1 -26%`` from the brief → the same row shape as live lots."""
        rows: list[dict] = []
        for raw in labels or []:
            text = str(raw or "").strip()
            if not text:
                continue
            ident, pct = text, None
            head, _, tail = text.rpartition(" ")
            if head and tail.endswith("%"):
                try:
                    pct = float(tail.rstrip("%"))
                    ident = head
                except ValueError:
                    ident, pct = text, None
            rows.append({
                "ident": ident,
                "symbol": ident.split(" ")[0].upper(),
                "sec": "OPT",
                "qty": 0.0,
                "avg": None,
                "mkt": None,
                "mtm_pct": pct,
                "unprotected": False,
            })
        return rows

    def _sync_session_score(self, scorecard: dict) -> None:
        sess = scorecard.get("session") if isinstance(scorecard, dict) else None
        if not isinstance(sess, dict) or not sess:
            self.lbl_session_score.value = "sess —"
            self.lbl_session_score.color = MUTED
            return
        pnl = sess.get("book_pnl")
        cost = sess.get("model_cost_usd")
        edge = sess.get("edge_usd")
        fills = sess.get("fills")
        wins = sess.get("wins")
        pnl_s = f"{pnl:+.0f}" if isinstance(pnl, (int, float)) else "—"
        cost_s = f"{cost:.2f}" if isinstance(cost, (int, float)) else "—"
        edge_s = f"{edge:+.0f}" if isinstance(edge, (int, float)) else "—"
        fill_s = f"{wins}/{fills}" if fills not in (None, 0) else f"{fills or 0}"
        self.lbl_session_score.value = (
            f"sess ΔNL={pnl_s} model$={cost_s} edge={edge_s} fills={fill_s}"
        )
        if isinstance(edge, (int, float)) and edge > 0:
            self.lbl_session_score.color = GREEN
        elif isinstance(edge, (int, float)):
            self.lbl_session_score.color = AMBER
        else:
            self.lbl_session_score.color = MUTED

    def _sync_edge_stat(self, scorecard: dict) -> None:
        sc = scorecard if isinstance(scorecard, dict) else {}
        edge = sc.get("edge_usd")
        beat = sc.get("beating_model")
        if isinstance(edge, (int, float)):
            self.lbl_edge.value = f"${edge:+,.0f}"
            self.lbl_edge.color = GREEN if beat is True else (AMBER if beat is False else TEXT)
        else:
            self.lbl_edge.value = "—"
            self.lbl_edge.color = MUTED
        cost = sc.get("model_cost_usd")
        self.lbl_edge_sub.value = (
            f"model ${cost:,.2f}" if isinstance(cost, (int, float)) else "vs model"
        )

    @staticmethod
    def _lot_view(positions: list | None, unprotected: list | None = None) -> list[dict]:
        """One row per lot: identity, MTM %, protection. Attention first."""
        from abcxauto.world_state import compact_position, lot_ident

        naked = {str(x).strip().upper() for x in (unprotected or []) if str(x).strip()}
        rows: list[dict] = []
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            row = compact_position(p, extra=True)
            try:
                qty = float(row.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if abs(qty) < 1e-9:
                continue
            sec = str(row.get("sec") or "STK").upper()
            sym = str(row.get("symbol") or "").upper()
            ident = lot_ident(p)
            ident_u = ident.upper()
            rows.append({
                "ident": ident,
                "symbol": sym,
                "sec": sec,
                "qty": qty,
                "avg": row.get("avg"),
                "mkt": row.get("mkt"),
                "mtm_pct": row.get("mtm_pct"),
                "unprotected": ident_u in naked or (sec.startswith("STK") and sym in naked),
            })
        rows.sort(
            key=lambda r: (
                not r["unprotected"],
                r["mtm_pct"] if isinstance(r["mtm_pct"], (int, float)) else 0.0,
            )
        )
        return rows

    @staticmethod
    def _lot_mark_pct(row: dict) -> float | None:
        """MTM % from live avg → mkt. Do not use a pre-rounded 0% suffix."""
        avg, mkt = row.get("avg"), row.get("mkt")
        try:
            avg_f = float(avg)
            mkt_f = float(mkt)
        except (TypeError, ValueError):
            mtm = row.get("mtm_pct")
            return float(mtm) if isinstance(mtm, (int, float)) else None
        if abs(avg_f) < 1e-12:
            return None
        try:
            qty_f = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty_f = 0.0
        if qty_f < 0:
            return (avg_f - mkt_f) / abs(avg_f) * 100.0
        return (mkt_f - avg_f) / abs(avg_f) * 100.0

    def _lot_control(self, row: dict) -> ft.Control:
        mtm = self._lot_mark_pct(row)
        if isinstance(mtm, (int, float)):
            mtm_txt = f"{mtm:+.2f}%"
            if mtm < 0:
                mtm_color = RED
            elif mtm > 0:
                mtm_color = GREEN
            else:
                mtm_color = MUTED
        else:
            mtm_txt, mtm_color = "—", MUTED
        avg, mkt = row.get("avg"), row.get("mkt")
        basis = (
            f"{avg:g} → {mkt:g}"
            if isinstance(avg, (int, float)) and isinstance(mkt, (int, float))
            else ""
        )
        cells: list[ft.Control] = [
            ft.Container(
                expand=True,
                content=ft.Text(
                    row.get("ident") or "?",
                    size=12,
                    color=TEXT,
                    font_family="Consolas",
                    no_wrap=True,
                ),
            ),
            ft.Container(
                width=110,
                content=ft.Text(basis, size=11, color=MUTED, text_align=ft.TextAlign.RIGHT),
            ),
            ft.Container(
                width=58,
                content=ft.Text(
                    mtm_txt,
                    size=12,
                    weight=ft.FontWeight.W_600,
                    color=mtm_color,
                    text_align=ft.TextAlign.RIGHT,
                ),
            ),
        ]
        if row.get("unprotected"):
            cells.append(ft.Text("naked", size=11, weight=ft.FontWeight.W_600, color=RED))
        return ft.Container(
            content=ft.Row(cells, spacing=6),
            padding=ft.Padding.symmetric(horizontal=8, vertical=5),
            border_radius=6,
            bgcolor=SURFACE if row.get("unprotected") else BG,
            border=ft.Border.all(1, RED if row.get("unprotected") else BORDER),
        )

    def _sync_lots(self) -> None:
        s = self.engine.state
        book = getattr(s, "portfolio", None) or {}
        naked = book.get("unprotected_symbols") if isinstance(book, dict) else None
        rows = self._lot_view(s.positions, naked)
        stale = ""
        if not rows and not s.equity:
            brief = self._brief()
            rows = self._brief_lot_rows(brief.get("open_lots"))
            stale = f"{self._brief_age(brief)} — not live" if rows else ""
        key = json.dumps([rows, stale], sort_keys=True, default=str)
        if key == self._lots_key:
            return
        self._lots_key = key
        if not rows:
            self.lbl_positions.value = "No open positions"
            self.col_lots.controls = [self.lbl_positions]
            return
        controls: list[ft.Control] = [self._lot_control(r) for r in rows]
        if stale:
            controls.insert(0, ft.Text(stale, size=11, color=AMBER))
        self.col_lots.controls = controls

    def _sync_orders(self) -> None:
        """One row per working order — role/covers is why it exists."""
        from abcxauto.world_state import compact_working_orders

        s = self.engine.state
        try:
            rows = compact_working_orders(s.open_orders or [], positions=s.positions)
        except Exception:
            rows = []
        key = json.dumps(rows, sort_keys=True, default=str)
        if key == self._orders_key:
            return
        self._orders_key = key
        if not rows:
            self.col_orders.controls = [self.lbl_working_orders]
            return
        controls: list[ft.Control] = []
        for o in rows:
            sec = str(o.get("sec") or "STK").upper()
            name = str(o.get("symbol") or "?")
            if sec.startswith("OPT"):
                bits = [str(o.get("right") or ""), str(o.get("strike") or "")]
                exp = str(o.get("expiration") or "")
                name = f"{name} {''.join(bits)} {exp}".strip()
            else:
                name = f"{name} {sec}"
            px = ""
            if o.get("stop") not in (None, ""):
                px = f"stop {o['stop']}"
            if o.get("lmt") not in (None, ""):
                px = f"{px}  lmt {o['lmt']}".strip()
            role = str(o.get("role") or "")
            covers = str(o.get("covers") or "")
            controls.append(
                self._blotter_row([
                    self._cell(str(o.get("order_id") or "?"), width=46, color=MUTED, mono=True),
                    self._cell(name, expand=True, mono=True),
                    self._cell(
                        f"{o.get('action') or ''} {o.get('type') or ''}".strip(),
                        width=104,
                        color=TEXT,
                    ),
                    self._cell(
                        str(o.get("qty") if o.get("qty") is not None else "?"),
                        width=38,
                        right=True,
                    ),
                    self._cell(px or "—", width=104, color=MUTED, right=True),
                    self._cell(
                        role or "—",
                        width=58,
                        color=GREEN if role == "exit" else MUTED,
                        right=True,
                    ),
                ])
            )
            if covers:
                controls.append(
                    ft.Container(
                        padding=ft.Padding.only(left=14, bottom=2),
                        content=ft.Text(f"covers {covers}", size=11, color=MUTED),
                    )
                )
        self.col_orders.controls = controls

    def _sync_fills(self) -> None:
        s = self.engine.state
        fills = list(getattr(s, "recent_fills", None) or [])[:20]
        key = json.dumps(fills, sort_keys=True, default=str)
        if key == self._fills_key:
            return
        self._fills_key = key
        if not fills:
            self.col_fills.controls = [self.lbl_recent_fills]
            return
        controls: list[ft.Control] = []
        for f in reversed(fills):
            side = str(f.get("side") or f.get("action") or "")
            controls.append(
                self._blotter_row([
                    self._cell(str(f.get("symbol") or "?"), expand=True, mono=True),
                    self._cell(
                        side,
                        width=56,
                        color=GREEN if side.upper().startswith("B") else RED,
                        weight=ft.FontWeight.W_600,
                    ),
                    self._cell(
                        str(f.get("quantity") or f.get("shares") or ""), width=44, right=True
                    ),
                    self._cell(
                        str(f.get("price") or f.get("avg_price") or ""),
                        width=76,
                        right=True,
                        color=MUTED,
                    ),
                ])
            )
        self.col_fills.controls = controls

    def _sync_scan_tape(self) -> None:
        """The screen Grok pulled: IBKR rank order plus whatever got a live last."""
        hits = getattr(self.engine.state, "scan_hits", None) or {}
        rows = [r for r in (hits.get("rows") or []) if isinstance(r, dict)]
        key = json.dumps([hits.get("source"), hits.get("quoted"), rows], sort_keys=True, default=str)
        if key == self._scan_key:
            return
        self._scan_key = key
        if not rows:
            self.lbl_scan_head.value = "No screen this session — Grok runs the scanner."
            self.lbl_scan_head.color = MUTED
            self.col_scan.controls = []
            return
        ranked = bool(hits.get("ranked"))
        code = str(hits.get("scan_code") or hits.get("arena") or "").strip()
        bits = [f"{len(rows)} hits", f"{int(hits.get('quoted') or 0)} quoted"]
        if code:
            bits.append(code)
        bits.append(str(hits.get("source") or "?"))
        bits.append(str(hits.get("rank_meaning") or ("ranked" if ranked else "not ranked")))
        self.lbl_scan_head.value = " · ".join(bits)
        self.lbl_scan_head.color = TEXT
        controls: list[ft.Control] = [
            self._head_row([("#", 30), ("symbol", 78), ("last", 84), ("metric", None)])
        ]
        for i, row in enumerate(rows[:12], start=1):
            rank = row.get("rank") if ranked else None
            last = row.get("last")
            metric = row.get("distance") or row.get("benchmark") or row.get("projection") or ""
            tags = []
            if row.get("on_book"):
                tags.append("on book")
            if row.get("in_turn"):
                tags.append("quoted this look")
            note = " · ".join(str(x) for x in ([metric] if metric else []) + tags)
            controls.append(
                self._blotter_row([
                    self._cell(
                        str(rank if rank not in (None, "") else i),
                        width=30,
                        color=MUTED,
                        mono=True,
                    ),
                    self._cell(
                        str(row.get("symbol") or "?"),
                        width=78,
                        mono=True,
                        weight=ft.FontWeight.W_600,
                        color=GREEN if row.get("on_book") else TEXT,
                    ),
                    self._cell(
                        f"{last:,.2f}" if isinstance(last, (int, float)) else "—",
                        width=84,
                        right=True,
                        color=TEXT if isinstance(last, (int, float)) else MUTED,
                        mono=True,
                    ),
                    self._cell(note or "—", expand=True, color=MUTED),
                ])
            )
        self.col_scan.controls = controls

    def _sync_lessons_line(self) -> None:
        """Structures the clerk refused recently. One line, newest first."""
        lessons = [x for x in (getattr(self.engine.state, "structure_lessons", None) or [])
                   if isinstance(x, dict)]
        if not lessons:
            self.lbl_lessons.visible = False
            self.lbl_lessons.value = ""
            return
        bits: list[str] = []
        for ev in lessons[:3]:
            head = " ".join(
                str(ev.get(k) or "").strip()
                for k in ("strategy", "symbol")
                if str(ev.get(k) or "").strip()
            )
            why = str(ev.get("reason_code") or ev.get("message") or "").strip()
            bits.append(f"{head} {why}".strip() or "—")
        self.lbl_lessons.value = "Lessons: " + " · ".join(bits)
        self.lbl_lessons.visible = True

    @staticmethod
    def _is_note(rec: dict) -> bool:
        """Lifecycle line from ProEngine._note — the message is the whole row.

        Keyed on shape, not a kind whitelist: RETRY / PARK / UNIVERSE notes were
        silently blanked when the list did not name them.
        """
        return bool(rec.get("msg")) and not rec.get("result") and not rec.get("action_obj")

    def _sync_activity(self) -> None:
        s = self.engine.state
        records = list(s.records or [])[-40:]
        key = str(len(s.records or [])) + json.dumps(
            [r.get("ts") for r in records[-3:]], default=str
        )
        if key == self._activity_key:
            return
        self._activity_key = key
        if not records:
            self.col_activity.controls = [self.lbl_activity]
            return
        controls: list[ft.Control] = []
        for r in reversed(records):
            kind = str(r.get("type") or "cycle").lower()
            ts = str(r.get("ts") or "")
            ts = ts.split("T", 1)[-1][:8] if "T" in ts else ts[-8:]
            if self._is_note(r):
                what = kind.upper()
                result = str(r.get("msg") or "—")
                color = NOTE_COLOR.get(kind, MUTED)
            else:
                what = str(
                    r.get("strat") or (r.get("action_obj") or {}).get("strategy") or "—"
                )
                result = self._format_result_status(r.get("result") or {})
                color = (
                    RED
                    if result.lower().startswith(("blocked", "rejected", "fail", "error"))
                    else TEXT
                )
            controls.append(
                self._blotter_row([
                    self._cell(ts, width=60, color=MUTED, mono=True),
                    self._cell(what, width=116, color=color, weight=ft.FontWeight.W_600),
                    self._cell(result, expand=True, color=MUTED),
                ])
            )
        self.col_activity.controls = controls

    def _sync_tabs(self) -> None:
        s = self.engine.state
        lots_n = len(s.positions or [])
        if not lots_n and not s.equity:
            lots_n = len(self._brief().get("open_lots") or [])
        counts = {
            "lots": lots_n,
            "orders": len(s.open_orders or []),
            "fills": len(getattr(s, "recent_fills", None) or []),
            "log": len(s.records or []),
        }
        panes = getattr(self, "tab_panes", None) or {}
        for key, tab in self.tabs.items():
            on = key == self._tab
            n = int(counts.get(key, 0))
            tab["text"].color = TEXT if on else MUTED
            tab["count"].value = str(n) if n else ""
            tab["count"].color = TEXT if on else MUTED
            tab["chip"].bgcolor = SURFACE if on else BG
            tab["chip"].border = ft.Border.all(1, MUTED if on else BORDER)
            body = self.tab_bodies.get(key)
            if body is not None:
                body.visible = on
            pane = panes.get(key)
            if pane is not None:
                pane.visible = on

    def _sync_mix_line(self) -> None:
        from abcxauto.world_state import concentration, format_mix, structure_mix

        s = self.engine.state
        positions = list(s.positions or [])
        if not positions and not s.equity:
            mix = format_mix(self._brief().get("mix"))
            self.lbl_mix.value = f"Mix: {mix}" if mix else "Mix: flat"
            self.lbl_mix.color = MUTED
            return
        bits = []
        mix = format_mix(structure_mix(positions))
        if mix:
            bits.append(mix)
        conc = concentration(positions)
        if conc.get("names"):
            bits.append(f"{conc['names']} names")
        self.lbl_mix.value = f"Mix: {' · '.join(bits)}" if bits else "Mix: flat"
        self.lbl_mix.color = TEXT if bits else MUTED

    def _sync_path_line(self) -> None:
        if not self.engine.state.equity:
            self.lbl_path.value = "Path: —"
            self.lbl_path.color = MUTED
            return
        try:
            from abcxauto.memory import get_journal
            from abcxauto.path_math import path_from_journal

            facts = path_from_journal(
                get_journal(),
                equity=self.engine.state.equity,
                risk_pct=getattr(get_config(), "max_risk_per_trade_pct", None),
            )
        except Exception:
            facts = {}
        self.lbl_path.value = self._path_line(facts)
        f, kelly = facts.get("f"), facts.get("kelly")
        over = (
            isinstance(f, (int, float))
            and isinstance(kelly, (int, float))
            and kelly > 0
            and f > kelly
        )
        self.lbl_path.color = AMBER if over else (TEXT if facts.get("n") else MUTED)

    @staticmethod
    def _path_line(facts: dict | None) -> str:
        row = facts if isinstance(facts, dict) else {}
        n = int(row.get("n") or 0)
        if n < 4:
            return f"Path: {n} closed fills — thin sample"
        bits = [f"n{n}"]
        if isinstance(row.get("E"), (int, float)):
            bits.append(f"E${row['E']:+,.0f}")
        if isinstance(row.get("p"), (int, float)):
            bits.append(f"win {row['p'] * 100:.0f}%")
        kelly = row.get("kelly")
        edge = isinstance(kelly, (int, float)) and kelly > 0
        if edge:
            bits.append(f"kelly {kelly * 100:.1f}%")
        else:
            bits.append("kelly none — no edge yet")
        if isinstance(row.get("f"), (int, float)):
            bits.append(f"f {row['f'] * 100:.1f}%")
        # Ruin only reads as a number once there is an edge to survive.
        if edge and isinstance(row.get("ruin"), (int, float)):
            bits.append(f"ruin {row['ruin'] * 100:.1f}%")
        if n < 20:
            bits.append("thin")
        return "Path: " + " · ".join(bits)

    def _copy_stream(self, _=None) -> None:
        text = str(getattr(self.engine.state, "think_live", "") or self.think_live.value or "")
        try:
            self.page.set_clipboard(text[-8000:])
            self._toast("Stream copied", color=BLUE)
        except Exception as exc:
            self._toast(f"Copy failed: {exc}", color=AMBER)

    def _refresh_book(self, _=None) -> None:
        err = self.engine.request_snapshot()
        self.engine.drain_apply()
        self._toast(err or "Refreshing book…", color=AMBER if err else BLUE)
        self._sync_widgets()
        self._safe_update()

    def _refresh_book_tab(self, _=None) -> None:
        self._refresh_book()

    def _refresh_agent_tab(self, _=None) -> None:
        try:
            pulse = self.engine.state.reality_pulse or build_reality_pulse(
                ibkr_connected=self.engine.state.connected,
                positions=self.engine.state.positions,
                account=None,
            )
            self._apply_clock(pulse)
        except Exception:
            pass
        self.engine.drain_apply()
        err = self.engine.request_snapshot()
        self._toast(
            "Dashboard refreshed (broker snapshot unavailable)" if err else "Refreshing…",
            color=AMBER if err else BLUE,
        )
        self._sync_widgets()
        self._safe_update()

    def _refresh_scorecard_tab(self, _=None) -> None:
        self._score_last = 0.0
        self._path_last = 0.0
        self._refresh_score_line()
        self._sync_path_line()
        self._sync_scorecard_page(force=True)
        self._safe_update()

    def _refresh_notebook_tab(self, _=None) -> None:
        self._sync_notebook_page(force=True)
        self._safe_update()

    def _refresh_risk_tab(self, _=None) -> None:
        self._sync_risk_page(force=True)
        self._safe_update()

    def _start(self, _=None) -> None:
        err = self.engine.start()
        if err:
            self._toast(err, color=RED)
        self._sync_widgets()
        self._safe_update()

    # ------------------------------------------------------------- notebook

    def _sync_notebook_page(self, *, force: bool = False) -> None:
        """Setup cards are the book. Fall back to the TYPE tree / prose."""
        try:
            from abcxauto.lab_playbook import (
                load_lab,
                notebook_text,
                playbook_age_hours,
                unknown_card_tickets,
            )
        except Exception:
            return
        try:
            lab = load_lab()
        except Exception:
            lab = {}
        lab = lab if isinstance(lab, dict) else {}
        cards = self._notebook_setup_cards(lab)
        body = ""
        try:
            body = notebook_text(lab) or ""
        except Exception:
            body = self._lab_notebook()[1]
        key = json.dumps([lab.get("revision"), lab.get("written_at"), len(body)], default=str)
        if not force and key == self._notebook_key:
            return
        self._notebook_key = key
        rev = lab.get("revision") if lab.get("revision") not in (None, "") else "—"
        mode = str(lab.get("mode") or "explore").strip() or "explore"
        if lab.get("promoted"):
            state, state_color = "promoted", GREEN
        elif lab.get("ready_to_promote"):
            state, state_color = "ready to promote", AMBER
        else:
            state, state_color = "lab", MUTED
        self.lbl_notebook_head.value = f"rev {rev} · {mode} · {state}"
        self.lbl_notebook_head.color = state_color
        try:
            age = playbook_age_hours(lab)
        except Exception:
            age = None
        age_s = f"{age:.1f}h old" if isinstance(age, (int, float)) else "age —"
        try:
            unsendable = unknown_card_tickets(cards)
        except Exception:
            unsendable = []
        self.lbl_notebook_meta.value = (
            f"{age_s} · {len(cards)} setup card(s) · {len(body):,} chars · playbook, not law"
            + (f" · UNSENDABLE ticket: {', '.join(unsendable)}" if unsendable else "")
        )
        self.lbl_notebook_meta.color = RED if unsendable else MUTED
        lots = [str(x) for x in (lab.get("lots_at_write") or [])][:8]
        self.lbl_notebook_lots.value = (
            f"lots at write: {', '.join(lots)}" if lots else "lots at write: none"
        )
        try:
            from abcxauto.lab_playbook import card_facts

            attrib = {
                str(r.get("card") or "").lower(): r
                for r in (card_facts(lab) or [])
                if isinstance(r, dict)
            }
        except Exception:
            attrib = {}
        if cards:
            self.col_notebook_cards.controls = [
                self._notebook_card(c, attrib) for c in cards
            ]
            self.notebook_raw_panel.visible = False
            self.lbl_notebook_body.value = ""
        else:
            self.col_notebook_cards.controls = [
                ft.Text(
                    "No setup cards yet — Grok writes them with write_lab_playbook.",
                    size=12,
                    color=MUTED,
                )
            ]
            self.lbl_notebook_body.value = body or "(empty)"
            self.notebook_raw_panel.visible = True
        self._sync_notebook_types(lab)
        self.lbl_nb_playbook.value = self.lbl_playbook.value
        self.lbl_nb_playbook.color = self.lbl_playbook.color
        self.lbl_nb_playbook.tooltip = self.lbl_playbook.tooltip

    def _notebook_setup_cards(self, lab: dict) -> list[dict]:
        """Nested type cards first. A leftover flat list is the fallback."""
        out: list[dict] = []
        try:
            from abcxauto.lab_playbook import walk_cards

            for type_name, card in walk_cards(lab):
                if not isinstance(card, dict) or not card.get("name"):
                    continue
                row = dict(card)
                if type_name and not row.get("ticket"):
                    row["ticket"] = type_name
                out.append(row)
        except Exception:
            out = []
        if out:
            return out
        return [c for c in (lab.get("cards") or []) if isinstance(c, dict) and c.get("name")]

    def _sync_notebook_types(self, lab: dict) -> None:
        """Every sendable trunk, filled or not. A gap here is Grok's to write."""
        try:
            from abcxauto.lab_playbook import type_coverage

            rows = type_coverage(lab) or []
        except Exception:
            rows = []
        if not rows:
            self.lbl_notebook_types.value = "order types unavailable"
            self.col_notebook_types.controls = []
            return
        filled = [r for r in rows if r.get("touched")]
        self.lbl_notebook_types.value = (
            f"{len(filled)}/{len(rows)} sendable types touched · "
            "a trunk appears once Grok learns something under it"
        )
        out: list[ft.Control] = [
            self._head_row([("order type", None), ("cards", 52), ("learned", 190)])
        ]
        for row in rows:
            learned = ", ".join(str(x) for x in (row.get("learned") or []))
            touched = bool(row.get("touched"))
            n_cards = int(row.get("cards") or 0)
            out.append(
                self._blotter_row([
                    self._cell(
                        str(row.get("type") or "?"),
                        expand=True,
                        color=TEXT if touched else MUTED,
                    ),
                    self._cell(
                        str(n_cards) if n_cards else "—",
                        width=52,
                        right=True,
                        color=TEXT if n_cards else MUTED,
                    ),
                    self._cell(
                        learned or ("—" if touched else "untouched"),
                        width=190,
                        color=MUTED,
                    ),
                ])
            )
        self.col_notebook_types.controls = out

    def _notebook_card(self, card: dict, attrib: dict | None = None) -> ft.Control:
        status = str(card.get("status") or "testing").strip().lower()
        color = CARD_STATUS_COLOR.get(status, MUTED)
        head: list[ft.Control] = [
            ft.Text(
                str(card.get("name") or "?"),
                size=13,
                weight=ft.FontWeight.BOLD,
                color=TEXT,
                expand=True,
            ),
            self._chip(status, color),
        ]
        ticket = str(card.get("ticket") or "").strip()
        if ticket:
            head.append(self._chip(ticket, BLUE))
        score = (attrib or {}).get(str(card.get("name") or "").lower()) or {}
        sends = int(score.get("sends") or 0)
        fills = int(score.get("attributed_fills") or 0)
        pnl = score.get("realized_pnl")
        if not sends:
            head.append(self._chip("no sends yet", MUTED))
        elif not fills or not isinstance(pnl, (int, float)):
            head.append(self._chip(f"{sends} send(s) · no fills yet", MUTED))
        else:
            head.append(
                self._chip(
                    f"{sends} send(s) · ${pnl:+,.2f}", GREEN if pnl > 0 else RED if pnl else MUTED
                )
            )
        rows: list[ft.Control] = [ft.Row(head, spacing=6)]
        for field, label in (
            ("when_on", "when"),
            ("scan", "scan"),
            ("shape", "shape"),
            ("invalidation", "invalid"),
            ("fill_assumption", "fill"),
            ("note", "note"),
        ):
            val = str(card.get(field) or "").strip()
            if not val:
                continue
            rows.append(
                ft.Row(
                    [
                        ft.Container(
                            width=56,
                            content=ft.Text(label, size=11, color=MUTED),
                        ),
                        ft.Text(val, size=12, color=TEXT, expand=True, selectable=True),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )
            )
        return ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, color if status == "working" else BORDER),
            border_radius=10,
            padding=12,
            content=ft.Column(rows, spacing=6, tight=True),
        )

    # ------------------------------------------------------------ scorecard

    def _sync_scorecard_page(self, *, force: bool = False) -> None:
        """Windows, per-card scores, revision ledger. The strategy tracker.

        Throttled by ``_sync_active_page`` — ``force`` is accepted so the page
        builder and the refresh control can paint immediately.
        """
        _ = force
        self.lbl_sc_netliq.value = self.lbl_equity.value
        self.lbl_sc_netliq.color = self.lbl_equity.color
        for src, dst in (
            (self.lbl_score, self.lbl_sc_score),
            (self.lbl_session_score, self.lbl_sc_session),
            (self.lbl_path, self.lbl_sc_path),
            (self.lbl_mix, self.lbl_sc_mix),
        ):
            dst.value = src.value
            dst.color = src.color
        sc: dict = {}
        if self.engine.state.equity:
            try:
                from abcxauto.scorecard import compute_scorecard

                sc = compute_scorecard(equity=self.engine.state.equity) or {}
            except Exception:
                sc = {}
        beat = sc.get("beating_model")
        if beat is True:
            self.lbl_sc_verdict.value = "BEATING the model bill"
            self.lbl_sc_verdict.color = GREEN
        elif beat is False:
            self.lbl_sc_verdict.value = "behind the model bill"
            self.lbl_sc_verdict.color = AMBER
        else:
            self.lbl_sc_verdict.value = "no live book — connect IBKR to score"
            self.lbl_sc_verdict.color = MUTED
        self._sync_sc_windows(sc)
        self._sync_sc_cards()
        self._sync_sc_ledger(sc)
        try:
            from abcxauto.memory import get_journal

            div = get_journal().strategy_diversity(limit=40) or {}
        except Exception:
            div = {}
        if div.get("n_distinct"):
            strats = ", ".join(str(x) for x in (div.get("strategies") or []))[:160]
            self.lbl_sc_strats.value = f"Types used: {div['n_distinct']} — {strats}"
            self.lbl_sc_strats.color = TEXT
        else:
            self.lbl_sc_strats.value = "Types used: none yet"
            self.lbl_sc_strats.color = MUTED

    def _sync_sc_windows(self, sc: dict) -> None:
        windows = sc.get("windows") if isinstance(sc, dict) else None
        windows = windows if isinstance(windows, dict) else {}
        rows: list[ft.Control] = [
            self._head_row([("window", 78), ("return", 84), ("edge", 90), ("verdict", None)])
        ]
        order = ["15m", "1h", "4h", "inception"]
        seen = [k for k in order if k in windows] + [k for k in windows if k not in order]
        if not seen:
            self.col_sc_windows.controls = [
                ft.Text("No journal history yet.", size=12, color=MUTED)
            ]
            return
        for label in seen:
            row = windows.get(label) or {}
            ret = row.get("book_return_pct")
            edge = row.get("edge_usd")
            beat = row.get("beating_model")
            cov = str(row.get("coverage") or "")
            if beat is True:
                verdict, color = "BEAT", GREEN
            elif beat is False:
                verdict, color = "behind", AMBER
            else:
                verdict, color = cov or "—", MUTED
            rows.append(
                self._blotter_row([
                    self._cell(label, width=78, weight=ft.FontWeight.W_600),
                    self._cell(
                        f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "—",
                        width=84,
                        right=True,
                        color=MUTED,
                    ),
                    self._cell(
                        f"${edge:+,.2f}" if isinstance(edge, (int, float)) else "—",
                        width=90,
                        right=True,
                        color=color,
                    ),
                    self._cell(verdict, expand=True, color=color, weight=ft.FontWeight.W_600),
                ])
            )
        self.col_sc_windows.controls = rows

    def _sync_sc_cards(self) -> None:
        try:
            from abcxauto.lab_playbook import card_scores, load_lab, walk_cards

            lab = load_lab()
            cards: list[dict] = []
            for type_name, card in walk_cards(lab):
                if not isinstance(card, dict) or not card.get("name"):
                    continue
                row = dict(card)
                row["type"] = type_name
                if type_name:
                    row.setdefault("ticket", type_name)
                cards.append(row)
            if not cards:
                cards = [c for c in (lab.get("cards") or []) if isinstance(c, dict)]
            scores = card_scores(cards) or []
        except Exception:
            scores = []
        if not scores:
            self.col_sc_cards.controls = [
                ft.Text(
                    "No card-attributed sends yet. A send records the card that called it.",
                    size=12,
                    color=MUTED,
                )
            ]
            return
        rows: list[ft.Control] = [
            self._head_row([
                ("card", None),
                ("sends", 52),
                ("fills", 46),
                ("hit", 84),
                ("realized", 88),
            ])
        ]
        for row in scores[:20]:
            pnl = row.get("realized_pnl")
            fills = int(row.get("attributed_fills") or 0)
            # Hit rate against the card's own claim. A green edge on a red hit
            # rate is one fat winner, which is the pair worth seeing together.
            cal = row.get("calibration")
            cal = cal if isinstance(cal, dict) else {}
            hit = cal.get("hit_rate")
            gap = cal.get("hit_rate_gap")
            if not isinstance(hit, (int, float)):
                hit_text, hit_color = "—", MUTED
            elif isinstance(gap, (int, float)):
                hit_text = f"{hit:g}% {gap:+g}"
                hit_color = GREEN if gap >= 0 else RED
            else:
                hit_text, hit_color = f"{hit:g}%", MUTED
            color = MUTED
            if fills and isinstance(pnl, (int, float)) and pnl:
                color = GREEN if pnl > 0 else RED
            # No joined fill is not a flat trade — $0.00 would read as one.
            if not fills:
                realized = "no fills yet"
            elif isinstance(pnl, (int, float)):
                realized = f"${pnl:+,.2f}"
            else:
                realized = "—"
            name = str(row.get("card") or "?")
            on_book = row.get("on_current_book")
            rows.append(
                self._blotter_row([
                    self._cell(
                        name if on_book is not False else f"{name} (retired)",
                        expand=True,
                        color=TEXT if on_book is not False else MUTED,
                    ),
                    self._cell(str(row.get("sends") or 0), width=52, right=True),
                    self._cell(
                        str(row.get("attributed_fills") or 0),
                        width=46,
                        right=True,
                        color=MUTED,
                    ),
                    self._cell(hit_text, width=84, right=True, color=hit_color),
                    self._cell(
                        realized,
                        width=88,
                        right=True,
                        color=color,
                        weight=ft.FontWeight.W_600,
                    ),
                ])
            )
        self.col_sc_cards.controls = rows

    def _sync_sc_ledger(self, sc: dict) -> None:
        try:
            from abcxauto.lab_playbook import playbook_facts

            facts = playbook_facts(sc) or {}
            ledger = [r for r in (facts.get("ledger") or []) if isinstance(r, dict)]
        except Exception:
            ledger = []
        if not ledger:
            self.col_sc_ledger.controls = [
                ft.Text("No notebook revisions yet.", size=12, color=MUTED)
            ]
            return
        rows: list[ft.Control] = [
            self._head_row([("rev", 46), ("mode", 70), ("at write", 88), ("closed", 88), ("", None)])
        ]
        for row in reversed(ledger[-8:]):
            at_edge = row.get("edge_usd")
            closed = row.get("closed_edge")
            beat = row.get("closed_beating")
            if beat is None:
                beat = row.get("beating_model")
            if beat is True:
                tag, color = "beat", GREEN
            elif beat is False:
                tag, color = "behind", AMBER
            else:
                tag, color = "open", MUTED
            rows.append(
                self._blotter_row([
                    self._cell(
                        f"r{row.get('revision')}", width=46, mono=True,
                        weight=ft.FontWeight.W_600,
                    ),
                    self._cell(str(row.get("mode") or "—"), width=70, color=MUTED),
                    self._cell(
                        f"${at_edge:+,.2f}" if isinstance(at_edge, (int, float)) else "—",
                        width=88,
                        right=True,
                        color=MUTED,
                    ),
                    self._cell(
                        f"${closed:+,.2f}" if isinstance(closed, (int, float)) else "—",
                        width=88,
                        right=True,
                        color=color,
                    ),
                    self._cell(tag, expand=True, color=color, right=True),
                ])
            )
        self.col_sc_ledger.controls = rows

    # ----------------------------------------------------------------- risk

    def _risk_settings_lines(self) -> list[str]:
        """Persisted knobs from get_config / risk_settings.json. Display only."""
        from abcxauto.config import get_config, load_risk_settings, resolve_effective_posture

        try:
            load_risk_settings()
        except Exception:
            pass
        cfg = get_config()
        stored = str(getattr(cfg, "risk_posture", "") or "")
        eff = resolve_effective_posture(stored, getattr(cfg, "trading_mode", "paper"))
        post = f"{stored} → {eff}" if stored and eff and stored != eff else (stored or "—")

        def yn(v: object) -> str:
            return "on" if bool(v) else "off"

        def pct(v: object) -> str:
            return f"{v:g}" if isinstance(v, (int, float)) else "—"

        return [
            f"posture {post}",
            f"trade {pct(getattr(cfg, 'max_risk_per_trade_pct', None))}%",
            f"day {pct(getattr(cfg, 'daily_loss_limit_pct', None))}%",
            f"position {pct(getattr(cfg, 'max_position_pct', None))}%",
            f"drawdown {pct(getattr(cfg, 'max_peak_drawdown_pct', None))}%",
            f"option {pct(getattr(cfg, 'max_option_premium_pct', None))}%",
            f"defined-risk {yn(getattr(cfg, 'defined_risk_only', True))}",
            f"cash-only {yn(getattr(cfg, 'cash_only', True))}",
            f"gates {yn(getattr(cfg, 'risk_gates_enabled', True))}",
        ]

    def _sync_risk_settings_view(self) -> None:
        from abcxauto.config import get_config

        cfg = get_config()
        self.lbl_risk_glance.value = "\n".join(self._risk_settings_lines())
        for key, _label, _hint in RISK_FIELDS:
            self._set_field(key, getattr(cfg, key, None))
        for key, _label in FLOOR_GATES:
            sw = self.gates.get(key)
            if sw is not None:
                sw.value = bool(getattr(cfg, key, True))

    def _sync_risk_page(self, *, force: bool = False) -> None:
        from abcxauto.config import get_config, resolve_effective_posture

        self._sync_risk_settings_view()
        cfg = get_config()
        stored = str(getattr(cfg, "risk_posture", "") or "") or "—"
        eff = resolve_effective_posture(stored, getattr(cfg, "trading_mode", "paper"))
        self.lbl_risk_posture.value = (
            f"{stored} → {eff} (live clamp)" if eff and eff != stored else stored
        )
        self.lbl_risk_posture.color = TEXT
        rows: list[ft.Control] = [
            self._field_row(key, label, hint) for key, label, hint in RISK_FIELDS
        ]
        for key, label in FLOOR_GATES:
            hint = (
                "paper may turn off; live forced on"
                if key == "risk_gates_enabled"
                else "floor — operator may re-arm, never disarm"
            )
            rows.append(
                self._field_row(
                    key,
                    label,
                    hint,
                    control=self.gates[key],
                )
            )
        self.col_risk_knobs.controls = rows
        try:
            from abcxauto.risk_gates import sizing_floors_active

            live = (not cfg.is_paper) or str(cfg.trading_mode or "").lower() == "live"
            on = True if live else sizing_floors_active(cfg)
        except Exception:
            live, on = False, False
        self.sw_size_floors.value = on
        self.sw_size_floors.disabled = live
        self.lbl_risk_floors.value = (
            "Floors ON — forced on live." if live and on
            else "Floors ON — % size floors apply." if on
            else "Floors OFF — Grok sizes freely (paper)."
        )
        self.lbl_risk_floors.color = GREEN if on else AMBER
        nl = float(self.engine.state.equity or 0) or None
        day = float(self.engine.state.pnl or 0) if self.engine.state.equity else None
        try:
            from abcxauto.book import clerk_halt_facts

            halt = clerk_halt_facts(nl, day) or {}
        except Exception:
            halt = {}
        if halt.get("clerk_halted"):
            kind = str(halt.get("halt_kind") or "halt")
            reason = str(halt.get("halt_reason") or "")[:120]
            self.lbl_risk_halt_state.value = f"HALTED ({kind}) — {reason}"
            self.lbl_risk_halt_state.color = RED
        else:
            self.lbl_risk_halt_state.value = "Clear — new risk allowed"
            self.lbl_risk_halt_state.color = GREEN
        trips = halt.get("halt_trips_at_usd")
        room = halt.get("ibkr_day_vs_halt")
        limit = halt.get("daily_loss_limit_pct")
        bits = []
        if isinstance(limit, (int, float)):
            bits.append(f"daily limit {limit:g}% NL")
        if isinstance(trips, (int, float)):
            bits.append(f"trips at ${trips:,.2f}")
        if isinstance(room, (int, float)):
            bits.append(f"room ${room:,.2f}")
        self.lbl_risk_halt_math.value = " · ".join(bits) or "connect IBKR for the halt math"
        self.lbl_risk_halt_math.color = MUTED

    def _sync_settings_page(self, *, force: bool = False) -> None:
        _ = force
        from abcxauto.config import (
            AGENT_DISCONNECTED_ONLY_KEYS,
            broker_link_connected,
            risk_settings_path,
        )

        cfg = get_config()
        for key in AGENT_FIELD_KEYS:
            self._set_field(key, getattr(cfg, key, None))
        link_locked = broker_link_connected()
        for key in AGENT_DISCONNECTED_ONLY_KEYS:
            tf = self.fields.get(key)
            if tf is not None:
                tf.disabled = link_locked
        for key in ("monitor_enabled", "monitor_extended_hours"):
            sw = self.gates.get(key)
            if sw is not None:
                sw.value = bool(getattr(cfg, key, False))
        live = self.engine.state.running and getattr(self.engine.state, "autonomous", False)
        sess_bit = ""
        try:
            from abcxauto.session_caps import usage

            used = usage(session=str(getattr(self.engine, "_last_session", "") or ""))
            sess_bit = (
                f" · session {used['looks']}/{used['look_cap']} looks · "
                f"{used['tokens']}/{used['token_cap']} tok"
            )
        except Exception:
            sess_bit = (
                f" · session cap {getattr(cfg, 'session_look_cap', '—')} looks / "
                f"{getattr(cfg, 'session_token_cap', '—')} tok"
            )
        self.lbl_settings_brain.value = (
            f"{getattr(cfg, 'model', '—')} · temp {getattr(cfg, 'temperature', '—')} · "
            f"{getattr(cfg, 'max_tokens', '—')} tokens/turn"
            + sess_bit
            + (" · applies on the next look" if live else "")
        )
        paper = bool(getattr(cfg, "is_paper", True))
        self.lbl_settings_mode.value = "Paper" if paper else "Live"
        self.lbl_settings_mode.color = GREEN if paper else RED
        self.lbl_settings_link.value = (
            f"{getattr(cfg, 'ibkr_host', '')}:{getattr(cfg, 'ibkr_port', '')} "
            f"cid={getattr(cfg, 'ibkr_client_id', '')} · "
            f"{'connected' if self.engine.state.connected else 'not connected'}"
        )
        try:
            self.lbl_settings_path.value = str(risk_settings_path())
        except Exception:
            self.lbl_settings_path.value = "risk_settings.json"

    def _refresh_settings_tab(self, _=None) -> None:
        """Drop pending edits and re-read the file so the page shows what stuck."""
        from abcxauto.config import load_risk_settings

        self._dirty.clear()
        try:
            load_risk_settings()
        except Exception:
            logger.debug("settings reload failed", exc_info=True)
        self._sync_settings_page(force=True)
        self._safe_update()

    def _set_risk_posture(self, posture: str) -> None:
        from abcxauto.config import update_risk_config

        try:
            update_risk_config(risk_posture=str(posture or "").strip().lower(), persist=True)
            self._sync_risk_settings_view()
            self._toast(f"Posture → {posture}", color=BLUE)
            self._safe_update()
        except Exception as exc:
            self._toast(f"Posture failed: {exc}", color=RED)
            self._safe_update()

    def _note_setting(self, msg: str, *, color: str = BLUE) -> None:
        """One line the operator can read after an Apply, plus a toast."""
        for lbl in (self.lbl_risk_status, self.lbl_settings_status):
            lbl.value = msg
            lbl.color = color
        self._toast(msg, color=color)

    def _apply_field(self, key: str) -> None:
        if key in AGENT_FIELD_KEYS:
            self._apply_agent_field(key)
        else:
            self._apply_risk_field(key)

    def _apply_risk_field(self, key: str) -> None:
        """Tighten-only: the writers clamp to the floor and we report what stuck."""
        from abcxauto.config import update_capacity_config, update_risk_config

        raw = str((self.fields.get(key) or ft.TextField()).value or "").strip()
        try:
            typed = float(raw)
        except (TypeError, ValueError):
            self._note_setting(f"{key} needs a number", color=AMBER)
            self._safe_update()
            return
        try:
            if key == "max_open_positions":
                update_capacity_config(max_open_positions=int(typed), persist=True)
            else:
                update_risk_config(**{key: typed}, persist=True)
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
            self._safe_update()
            return
        self._dirty.discard(key)
        now = getattr(get_config(), key, None)
        if isinstance(now, (int, float)) and abs(float(now) - typed) > 1e-9:
            self._note_setting(
                f"{key} clamped to {now:g} — walk-away floor", color=AMBER
            )
        else:
            self._note_setting(f"{key} → {typed:g}")
        self._sync_risk_page(force=True)
        self._safe_update()

    def _apply_agent_field(self, key: str) -> None:
        from abcxauto.config import set_agent_knobs

        raw = str((self.fields.get(key) or ft.TextField()).value or "").strip()
        try:
            res = set_agent_knobs({key: raw}, persist=True)
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
            self._safe_update()
            return
        self._dirty.discard(key)
        self._report_agent_result(key, res)
        self._sync_settings_page(force=True)
        self._safe_update()

    def _apply_agent_switch(self, key: str) -> None:
        from abcxauto.config import set_agent_knobs

        sw = self.gates.get(key)
        want = bool(getattr(sw, "value", True))
        try:
            res = set_agent_knobs({key: want}, persist=True)
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
            self._safe_update()
            return
        self._report_agent_result(key, res)
        self._sync_settings_page(force=True)
        self._safe_update()

    def _report_agent_result(self, key: str, res: dict) -> None:
        res = res if isinstance(res, dict) else {}
        why = (res.get("rejected") or {}).get(key)
        if why:
            self._note_setting(f"{key} refused: {why}", color=RED)
            return
        note = (res.get("clamped") or {}).get(key)
        if isinstance(note, dict):
            self._note_setting(
                f"{key} clamped to {note.get('clamped')} (asked {note.get('raw')})",
                color=AMBER,
            )
            return
        value = (res.get("applied") or {}).get(key)
        tail = " — next look" if key in ("model", "temperature", "max_tokens") else ""
        self._note_setting(f"{key} → {value}{tail}")

    def _toggle_floor_gate(self, key: str) -> None:
        """Live floor gates stay armed. Paper may turn risk_gates_enabled off."""
        from abcxauto.config import update_risk_config

        sw = self.gates.get(key)
        if sw is None:
            return
        cfg = get_config()
        paper = bool(getattr(cfg, "is_paper", True)) and str(
            getattr(cfg, "trading_mode", "paper") or ""
        ).lower() != "live"
        if key == "risk_gates_enabled" and paper:
            want = bool(sw.value)
            try:
                update_risk_config(risk_gates_enabled=want, persist=True)
                self._note_setting(f"risk_gates_enabled → {want}")
            except Exception as exc:
                self._note_setting(f"{key} failed: {exc}", color=RED)
            self._sync_risk_page(force=True)
            self._safe_update()
            return
        if not bool(sw.value):
            sw.value = True
            self._note_setting(
                f"{key} is the walk-away floor — Pro cannot turn it off", color=AMBER
            )
            self._safe_update()
            return
        try:
            update_risk_config(**{key: True}, persist=True)
            self._note_setting(f"{key} armed")
        except Exception as exc:
            self._note_setting(f"{key} failed: {exc}", color=RED)
        self._sync_risk_page(force=True)
        self._safe_update()

    def _toggle_trading_mode(self, _=None) -> None:
        if get_config().is_paper:
            self._open_live_confirm_dialog()
            return
        try:
            self.engine.switch_trading_mode("paper")
            self._after_mode_change()
        except Exception as exc:
            self._toast(str(exc), color=RED)
            self._safe_update()

    def _toggle_sizing_floors(self, _=None) -> None:
        """Paper two-way toggle of clerk ``sizing_floors``. Live paints ON and ignores clicks."""
        cfg = get_config()
        if not cfg.is_paper or str(cfg.trading_mode or "").lower() == "live":
            self._sync_floors_chip()
            self._toast("Live: Floors on (forced)", color=AMBER)
            self._safe_update()
            return
        try:
            from abcxauto.config import update_risk_config
            from abcxauto.risk_gates import sizing_floors_active

            next_on = not sizing_floors_active(cfg)
            update_risk_config(sizing_floors=next_on, persist=True)
            self._sync_floors_chip()
            self._toast(
                f"Floors → {'on' if next_on else 'off'}",
                color=GREEN if next_on else AMBER,
            )
            self._safe_update()
        except Exception as exc:
            self._sync_floors_chip()
            self._toast(f"Floors toggle failed: {exc}", color=RED)
            self._safe_update()

    def _sync_floors_chip(self) -> None:
        from abcxauto.risk_gates import sizing_floors_active

        cfg = get_config()
        live = (not cfg.is_paper) or str(cfg.trading_mode or "").lower() == "live"
        on = True if live else sizing_floors_active(cfg)
        self.lbl_floors.value = "Floors on" if on else "Floors off"
        self.lbl_floors.color = GREEN if on else AMBER
        # A refused or failed toggle has to snap the Risk page switch back now,
        # not on the next repaint.
        self.sw_size_floors.value = on
        self.sw_size_floors.disabled = live
        self.btn_floors.border = ft.Border.all(1, GREEN if on else AMBER)
        self.btn_floors.tooltip = (
            "Live: Floors on (forced)"
            if live
            else "Paper: click to toggle % size floors"
        )

    def _open_live_confirm_dialog(self) -> None:
        self.tf_live_confirm.value = ""

        def _cancel(_=None) -> None:
            dlg.open = False
            self._safe_update()

        def _confirm(_=None) -> None:
            phrase = str(self.tf_live_confirm.value or "").strip()
            try:
                self.engine.switch_trading_mode("live", live_confirm=phrase)
                dlg.open = False
                self._after_mode_change()
            except Exception as exc:
                self._toast(str(exc), color=RED)
                self._safe_update()

        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=SURFACE,
            title=ft.Text("Switch to Live?", color=TEXT),
            content=ft.Column(
                [
                    ft.Text("Real-money mode. Type the exact confirm phrase:", size=13, color=MUTED),
                    ft.Text(LIVE_CONFIRM_PHRASE, size=12, color=TEXT, selectable=True),
                    self.tf_live_confirm,
                ],
                tight=True,
                spacing=12,
                width=360,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_cancel),
                ft.TextButton("Switch to Live", on_click=_confirm),
            ],
            shape=ft.RoundedRectangleBorder(radius=16),
        )
        self.page.overlay.append(dlg)
        dlg.open = True
        self._safe_update()

    def _after_mode_change(self) -> None:
        self._sync_ibkr_account_label()
        self._toast(f"Mode → {self.lbl_account_mode.value}", color=BLUE)
        self._safe_update()

    def _sync_ibkr_account_label(self) -> None:
        s = self.engine.state
        cfg = get_config()
        paper = bool(cfg.is_paper)
        aid = str(getattr(s, "ibkr_account_id", "") or "")
        aname = str(getattr(s, "ibkr_account_name", "") or "")
        self.lbl_account_mode.value = "Paper" if paper else "Live"
        self.lbl_account_mode.color = GREEN if paper else RED
        self.btn_account_mode.border = ft.Border.all(1, GREEN if paper else RED)
        self._sync_floors_chip()
        if s.connected and aid:
            self.lbl_account_name.value = aname or f"IBKR {aid}"
            self.lbl_account_id.value = aid
        elif s.connected:
            self.lbl_account_name.value = aname or "IBKR"
            self.lbl_account_id.value = "Account id pending…"
        else:
            self.lbl_account_name.value = "IBKR"
            self.lbl_account_id.value = "Not connected"

    def _refresh_alert(self, unprot: int) -> None:
        halted = bool(getattr(self.engine.state, "halted", False))
        bits: list[str] = []
        if halted:
            bits.append("HALTED — click Resume to send")
        if unprot:
            bits.append(f"{unprot} stock lot(s) need a last-stop")
        if bits:
            self.lbl_alert.value = " · ".join(bits)
            self.lbl_alert.visible = True
            self.lbl_alert.color = RED
        else:
            self.lbl_alert.value = ""
            self.lbl_alert.visible = False

    def _refresh_run_btn(self) -> None:
        s = self.engine.state
        running = bool(s.running) and getattr(s, "autonomous", False) and not getattr(s, "paused", False)
        if running:
            self._set_btn_text(self.btn_run, "Stop", filled=False)
            self.lbl_run_state.value = "Grok on"
            self.lbl_run_state.color = GREEN
            self.lbl_desk.value = "On"
            self.lbl_desk.color = GREEN
            self.lbl_desk_sub.value = ""
        else:
            self._set_btn_text(self.btn_run, "Start", filled=True)
            paused = bool(getattr(s, "paused", False))
            self.lbl_run_state.value = "Grok paused" if paused else "Grok off"
            self.lbl_run_state.color = AMBER if paused else MUTED
            self.lbl_desk.value = "Paused" if paused else "Off"
            self.lbl_desk.color = AMBER if paused else MUTED
            self.lbl_desk_sub.value = ""

    def _refresh_connect_btn(self) -> None:
        s = self.engine.state
        linked = bool(s.connected) or (
            self.engine.worker is not None and self.engine.worker.is_alive()
        )
        if linked:
            self._set_btn_text(self.btn_connect, "Disconnect IBKR", danger=True)
        else:
            self._set_btn_text(self.btn_connect, "Connect IBKR", outlined=True)

    def _refresh_halt_btn(self) -> None:
        halted = bool(getattr(self.engine.state, "halted", False))
        try:
            from abcxauto.risk_gates import get_risk_gate

            halted = halted or bool(get_risk_gate().is_halted)
        except Exception:
            pass
        if halted:
            self._set_btn_text(self.btn_halt, "Resume", danger=True)
            self.lbl_halt.value = "HALTED"
            self.lbl_halt.color = RED
        else:
            self._set_btn_text(self.btn_halt, "Halt", outlined=True)
            self.lbl_halt.value = "clear"
            self.lbl_halt.color = GREEN

    def _refresh_service_status(self) -> None:
        try:
            from abcxauto.connections import connection_status

            st = connection_status(self.engine.conn)
        except Exception:
            st = {}
        s = self.engine.state
        ibkr_ok = bool(s.connected)
        xai_ok = bool(st.get("xai_configured"))
        mda_ok = bool(st.get("mda_configured"))
        mode = str(st.get("trading_mode") or get_config().trading_mode or "paper")
        if s.status == "Connecting" and not ibkr_ok:
            self.dot_conn.bgcolor = AMBER
            self.lbl_ibkr_status.value = "Connecting…"
            self.lbl_ibkr_status.color = AMBER
        else:
            self.dot_conn.bgcolor = GREEN if ibkr_ok else RED
            self.lbl_ibkr_status.value = f"Connected ({mode})" if ibkr_ok else "Disconnected"
            self.lbl_ibkr_status.color = GREEN if ibkr_ok else MUTED
        self.dot_xai.bgcolor = GREEN if xai_ok else RED
        self.lbl_xai_status.value = "Ready" if xai_ok else "Missing key"
        self.lbl_xai_status.color = GREEN if xai_ok else MUTED
        self.dot_mda.bgcolor = GREEN if mda_ok else RED
        self.lbl_mda_status.value = "Ready" if mda_ok else "Not configured"
        self.lbl_mda_status.color = GREEN if mda_ok else MUTED
        cfg = get_config()
        host = st.get("ibkr_host") or getattr(cfg, "ibkr_host", "") or "127.0.0.1"
        port = st.get("ibkr_port") or getattr(cfg, "ibkr_port", 0) or 0
        cid = st.get("ibkr_client_id") or getattr(cfg, "ibkr_client_id", 0) or 0
        self.lbl_link.value = f"{host}:{port} cid={cid}"
        err = getattr(s, "last_error", None)
        if err:
            s.last_error = None
            self._toast(str(err), color=RED)

    def _sync_think_stream(self) -> None:
        live = str(getattr(self.engine.state, "think_live", "") or "")
        status = str(getattr(self.engine.state, "status", "") or "").strip()
        n = len(live)
        self.lbl_stream_status.value = f"{n:,} chars" + (f" · {status}" if status else "")
        key = live[-24000:]
        if key == getattr(self, "_think_sync_key", None):
            return
        self._think_sync_key = key
        body = key.strip()
        if not body:
            prev = self._prev_stream()
            self.think_live.value = prev or "Grok stream: waiting for tools..."
            self.think_live.color = MUTED
            self.think_live.visible = True
            self.col_stream.controls = []
            self._stream_lines_key = ""
        else:
            # Plain-text fallback + what Copy stream and the tests read.
            self.think_live.value = body[-STREAM_RAW_TAIL_CHARS:]
            self.think_live.color = TEXT
            self.think_live.visible = False
            self._sync_stream_lines(body)

    def _sync_stream_lines(self, body: str) -> None:
        """One control per stream line so markers read at a glance.

        Every line is painted with its own text — no marker is rewritten,
        stripped or reordered. Only whitespace-only lines become spacing.
        """
        lines = body[-STREAM_TAIL_CHARS:].splitlines()[-STREAM_MAX_LINES:]
        key = "\n".join(lines)
        if key == self._stream_lines_key:
            return
        self._stream_lines_key = key
        # Only attach the screen to a scan line whose own counts match it. A
        # stale payload next to this look's hits= would be a quiet lie.
        hits = getattr(self.engine.state, "scan_hits", None) or {}
        n_rows = len([r for r in (hits.get("rows") or []) if isinstance(r, dict)])
        want = f"hits={n_rows} quoted={hits.get('quoted')} " if n_rows else ""
        scan_at = -1
        if want:
            for i, raw in enumerate(lines):
                if stream_line_kind(raw) == "scan" and raw.strip().startswith(want):
                    scan_at = i
        controls: list[ft.Control] = []
        mode = "say"
        for i, raw in enumerate(lines):
            kind = stream_line_kind(raw)
            if kind == "blank":
                continue
            if kind == "think":
                mode = "think"
            elif kind == "clerk":
                mode = "clerk"
            elif kind == "say":
                mode = "say"
            elif kind == "banner":
                mode = "clerk" if "CLERK" in raw else "say"
            controls.append(self._stream_line(raw, kind, mode))
            if i == scan_at:
                controls.append(self._scan_inline())
        self.col_stream.controls = controls

    def _stream_line(self, raw: str, kind: str, mode: str) -> ft.Control:
        color: str = TEXT
        weight: Any = None
        if kind == "banner":
            if "CLERK" in raw:
                color, weight = MUTED, ft.FontWeight.BOLD
            else:
                color, weight = BLUE, ft.FontWeight.BOLD
        elif kind == "clerk":
            color, weight = MUTED, ft.FontWeight.W_600
        elif kind == "send":
            color, weight = GREEN, ft.FontWeight.BOLD
        elif kind == "tool":
            color, weight = BLUE, ft.FontWeight.W_600
        elif kind == "alarm":
            color, weight = RED, ft.FontWeight.BOLD
        elif kind in ("warn", "poke"):
            color, weight = AMBER, ft.FontWeight.W_600
        elif kind == "think":
            color, weight = MUTED, ft.FontWeight.W_600
        elif kind == "say":
            color, weight = BLUE, ft.FontWeight.W_600
        elif kind in ("cached", "scan"):
            color = MUTED
        elif mode == "think":
            color = MUTED
        line = ft.Text(
            raw,
            size=STREAM_FONT_SIZE,
            color=color,
            weight=weight,
            selectable=True,
            no_wrap=False,
            font_family="Consolas",
        )
        if kind != "banner":
            return line
        # A look boundary is the thing the operator scans for — rule it off.
        return ft.Container(
            content=line,
            padding=ft.Padding.only(top=10, bottom=4),
            border=ft.Border(top=ft.BorderSide(1, BORDER)),
        )

    def _scan_inline(self) -> ft.Control:
        """The screen that scan pulled, at the look where it was pulled."""
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            content=ft.Column(
                [
                    ft.Container(
                        content=self.lbl_scan_head,
                        ink=True,
                        tooltip="Show / hide the screen this scan pulled",
                        on_click=self._toggle_scan_inline,
                    ),
                    self.col_scan,
                ],
                spacing=6,
                tight=True,
            ),
        )

    def _toggle_scan_inline(self, _=None) -> None:
        self._scan_inline_open = not getattr(self, "_scan_inline_open", True)
        self.col_scan.visible = self._scan_inline_open
        self._safe_update()

    def _toggle_stream_follow(self, _=None) -> None:
        """Reading back must not be yanked to the tail. The chip is the way home."""
        follow = not getattr(self, "_stream_follow", True)
        self._stream_follow = follow
        self.think_scroll.auto_scroll = follow
        self.lbl_stream_follow.value = "live" if follow else "jump to live"
        self.lbl_stream_follow.color = GREEN if follow else AMBER
        self.btn_stream_follow.border = ft.Border.all(1, GREEN if follow else AMBER)
        self._safe_update()

    @staticmethod
    def _age_s(sec: float) -> str:
        if sec < 90:
            return f"{sec:.0f}s"
        if sec < 5400:
            return f"{sec / 60:.0f}m"
        return f"{sec / 3600:.1f}h"

    def _think_tail_moved(self, buf: str) -> bool:
        """True when think_live changed since the last paint. First paint is not motion.

        Length alone lies once the 24k cap is full — the last 512 chars still
        move. Hold TAIL_LIVE_S after the last change so a 4s snap gap stays looking.
        """
        text = buf or ""
        n = len(text)
        fp = text[-512:]
        prev_n = self._tail_len
        prev_fp = self._tail_fp
        self._tail_len = n
        self._tail_fp = fp
        now = time.monotonic()
        if prev_n is None:
            return False
        if n > prev_n or (prev_fp is not None and fp != prev_fp):
            self._tail_moved_mono = now
            return True
        age = now - float(self._tail_moved_mono or 0)
        return bool(self._tail_moved_mono) and age < TAIL_LIVE_S

    def _sync_last_line(self) -> None:
        """Last say in the tail, else last real card send. Never Last send: — after a look."""
        s = self.engine.state
        buf = str(getattr(s, "think_live", "") or "")
        say = think_tail_last_say(buf)
        stage_err = str(getattr(s, "stage_error", "") or "").strip()
        strat = str(getattr(s, "brain_strat", "") or "").strip()
        sends_look = int(getattr(s, "sends_last_look", 0) or 0)
        if stage_err:
            self.lbl_last_send.value = f"Block: {stage_err[:240]}"
            self.lbl_last_send.color = AMBER
            return
        if say:
            self.lbl_last_send.value = say[:240]
            self.lbl_last_send.color = TEXT
            return
        card = last_card_send_label()
        if card:
            self.lbl_last_send.value = card[:240]
            self.lbl_last_send.color = TEXT
            return
        if strat and strat not in ("—",):
            self.lbl_last_send.value = f"Last send: {strat}"
            self.lbl_last_send.color = TEXT
            return
        if sends_look:
            self.lbl_last_send.value = f"Last look: {sends_look} send(s)"
            self.lbl_last_send.color = TEXT
            return
        if not s.equity and self._brief().get("strat"):
            brief = self._brief()
            self.lbl_last_send.value = (
                f"Last send: {brief.get('strat')} · {brief.get('sends') or 0} sends "
                f"({self._brief_age(brief)})"
            )
            self.lbl_last_send.color = MUTED
            return
        self.lbl_last_send.value = "—"
        self.lbl_last_send.color = MUTED

    def _sync_health_strip(self) -> None:
        """Silence, burn, link — the three things that make the operator step in.

        Monotonic math and small dict reads only; this paints every tick.
        """
        s, eng = self.engine.state, self.engine
        running = bool(s.running) and bool(getattr(s, "autonomous", False))
        streak = int(getattr(eng, "_fail_streak", 0) or 0)
        last = float(getattr(eng, "_last_grok_mono", 0.0) or 0.0)
        status = str(getattr(s, "status", "") or "")
        st = status.lower()
        buf = str(getattr(s, "think_live", "") or "")
        parked = bool(getattr(eng, "_think_parked", False) or st == "parked")
        tail_moved = self._think_tail_moved(buf)
        say = think_tail_last_say(buf)
        tail_live = think_tail_in_flight(buf) or (bool(say) and tail_moved)
        state = grok_sub_state(
            running=running,
            status=status,
            fail_streak=streak,
            parked=parked,
            tail_moved=tail_moved,
            tail_live=tail_live,
        )
        color = grok_sub_color(state)
        looking = state == "looking"
        self.lbl_hs_state.value = state
        self.lbl_hs_state.color = color
        # Grok tile mirrors the strip so "On" alone never hides a think or a wait.
        if running:
            self.lbl_desk_sub.value = state
            self.lbl_desk_sub.color = color
        age = (time.monotonic() - last) if last else None
        if age is None:
            self.lbl_hs_age.value = "no look yet"
            self.lbl_hs_age.color = AMBER if running else MUTED
        else:
            self.lbl_hs_age.value = f"last look {self._age_s(age)} ago"
            self.lbl_hs_age.color = (
                RED if running and age > 1800 else (AMBER if running and age > 900 else MUTED)
            )
        capped = bool(getattr(eng, "_session_capped", False) or st == "idle")
        if capped and not looking:
            self.lbl_hs_next.value = "session cap — idle"
            self.lbl_hs_next.color = AMBER
        elif streak and not looking:
            self.lbl_hs_next.value = f"look failed (x{streak})"
            self.lbl_hs_next.color = AMBER
        else:
            self.lbl_hs_next.value = ""
            self.lbl_hs_next.color = MUTED
        sends = int(getattr(s, "sends_last_look", 0) or 0)
        tools = len(think_tail_tool_chips(buf))
        burning = False
        if sends:
            self.lbl_hs_burn.value = f"{sends} ticket(s) this look"
            self.lbl_hs_burn.color = GREEN
        else:
            self.lbl_hs_burn.value = ""
            self.lbl_hs_burn.color = MUTED
        self.lbl_hs_look.value = f"this look: {tools} tool(s) · {sends} send(s)"
        self.lbl_hs_look.color = TEXT if tools else MUTED
        # Only a burn gets a box — the strip is otherwise a plain status bar.
        self.health_box.border = ft.Border.all(1, RED) if burning else None
        pulse = getattr(s, "reality_pulse", None) or {}
        block = pulse.get("session") if isinstance(pulse, dict) else None
        sess = str(block.get("status") or "") if isinstance(block, dict) else str(block or "")
        link = str(self.lbl_ibkr_status.value or "")
        self.lbl_hs_link.value = f"{link} · {sess}" if sess else link
        self.lbl_hs_link.color = GREEN if bool(getattr(s, "connected", False)) else RED
        self._sync_last_line()

    def _sync_book_strip(self) -> None:
        """Three lots and three working orders — context for reading the stream."""
        s = self.engine.state
        book = getattr(s, "portfolio", None) or {}
        naked = book.get("unprotected_symbols") if isinstance(book, dict) else None
        lots = self._lot_view(s.positions, naked)[:3]
        try:
            from abcxauto.world_state import compact_working_orders

            orders = compact_working_orders(s.open_orders or [], positions=s.positions)[:3]
        except Exception:
            orders = []
        key = json.dumps([lots, orders], sort_keys=True, default=str)
        if key == self._book_strip_key:
            return
        self._book_strip_key = key
        if not lots and not orders:
            self.lbl_book_strip.value = "No open lots"
            self.col_book_strip.controls = [self.lbl_book_strip]
            return
        controls: list[ft.Control] = []
        for r in lots:
            mkt, pct = r.get("mkt"), r.get("mtm_pct")
            protected = not bool(r.get("unprotected"))
            controls.append(
                self._blotter_row(
                    [
                        self._cell(str(r.get("ident") or "?"), expand=True, mono=True),
                        self._cell(
                            f"{mkt:,.2f}" if isinstance(mkt, (int, float)) else "—",
                            width=76,
                            right=True,
                            mono=True,
                            color=TEXT if isinstance(mkt, (int, float)) else MUTED,
                        ),
                        self._cell(
                            f"{pct:+.0f}%" if isinstance(pct, (int, float)) else "—",
                            width=52,
                            right=True,
                            color=(
                                MUTED
                                if not isinstance(pct, (int, float))
                                else (GREEN if pct >= 0 else RED)
                            ),
                        ),
                        self._cell(
                            "protected" if protected else "naked",
                            width=72,
                            right=True,
                            color=MUTED if protected else RED,
                            weight=ft.FontWeight.W_600,
                        ),
                    ],
                    alert=not protected,
                )
            )
        for o in orders:
            px = []
            if o.get("stop") not in (None, ""):
                px.append(f"stop {o['stop']}")
            if o.get("lmt") not in (None, ""):
                px.append(f"lmt {o['lmt']}")
            controls.append(
                ft.Text(
                    " ".join(
                        str(x)
                        for x in (
                            o.get("order_id") or "?",
                            o.get("symbol") or "?",
                            o.get("action") or "",
                            o.get("type") or "",
                            " · ".join(px),
                        )
                        if str(x).strip()
                    ),
                    size=11,
                    color=MUTED,
                    selectable=True,
                )
            )
        self.col_book_strip.controls = controls

    def _prev_stream(self) -> str:
        """An idle pane is wasted — show the last look Grok took."""
        if self._prev_text is None:
            try:
                from abcxauto.think_stream import THINK_PREV_PATH

                raw = (
                    THINK_PREV_PATH.read_text(encoding="utf-8")
                    if THINK_PREV_PATH.is_file()
                    else ""
                )
            except OSError:
                raw = ""
            tail = raw.strip()[-3000:]
            self._prev_text = f"— previous look —\n\n{tail}" if tail else ""
        return self._prev_text

    def _sync_active_page(self, *, force: bool = False) -> None:
        """Only the visible page reads disk, and at most every PAGE_REFRESH_S."""
        now = time.monotonic()
        if not force and now - float(self._page_last or 0) < PAGE_REFRESH_S:
            return
        self._page_last = now
        try:
            if self.tab == "notebook":
                self._sync_notebook_page(force=force)
            elif self.tab == "scorecard":
                self._sync_scorecard_page(force=force)
            elif self.tab == "risk":
                self._sync_risk_page(force=force)
            elif self.tab == "settings":
                self._sync_settings_page(force=force)
        except Exception:
            logger.debug("page sync failed tab=%s", self.tab, exc_info=True)

    def _sync_widgets(self) -> None:
        s = self.engine.state
        self._sync_ibkr_account_label()
        brief = {} if s.equity else self._brief()
        nl = float(s.equity or 0) or float(brief.get("net_liquidation") or 0)
        self.lbl_equity.value = f"${nl:,.2f}" if nl else "—"
        self.lbl_equity.color = TEXT if s.equity else MUTED
        self.lbl_equity_sub.value = "live" if s.equity else self._brief_age(brief) if nl else ""
        if s.equity:
            self.lbl_pnl.value = f"${s.pnl:+.2f}"
            self.lbl_pnl.color = GREEN if s.pnl >= 0 else RED
            self.lbl_pnl_pct.value = f"{s.pnl / s.equity * 100:+.2f}% vs prior close"
        else:
            self.lbl_pnl.value = "—"
            self.lbl_pnl.color = MUTED
            self.lbl_pnl_pct.value = ""
        try:
            from abcxauto.world_state import open_upnl_of

            upnl = open_upnl_of(s.positions)
        except Exception:
            upnl = None
        if isinstance(upnl, (int, float)):
            self.lbl_open_upnl.value = f"${upnl:+,.2f}"
            self.lbl_open_upnl.color = GREEN if upnl >= 0 else RED
            self.lbl_open_upnl_sub.value = "marks now"
        else:
            self.lbl_open_upnl.value = "—"
            self.lbl_open_upnl.color = MUTED
            self.lbl_open_upnl_sub.value = "no open marks"
        unprot = int(getattr(s, "unprotected_count", 0) or 0)
        self.lbl_unprotected.value = str(unprot)
        self.lbl_unprotected.color = RED if unprot else GREEN
        lots = len(s.positions or []) or len(brief.get("open_lots") or [])
        try:
            cap = int(getattr(get_config(), "max_open_positions", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        self.lbl_lot_count.value = f"{lots}/{cap}" if cap else str(lots)
        self.lbl_lot_count.color = AMBER if cap and lots >= cap else TEXT
        self.lbl_risk.value = f"Risk: {s.risk}" if s.risk else "Risk: —"
        self.lbl_status.value = s.status
        running = bool(s.running) and getattr(s, "autonomous", False)
        self.lbl_status.color = GREEN if running else (AMBER if getattr(s, "paused", False) else MUTED)
        self._refresh_run_btn()
        self._refresh_connect_btn()
        self._refresh_halt_btn()
        self._refresh_alert(unprot)
        self._refresh_service_status()
        self._sync_think_stream()
        result = s.last_result or {}
        status = self._format_result_status(result)
        self.lbl_result.value = f"Result: {status}"
        blocked = status.lower().startswith(("blocked", "rejected", "fail", "error"))
        self.lbl_result.color = RED if blocked else TEXT
        rationale = str(s.brain_rationale or "").strip()
        if not s.equity:
            rationale = str(self._brief().get("rationale") or "").strip() or rationale
        self.lbl_why.value = f"Why: {rationale[:240]}" if rationale and rationale != "—" else "Why: —"
        self.lbl_why.color = TEXT if rationale and rationale != "—" else MUTED
        self.lbl_why.tooltip = rationale[:600] or None
        market_read = str(getattr(s, "market_read", "") or "").strip()
        self.lbl_focus.value = f"Focus: {market_read[:220]}" if market_read else "Focus: —"
        self.lbl_focus.color = TEXT if market_read else MUTED
        pace = getattr(s, "pace", None) or {}
        if isinstance(pace, dict) and pace:
            sleep_s = pace.get("sleep_s") or pace.get("wait_s")
            reason = pace.get("reason") or pace.get("tier") or ""
            self.lbl_pace.value = f"Pace: {sleep_s}s {reason}".strip()
        else:
            self.lbl_pace.value = "Pace: —"
        trace = list(getattr(s, "tool_trace", None) or [])
        self.lbl_tools.value = f"Tools: {' '.join(trace[-12:])}" if trace else "Tools: —"
        self.lbl_tools.color = TEXT if trace else MUTED
        self.lbl_dash_tools.value = self.lbl_tools.value
        self.lbl_dash_tools.color = self.lbl_tools.color
        skip = str(getattr(s, "skip_reason", "") or getattr(s, "stage_error", "") or "")
        if getattr(s, "book_unreliable", False) and "unreliable" not in skip:
            skip = skip or "book_unreliable"
        strat = str(getattr(s, "brain_strat", "") or "").strip()
        if skip:
            self.lbl_banner.value = skip
            self.lbl_banner.visible = True
            self.lbl_banner.color = RED if "error" in skip.lower() else AMBER
        elif blocked:
            self.lbl_banner.value = f"{strat}: {status}" if strat else status
            self.lbl_banner.visible = True
            self.lbl_banner.color = RED
        else:
            self.lbl_banner.value = ""
            self.lbl_banner.visible = False
        now = time.monotonic()
        try:
            eq_k = round(float(getattr(s, "equity", 0) or 0), 2)
        except (TypeError, ValueError):
            eq_k = 0.0
        if (
            now - float(getattr(self, "_score_last", 0) or 0) >= 3.0
            or eq_k != getattr(self, "_score_eq", None)
        ):
            self._score_last = now
            self._score_eq = eq_k
            self._refresh_score_line()
        if now - float(self._path_last or 0) >= 20.0 or not self._path_last:
            self._path_last = now
            self._sync_path_line()
        self._sync_mix_line()
        self._sync_lots()
        self._sync_orders()
        self._sync_fills()
        self._sync_activity()
        self._sync_scan_tape()
        self._sync_health_strip()
        self._sync_book_strip()
        self._sync_lessons_line()
        self._sync_tabs()
        try:
            from abcxauto.lab_playbook import is_paper, load_lab, load_live

            pb = load_lab() if is_paper() else load_live()
            pb = pb if isinstance(pb, dict) else {}
            inst = str(pb.get("instructions") or "").strip()
            if is_paper():
                tag = "promoted" if pb.get("promoted") else (
                    "ready" if pb.get("ready_to_promote") else "lab"
                )
            else:
                tag = "live" if inst else "no promote"
            rev = pb.get("revision") or pb.get("promoted_revision") or "—"
            score = pb.get("paper_score") if isinstance(pb.get("paper_score"), dict) else {}
            edge = score.get("edge_usd")
            edge_s = f"{edge:,.2f}" if isinstance(edge, (int, float)) else edge
            line = (
                f"Playbook [{tag}] rev={rev} edge={edge_s}"
                if inst else f"Playbook [{tag}]: none"
            )
            self.lbl_playbook.value = line
            self.lbl_playbook.tooltip = str(pb.get("instructions") or "")[:600] or None
            self.lbl_playbook.color = TEXT if inst else MUTED
        except Exception:
            self.lbl_playbook.value = "Playbook: —"
        self.page.title = "ABCXAUTO"
        self.lbl_working_orders.value = self._format_working_orders(
            s.open_orders or [], positions=getattr(s, "positions", None)
        )
        self.lbl_working_orders.color = TEXT if s.open_orders else MUTED
        fills = getattr(s, "recent_fills", None) or []
        self.lbl_recent_fills.value = self._format_recent_fills(fills)
        self.lbl_recent_fills.color = TEXT if fills else MUTED
        self.lbl_activity.value = self._cycle_log_text(s.records)
        health = str(getattr(s, "mandate_health", "") or "green")
        label = str(getattr(s, "mandate_health_label", "") or "protected")
        self.lbl_mandate_health.value = f"{health} — {label}"
        self.lbl_mandate_health.color = (
            RED if health == "red" else (AMBER if health == "amber" else GREEN)
        )
        self._sync_active_page()
        try:
            pulse = s.reality_pulse or {}
            if pulse:
                self._apply_clock(pulse)
        except Exception:
            pass
        self._paint_think_news_if_flat()

    @staticmethod
    def _format_result_status(res: object) -> str:
        if not isinstance(res, dict) or not res:
            return "—"
        if res.get("success") is True:
            if res.get("filled") is True:
                ep = res.get("entry_price")
                return f"filled @{ep}" if ep not in (None, "") else "filled"
            return "ok"
        status = str(res.get("status") or "").strip()
        note = str(res.get("note") or res.get("reason_code") or res.get("error") or "").strip()
        if status and note and note.lower() not in status.lower():
            combo = f"{status}: {note}"
            return combo if len(combo) <= 120 else combo[:117] + "…"
        return status or note or ("fail" if res.get("success") is False else "ok")

    def _format_working_orders(self, orders: list, positions: list | None = None) -> str:
        from abcxauto.world_state import compact_working_orders

        rows = compact_working_orders(orders, positions=positions)
        if not rows:
            return "No working orders"
        lines = []
        for o in rows:
            oid = o.get("order_id") or "?"
            sym = o.get("symbol") or "?"
            sec = o.get("sec") or "STK"
            otype = o.get("type") or "?"
            qty = o.get("qty") if o.get("qty") is not None else "?"
            action = o.get("action") or ""
            bit = f" stop={o['stop']}" if o.get("stop") not in (None, "") else ""
            if o.get("lmt") not in (None, "") and "lmt" not in bit:
                bit += f" lmt={o['lmt']}"
            leg = ""
            if str(sec).upper().startswith("OPT"):
                right = o.get("right") or ""
                strike = o.get("strike")
                exp = o.get("expiration") or ""
                leg = f" {right}{strike} {exp}".rstrip()
            role = str(o.get("role") or "").strip()
            covers = str(o.get("covers") or "").strip()
            tag = f"  {role} {covers}".rstrip() if role else ""
            act = f"{action} " if action else ""
            lines.append(f"{oid}  {sym} {sec} {act}{otype} x{qty}{leg}{bit}{tag}")
        return "\n".join(lines)

    def _format_recent_fills(self, fills: list) -> str:
        if not fills:
            return "No fills this session"
        lines = []
        for f in fills[:8]:
            sym = f.get("symbol") or "?"
            side = f.get("side") or f.get("action") or ""
            qty = f.get("quantity") or f.get("shares") or ""
            px = f.get("price") or f.get("avg_price") or ""
            lines.append(f"{sym} {side} x{qty} @{px}")
        return "\n".join(lines)

    def _cycle_log_text(self, records: list[dict]) -> str:
        if not records:
            return "Connect IBKR."
        lines: list[str] = []
        for r in reversed(list(records or [])[-20:]):
            kind = str(r.get("type") or "cycle").lower()
            ts = str(r.get("ts") or "")
            if "T" in ts:
                ts = ts.split("T", 1)[-1][:8]
            else:
                ts = ts[-8:]
            if self._is_note(r):
                lines.append(f"{ts}  {kind.upper()}  {r.get('msg') or '—'}")
                continue
            strat = r.get("strat") or (r.get("action_obj") or {}).get("strategy") or "—"
            status = self._format_result_status(r.get("result") or {})
            lines.append(f"{ts}  {strat}  {status}")
        return "\n".join(lines) or "Connect IBKR."

    def _apply_clock(self, pulse: dict) -> None:
        view = pulse_clock_view(pulse)
        self.lbl_clock.value = format_desk_clock() or view.get("clock") or "—"
        status = (view.get("session_status") or "closed").lower()
        self.lbl_session_badge.value = view.get("session") or "—"
        self.lbl_session_badge.color = (
            GREEN if status == "regular"
            else (AMBER if status in ("premarket", "postmarket") else MUTED)
        )
        self.lbl_countdown_title.value = (
            "Open time" if view.get("countdown_to") == "open" else "Close time"
        )
        self.lbl_countdown.value = view.get("countdown_human") or "—"
        self.lbl_data_age.value = view.get("ibkr_refresh") or "n/a"
        narrative = (pulse or {}).get("narrative")
        if narrative:
            self.lbl_pulse_narrative.value = str(narrative)
            self.lbl_pulse_narrative.color = TEXT

    # ------------------------------------------------------------ right rail

    @staticmethod
    def _format_return_pct(value) -> tuple[str, str]:
        if value is None:
            return "—", MUTED
        try:
            pct = float(value) * 100.0
        except (TypeError, ValueError):
            return "—", MUTED
        return f"{pct:+.2f}%", GREEN if pct >= 0 else RED

    def _nav_disclaimer(self, perf: dict) -> str:
        """Short tracking-since / updated line for the Account card."""
        src = str((perf or {}).get("source") or "none")
        if src != "ibkr_nav":
            return "IBKR NAV — building history…"
        start = (perf or {}).get("history_start")
        days = (perf or {}).get("history_days")
        as_of = (perf or {}).get("as_of")
        start_bit = "—"
        if start:
            try:
                dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                start_bit = dt.strftime("%Y-%m-%d")
            except ValueError:
                start_bit = str(start)[:10]
        days_bit = f" ({int(days)}d)" if days is not None else ""
        updated_bit = ""
        if as_of:
            try:
                dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
                updated_bit = f" · updated {dt.strftime('%H:%MZ')}"
            except ValueError:
                updated_bit = f" · updated {str(as_of)[:16]}"
        return f"IBKR NAV since {start_bit}{days_bit}{updated_bit}"

    def _apply_return_perf(self, perf: dict) -> None:
        self.lbl_ret_source.value = self._nav_disclaimer(perf)
        self.lbl_ret_source.color = MUTED
        for lbl_attr, col_attr, key in (
            ("lbl_ret_1w", "col_ret_1w", "ret_1w"),
            ("lbl_ret_3m", "col_ret_3m", "ret_3m"),
            ("lbl_ret_1y", "col_ret_1y", "ret_1y"),
        ):
            raw = (perf or {}).get(key)
            label, color = self._format_return_pct(raw)
            getattr(self, lbl_attr).value = label
            getattr(self, lbl_attr).color = color
            col = getattr(self, col_attr, None)
            if col is not None:
                col.visible = raw is not None

    async def _refresh_returns(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._ret_last_fetch and (now - self._ret_last_fetch) < 120.0:
            return
        self._ret_last_fetch = now
        try:
            from abcxauto.account_returns import compute_account_returns

            perf = compute_account_returns(
                equity=self.engine.state.equity,
                daily_pnl=self.engine.state.pnl,
            )
            self._ret_cache = perf
            self._apply_return_perf(perf)
        except Exception:
            self.lbl_ret_source.value = "IBKR NAV — error"
            self.lbl_ret_source.color = MUTED

    def _open_lots_empty(self) -> bool:
        if self.engine.state.positions:
            return False
        lots = (self.engine.state.world_state or {}).get("open_lots")
        if lots:
            return False
        return True

    def _flat_book_news_names(self) -> list[str]:
        """Last scan/think names when the book is flat. Not a sandbox pad."""
        order: list[str] = []

        def _add(raw: Any) -> None:
            su = str(raw or "").upper().strip()
            if su and su not in order:
                order.append(su)

        s = self.engine.state
        for name in s.scan_fetched or []:
            _add(name)
        hits = s.scan_hits if isinstance(s.scan_hits, dict) else {}
        for row in hits.get("rows") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        for it in s.news_items or []:
            if isinstance(it, dict):
                _add(it.get("symbol"))
        try:
            from abcxauto.think_stream import last_look_for_hunt

            last = last_look_for_hunt()
            last_hits = last.get("scan_hits") if isinstance(last.get("scan_hits"), dict) else {}
            for row in last_hits.get("rows") or []:
                if isinstance(row, dict):
                    _add(row.get("symbol"))
            for name in last.get("scan_fetched") or []:
                _add(name)
        except Exception:
            pass
        return order[:14]

    def _news_rail_universe(self) -> list[dict]:
        """Positions when the book is open; scan/think names when it is flat."""
        pos = [
            p
            for p in (self.engine.state.positions or [])
            if isinstance(p, dict) and str(p.get("symbol") or "").strip()
        ]
        if pos:
            return pos
        return [{"symbol": s} for s in self._flat_book_news_names()]

    def _think_news_items(self) -> list[dict]:
        """Headlines the think already fetched. Never invented."""
        items: list[dict] = []
        seen: set[str] = set()

        def _take(it: Any) -> None:
            if not isinstance(it, dict):
                return
            hl = str(it.get("headline") or "").strip()
            if not hl or hl in seen:
                return
            seen.add(hl)
            items.append(it)

        for it in self.engine.state.news_items or []:
            _take(it)
        hits = self.engine.state.scan_hits if isinstance(self.engine.state.scan_hits, dict) else {}
        top = hits.get("news")
        if isinstance(top, list):
            for it in top:
                _take(it)
        for row in hits.get("rows") or []:
            if not isinstance(row, dict):
                continue
            mda = row.get("mda") if isinstance(row.get("mda"), dict) else {}
            news = mda.get("news") if isinstance(mda, dict) else None
            if isinstance(news, list):
                for it in news:
                    _take(it)
        return items

    def _paint_think_news_if_flat(self) -> None:
        if not self._open_lots_empty():
            return
        if self._news_cache:
            return
        items = self._think_news_items()
        if not items:
            return
        self._news_cache = items
        self._render_news_list(items)

    def _render_news_list(self, items: list[dict], *, fallback: str = "") -> None:
        rows: list[ft.Control] = []
        for it in (items or [])[:10]:
            hl = str(it.get("headline") or "").strip()
            if not hl:
                continue
            sym = str(it.get("symbol") or "").upper()
            src = str(it.get("publisher") or "")
            feed = str(it.get("source") or "")
            if not src and feed not in ("mda", "ibkr", "marketdata"):
                src = feed
            if src.startswith("http"):
                try:
                    from urllib.parse import urlparse

                    host = urlparse(src).netloc or src
                except Exception:
                    host = src
            else:
                host = src
            meta = " · ".join(x for x in (sym, host) if x)
            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=8),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            ft.Text(hl, size=13, color=TEXT, weight=ft.FontWeight.W_500),
                            ft.Text(meta or "news", size=11, color=MUTED),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                )
            )
        if not rows:
            rows = [
                ft.Container(
                    padding=ft.Padding.symmetric(vertical=8),
                    content=ft.Text(
                        fallback or "No headlines yet.", size=13, color=MUTED, selectable=True
                    ),
                )
            ]
        self.news_list.controls = rows

    async def _refresh_news(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._news_last_fetch and (now - self._news_last_fetch) < 60.0:
            return
        self._news_last_fetch = now
        think_items = self._think_news_items()
        from abcxauto.news_feed import (
            coalesce_news,
            fetch_agent_news,
            is_real_headline,
            remember_headlines,
        )

        remember_headlines(think_items)
        try:
            unique = await fetch_agent_news(
                self._news_rail_universe(), force=force, per_symbol=5
            )
        except Exception:
            unique = []
        remember_headlines(unique)
        names = [
            str((p or {}).get("symbol") or "").upper().strip()
            for p in self._news_rail_universe()
            if str((p or {}).get("symbol") or "").strip()
        ]
        painted = coalesce_news(unique, names)
        if not any(is_real_headline(it) for it in painted):
            painted = think_items or unique
        self._news_cache = painted
        remember_headlines(painted)
        self._render_news_list(painted)

    # ----------------------------------------------------------------- loops

    async def _reveal_window(self) -> None:
        try:
            await self.page.window.wait_until_ready_to_show()
        except Exception:
            pass
        try:
            self.page.window.visible = True
            await self.page.window.to_front()
        except Exception:
            try:
                self.page.window.visible = True
            except Exception:
                pass
        self._safe_update()

    async def _poll_loop(self) -> None:
        while True:
            try:
                self.engine.drain_apply()
                self._sync_widgets()
                self._safe_update()
            except Exception:
                logger.exception("poll loop tick failed")
            await asyncio.sleep(0.12)

    async def _clock_loop(self) -> None:
        while True:
            try:
                pulse = self.engine.state.reality_pulse or build_reality_pulse(
                    ibkr_connected=self.engine.state.connected,
                    positions=self.engine.state.positions,
                    account=None,
                )
                self._apply_clock(pulse)
                await self._refresh_returns()
                await self._refresh_news()
                self._safe_update()
            except Exception:
                pass
            await asyncio.sleep(1.0)


def main(page: ft.Page) -> None:
    ProTerminal(page).build()


def write_launch_probe(path: str | Path) -> None:
    Path(path).write_text(
        f"title={TITLE}\nexpected={TITLE}\nstatus=Safe\nmainloop_ready=True\n",
        encoding="utf-8",
    )


def run_app() -> None:
    setup_file_logging()
    try:
        from abcxauto.headless import _quiet_ibkr_scanner_noise

        _quiet_ibkr_scanner_noise()
    except Exception:
        pass
    probe = os.environ.get("ABCXAUTO_LAUNCH_PROBE")
    if probe:
        write_launch_probe(probe)
        print(f"ABCXAUTO title={TITLE} mainloop_ready=True status=Safe", flush=True)
        return
    print(f"ABCXAUTO Pro entry={Path(__file__).resolve()} title={TITLE}", flush=True)
    runner = getattr(ft, "run", None) or ft.app
    view = ft.AppView.FLET_APP
    if os.environ.get("ABCXAUTO_PRO_WEB", "").strip() in ("1", "true", "yes"):
        view = ft.AppView.WEB_BROWSER
    kwargs: dict[str, Any] = {"assets_dir": str(ASSETS_DIR), "view": view}
    try:
        runner(main, **kwargs)
    except TypeError:
        runner(main)


if __name__ == "__main__":
    run_app()
