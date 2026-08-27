"""The watch surface: think-stream spine, health strip, book strip, inline scan.

The operator sits and reads the stream to decide whether to trust the desk, so
these cover legibility (markers stay verbatim but paint differently), the three
things that make them intervene (silence, burn, link), and honest empty states.
"""

from __future__ import annotations

import pytest

from abcxauto.pro_desktop import (
    AMBER,
    GREEN,
    MUTED,
    RED,
    TEXT,
    NAV,
    NAV_TITLES,
    ProTerminal,
    grok_sub_state,
    last_card_send_label,
    stream_line_kind,
    think_tail_last_say,
    think_tail_tool_chips,
)


class _Cfg:
    xai_api_key = "test-key"
    model = "grok-4.6"
    trading_mode = "paper"
    ibkr_port = 7497

    @property
    def is_paper(self) -> bool:
        return True


class _Page:
    title = ""
    bgcolor = ""
    padding = 0
    theme_mode = None

    def __init__(self):
        self.window = type(
            "W", (), {"width": 1280, "height": 860, "min_width": 960, "min_height": 720}
        )()
        self.snack_bar = None
        self.overlay = []
        self.controls = []

    def add(self, *_):
        pass

    def update(self):
        pass

    def run_task(self, _):
        pass


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr("abcxauto.pro_desktop.get_config", lambda: _Cfg())
    term = ProTerminal(_Page())
    term._prev_text = ""  # never fall back to a previous look inside a test
    return term


def _walk(control):
    yield control
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from _walk(content)
    for child in getattr(control, "controls", None) or []:
        yield from _walk(child)


def _visible_walk(control):
    """Walk what the operator can actually see — hidden columns keep labels alive."""
    if getattr(control, "visible", True) is False:
        return
    yield control
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from _visible_walk(content)
    for child in getattr(control, "controls", None) or []:
        yield from _visible_walk(child)


def _texts(control) -> list[str]:
    out = []
    for node in _walk(control):
        val = getattr(node, "value", None)
        if isinstance(val, str) and val:
            out.append(val)
    return out


def _line(pro, needle: str):
    """The stream control whose own text is exactly this raw line."""
    for node in _walk(pro.col_stream):
        if getattr(node, "value", None) == needle:
            return node
    raise AssertionError(f"{needle!r} not painted in the spine")


BUFFER = "\n".join(
    [
        "--- GROK ---",
        "[think]",
        "WMT is holding the 103 shelf.",
        "[book]",
        "[scan]",
        "hits=3 quoted=2 src=ibkr",
        "[quote = already have it]",
        "[send]",
        "[say]",
        "Bought WMT 70 with a stop at 101.",
        "[fill]",
        "[stream failed: xAI at capacity]",
        "[think stopped: step ceiling]",
        "",
    ]
)


# ------------------------------------------------------------------ legibility


def test_stream_line_kind_classifies_every_marker_the_desk_emits():
    assert stream_line_kind("--- GROK ---") == "banner"
    assert stream_line_kind("--- GROK JUDGE ---") == "banner"
    assert stream_line_kind("[think]") == "think"
    assert stream_line_kind("[say]") == "say"
    assert stream_line_kind("[book]") == "tool"
    assert stream_line_kind("[send]") == "send"
    assert stream_line_kind("[quote = already have it]") == "cached"
    assert stream_line_kind("[stream failed: boom]") == "alarm"
    assert stream_line_kind("[stream loop]") == "alarm"
    assert stream_line_kind("[stream stalled]") == "alarm"
    assert stream_line_kind("scan timed out") == "alarm"
    assert stream_line_kind("[think stopped: step ceiling]") == "warn"
    assert stream_line_kind("[truncated: max_tokens]") == "warn"
    assert stream_line_kind("[fill]") == "poke"
    assert stream_line_kind("[order_change]") == "poke"
    assert stream_line_kind("[unprotected]") == "poke"
    assert stream_line_kind("hits=3 quoted=2 src=ibkr") == "scan"
    assert stream_line_kind("WMT is holding the shelf.") == "prose"
    assert stream_line_kind("   ") == "blank"


def test_spine_paints_every_marker_verbatim(pro):
    pro.engine.state.think_live = BUFFER
    pro._sync_think_stream()
    painted = _texts(pro.col_stream)
    for raw in BUFFER.splitlines():
        if raw.strip():
            assert raw in painted, f"{raw!r} was not painted verbatim"


def test_spine_makes_think_and_say_visually_distinct(pro):
    pro.engine.state.think_live = BUFFER
    pro._sync_think_stream()
    # Reasoning is quiet, output is not — same font, different weight of voice.
    assert _line(pro, "WMT is holding the 103 shelf.").color == MUTED
    assert _line(pro, "Bought WMT 70 with a stop at 101.").color == TEXT
    assert _line(pro, "[think]").color == MUTED
    assert _line(pro, "[say]").color != MUTED


def test_spine_makes_the_tool_skeleton_scannable(pro):
    pro.engine.state.think_live = BUFFER
    pro._sync_think_stream()
    assert _line(pro, "[book]").weight is not None
    assert _line(pro, "[send]").color == GREEN
    assert _line(pro, "[quote = already have it]").color == MUTED
    assert _line(pro, "[fill]").color == AMBER
    assert _line(pro, "[stream failed: xAI at capacity]").color == RED
    assert _line(pro, "[think stopped: step ceiling]").color == AMBER


def test_spine_rules_off_look_boundaries(pro):
    pro.engine.state.think_live = BUFFER
    pro._sync_think_stream()
    banner = _line(pro, "--- GROK ---")
    holder = [c for c in pro.col_stream.controls if banner in list(_walk(c))][0]
    assert holder is not banner, "the banner needs its own separated block"
    assert getattr(holder, "border", None) is not None


def test_empty_buffer_is_a_stated_state_not_a_blank_void(pro):
    pro.engine.state.think_live = ""
    pro._think_sync_key = "stale"
    pro._sync_think_stream()
    assert pro.col_stream.controls == []
    assert pro.think_live.visible is True
    assert "waiting" in (pro.think_live.value or "").lower()


def test_buffer_arriving_hides_the_fallback_but_keeps_it_readable(pro):
    pro.engine.state.think_live = BUFFER
    pro._sync_think_stream()
    assert pro.think_live.visible is False
    # Copy stream and the plain-text fallback still see the tail.
    assert "Bought WMT 70" in (pro.think_live.value or "")


def test_follow_chip_holds_position_then_jumps_back_to_live(pro):
    assert pro.think_scroll.auto_scroll is True
    assert pro._stream_follow is True
    pro._toggle_stream_follow()
    assert pro.think_scroll.auto_scroll is False
    assert "jump" in (pro.lbl_stream_follow.value or "").lower()
    pro._toggle_stream_follow()
    assert pro.think_scroll.auto_scroll is True
    assert pro.lbl_stream_follow.value == "live"


# ----------------------------------------------------------------- inline scan


def _ranked_hits() -> dict:
    return {
        "source": "ibkr",
        "scan_code": "TOP_PERC_GAIN",
        "ranked": True,
        "rank_meaning": "IBKR scanCode sort order",
        "quoted": 2,
        "rows": [
            {"symbol": "NVDA", "rank": 1, "last": 182.5, "distance": "12.4"},
            {"symbol": "WMT", "rank": 2, "last": 103.08, "on_book": True},
            {"symbol": "IWM", "rank": 3},
        ],
    }


def test_scan_renders_inline_where_the_scan_happened(pro):
    s = pro.engine.state
    s.scan_hits = _ranked_hits()
    s.think_live = BUFFER
    pro._sync_scan_tape()
    pro._sync_think_stream()
    mounted = list(_walk(pro.col_stream))
    assert pro.col_scan in mounted, "the screen belongs at the look that pulled it"
    assert pro.lbl_scan_head in mounted
    blob = " | ".join(_texts(pro.col_scan))
    assert "NVDA" in blob and "182.50" in blob
    assert "103.08" in blob and "on book" in blob
    assert "TOP_PERC_GAIN" in (pro.lbl_scan_head.value or "")


def test_inline_scan_is_collapsible(pro):
    s = pro.engine.state
    s.scan_hits = _ranked_hits()
    s.think_live = BUFFER
    pro._sync_scan_tape()
    pro._sync_think_stream()
    pro._toggle_scan_inline()
    assert pro.col_scan.visible is False
    pro._toggle_scan_inline()
    assert pro.col_scan.visible is True


def test_inline_scan_never_dresses_an_unranked_pull_as_ranked(pro):
    s = pro.engine.state
    s.scan_hits = {
        "source": "symbols",
        "ranked": False,
        "rank_meaning": "not ranked",
        "quoted": 0,
        "rows": [{"symbol": "QQQ", "rank": 9}],
    }
    s.think_live = "--- GROK ---\n[scan]\nhits=1 quoted=0 src=symbols\n"
    pro._sync_scan_tape()
    pro._sync_think_stream()
    assert pro.col_scan in list(_walk(pro.col_stream))
    assert "not ranked" in (pro.lbl_scan_head.value or "")
    blob = " | ".join(_texts(pro.col_scan))
    assert " 9 " not in f" {blob} "


def test_inline_scan_will_not_pair_a_stale_screen_with_this_look(pro):
    """hits= says 3, the payload holds 1 — show the line, not a wrong screen."""
    s = pro.engine.state
    s.scan_hits = {
        "source": "ibkr",
        "ranked": True,
        "quoted": 0,
        "rows": [{"symbol": "QQQ", "rank": 1}],
    }
    s.think_live = BUFFER
    pro._sync_scan_tape()
    pro._sync_think_stream()
    assert pro.col_scan not in list(_walk(pro.col_stream))
    assert "hits=3 quoted=2 src=ibkr" in _texts(pro.col_stream)


def test_scan_is_no_longer_a_dashboard_section(pro):
    from pathlib import Path

    src = Path(pro.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).resolve().parents[1] / src).read_text(encoding="utf-8")
    assert '_section("Scan tape"' not in text
    pro.engine.state.scan_hits = _ranked_hits()
    pro.engine.state.think_live = BUFFER
    pro._sync_scan_tape()
    pro._sync_think_stream()
    mounted = [c for c in _walk(pro._page_overview()) if c is pro.col_scan]
    assert len(mounted) == 1, "the screen must live in exactly one place"


# ---------------------------------------------------------------- health strip


def test_health_strip_calls_out_silence(pro):
    import time

    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    pro.engine._last_grok_mono = time.monotonic() - 2000
    pro._sync_health_strip()
    # Stay-up has no sit clock, so the strip stays "sat". Age going red is the call-out.
    assert pro.lbl_hs_state.value == "sat"
    assert "last look" in (pro.lbl_hs_age.value or "")
    assert pro.lbl_hs_age.color == RED


def test_health_strip_shows_the_backoff_streak_and_wait(pro):
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    pro.engine._fail_streak = 3
    s.backoff_wait_s = 360.0
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "look failed"
    assert pro.lbl_desk_sub.value == "look failed"
    assert "x3" in (pro.lbl_hs_next.value or "")
    assert "360s" in (pro.lbl_hs_next.value or "")


def test_health_strip_falls_back_to_the_park_clock_backoff(pro):
    """No note landed yet — the wait still comes from park_clock, jitter and all."""
    import re

    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    pro.engine._fail_streak = 2
    s.backoff_wait_s = 0.0
    pro._sync_health_strip()
    shown = pro.lbl_hs_next.value or ""
    assert "x2" in shown
    match = re.search(r"next look (\d+)s", shown)
    assert match and int(match.group(1)) > 0


def test_health_strip_reports_looking(pro):
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "Grok"
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "looking"
    assert pro.lbl_hs_state.color == GREEN
    assert pro.lbl_desk_sub.value == "looking"


def test_health_strip_alarms_on_burn_with_no_tickets(pro):
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.looks_since_send = 9
    s.sends_last_look = 0
    s.think_live = "--- GROK ---\n[book]\n[quote]\n[scan]\n"
    s.tool_trace = []
    pro._sync_health_strip()
    assert "9 look(s) since a ticket" in (pro.lbl_hs_burn.value or "")
    assert pro.lbl_hs_burn.color == RED
    assert "3 tool(s)" in (pro.lbl_hs_look.value or "")
    assert "0 send(s)" in (pro.lbl_hs_look.value or "")
    assert not hasattr(pro, "lbl_hs_cost")
    # The band itself goes red so it is visible without reading the numbers.
    assert pro.health_box.border is not None


def test_health_strip_credits_a_ticket(pro):
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.sends_last_look = 1
    s.looks_since_send = 0
    pro._sync_health_strip()
    assert "1 ticket(s) this look" in (pro.lbl_hs_burn.value or "")
    assert pro.lbl_hs_burn.color == GREEN


# ------------------------------------------------- slice 1 strip paints


LIVE_LOOK = "\n".join(
    [
        "--- GROK ---",
        "[think]",
        "mega-cap earnings-flush bounce",
        "[say]",
        "BSX already hit 48.43 today; SCHW already scratched.",
        "large-cap 3pct gap hold",
        "--- GROK ---",
        "[think]",
        "Book is stale and the book is flat.",
        "[book]",
        "[status]",
        "[playbook]",
        "[scan]",
        "",
    ]
)


def test_strip_looking_beats_fail_streak_on_a_live_stream(pro):
    """Paint 1: Grok sub is looking while think is in flight, even with a junk fail_streak."""
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "Thinking"
    s.think_live = LIVE_LOOK
    s.tool_trace = []
    pro.engine._fail_streak = 1
    s.backoff_wait_s = 23.0
    pro._sync_health_strip()
    assert grok_sub_state(
        running=True, status="Thinking", fail_streak=1, tail_moved=False
    ) == "looking"
    assert pro.lbl_hs_state.value == "looking"
    assert pro.lbl_desk_sub.value == "looking"
    assert pro.lbl_hs_state.color == GREEN
    assert "look failed" not in (pro.lbl_hs_next.value or "")
    assert "backing off" not in (pro.lbl_hs_state.value or "")


def test_strip_looking_when_think_tail_moves_while_status_lags(pro):
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    pro.engine._fail_streak = 1
    s.think_live = "--- GROK ---\n[think]\n"
    pro._tail_len = len(s.think_live)
    pro._tail_moved_mono = 0.0
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "look failed"
    s.think_live += "[book]\n[status]\n[playbook]\n[scan]\n"
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "looking"
    assert pro.lbl_desk_sub.value == "looking"


def test_strip_last_line_is_last_say_or_real_card_send(pro, monkeypatch):
    """Paint 2: last line is the last [say] (or card_sends), never Last send: — after a look."""
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "Thinking"
    s.think_live = LIVE_LOOK
    s.brain_strat = ""
    s.sends_last_look = 0
    s.looks_since_send = 20
    s.tool_trace = []
    pro.engine._fail_streak = 1
    pro._sync_health_strip()
    line = pro.lbl_last_send.value or ""
    assert "BSX already hit 48.43 today" in line
    assert "Last send: —" not in line
    assert think_tail_last_say("\n[say]\n?\n--- GROK ---\n[think]\n") == ""
    assert think_tail_last_say(LIVE_LOOK).startswith("BSX already hit")

    s.think_live = "--- GROK ---\n[think]\nweighing\n"
    s.looks_since_send = 2
    assert last_card_send_label(
        [{"card": "large-cap 3pct gap hold", "symbol": "SCHW"}]
    ) == "SCHW · large-cap 3pct gap hold"
    monkeypatch.setattr(
        "abcxauto.pro_desktop.last_card_send_label",
        lambda rows=None: "SCHW · large-cap 3pct gap hold",
    )
    pro._sync_last_line()
    assert "large-cap 3pct gap hold" in (pro.lbl_last_send.value or "")
    assert "Last send: —" not in (pro.lbl_last_send.value or "")


def test_strip_this_look_tools_come_from_think_tail_chips(pro):
    """Paint 3: this look N tools from [book][status][playbook][scan], not empty last_turn.tool_trace."""
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "Thinking"
    s.think_live = LIVE_LOOK
    s.tool_trace = []
    s.sends_last_look = 0
    pro.engine._fail_streak = 1
    pro._sync_health_strip()
    chips = think_tail_tool_chips(LIVE_LOOK)
    assert chips == ["book", "status", "playbook", "scan"]
    assert "4 tool(s)" in (pro.lbl_hs_look.value or "")
    assert "0 send(s)" in (pro.lbl_hs_look.value or "")
    assert pro.lbl_hs_look.color == TEXT


def test_strip_has_no_model_edge_cost(pro):
    """Paint 4: leftover model $ / edge $ behind is gone from the thin strip."""
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "Thinking"
    s.think_live = LIVE_LOOK
    pro._sc_last = {"model_cost_usd": 64.39, "edge_usd": -1531.90, "beating_model": False}
    pro._sync_health_strip()
    strip = pro._status_strip()
    painted = " | ".join(_texts(strip))
    assert not hasattr(pro, "lbl_hs_cost")
    assert "model $" not in painted
    assert "behind" not in painted
    assert "$-1,531" not in painted
    # Path / Tools / Why / Focus / Pace stay out of the strip.
    assert pro.lbl_path not in list(_walk(strip))
    assert pro.lbl_tools not in list(_walk(strip))
    assert pro.lbl_why not in list(_walk(strip))
    assert pro.lbl_focus not in list(_walk(strip))
    assert pro.lbl_pace not in list(_walk(strip))


def test_health_strip_explains_a_quiet_desk_with_link_context(pro):
    s = pro.engine.state
    s.connected = True
    s.reality_pulse = {"session": {"status": "rth"}}
    pro.lbl_ibkr_status.value = "Connected (paper)"
    pro._sync_health_strip()
    assert "Connected (paper)" in (pro.lbl_hs_link.value or "")
    assert "rth" in (pro.lbl_hs_link.value or "")
    assert pro.lbl_hs_link.color == GREEN


def test_looks_since_send_counts_looks_that_bought_nothing():
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    base = {"cycle": 1, "pnl": 0.0, "equity": 1000.0}
    eng._on_cycle({**base, "sends": 0})
    eng._on_cycle({**base, "sends": 0})
    assert eng.state.looks_since_send == 2
    assert eng.state.sends_last_look == 0
    eng._on_cycle({**base, "sends": 1})
    assert eng.state.looks_since_send == 0
    assert eng.state.sends_last_look == 1


# ------------------------------------------------------------------ book strip


def test_book_strip_shows_the_lot_the_stream_is_talking_about(pro):
    s = pro.engine.state
    s.positions = [
        {
            "symbol": "WMT",
            "quantity": 70,
            "sec_type": "STK",
            "avgCost": 103.08,
            "marketPrice": 104.10,
            "conId": 7,
        }
    ]
    s.open_orders = [
        {"order_id": 4445, "symbol": "WMT", "order_type": "STP", "quantity": 70, "aux_price": 101},
        {"order_id": 4446, "symbol": "WMT", "order_type": "LMT", "quantity": 70, "lmtPrice": 107.5},
    ]
    pro._sync_book_strip()
    blob = " | ".join(_texts(pro.col_book_strip))
    assert "WMT" in blob
    assert "4445" in blob and "4446" in blob


def test_book_strip_says_naked_when_a_lot_is_unprotected(pro):
    s = pro.engine.state
    s.positions = [
        {"symbol": "SPY", "quantity": 5, "sec_type": "STK", "avgCost": 500.0, "conId": 1}
    ]
    s.portfolio = {"unprotected_symbols": ["SPY"]}
    pro._sync_book_strip()
    assert "naked" in " | ".join(_texts(pro.col_book_strip))


def test_book_strip_empty_book_says_so(pro):
    pro.engine.state.positions = []
    pro.engine.state.open_orders = []
    pro._sync_book_strip()
    assert "No open lots" in " | ".join(_texts(pro.col_book_strip))


# ------------------------------------------------- pinned strips on every page


def test_every_surface_builds_with_the_spine_and_strips(pro):
    keys = [k for k, _label, _o, _f in NAV]
    pro.page.add(pro._shell())
    for key in keys:
        pro._show_tab(key)
        assert pro.tab == key
        assert pro.content.content is not None
        assert pro.lbl_center_title.value == NAV_TITLES[key]
    shell = pro._shell()
    mounted = list(_walk(shell))
    # Liveness, burn and the book stay on screen while reviewing a drill-down.
    assert pro.lbl_hs_state in mounted
    assert pro.lbl_hs_burn in mounted
    assert pro.col_book_strip in mounted


def test_dashboard_is_the_stream_not_a_card_wall(pro):
    page = list(_walk(pro._page_overview()))
    assert pro.think_scroll in page
    # The cards moved to the pinned strip; the spine owns the surface. Cut metrics
    # stay in the hidden column so their sync keeps working, so look at what paints.
    painted = list(_visible_walk(pro._page_overview()))
    assert pro.think_scroll in painted
    assert pro.lbl_lot_count not in painted
    assert pro.lbl_status not in painted
    assert pro.lbl_open_upnl not in painted


# -------------------------------------------------------------------- playbook


def test_playbook_label_replaces_notebook_jargon(pro):
    labels = {k: label for k, label, _o, _f in NAV}
    assert labels["notebook"] == "Playbook"
    assert NAV_TITLES["notebook"] == "Playbook"
    # The nav key and the internal identifiers stay put.
    assert hasattr(pro, "_page_notebook")
    assert hasattr(pro, "_sync_notebook_page")


def test_playbook_card_with_no_sends_reads_no_sends_yet(pro):
    card = {"name": "shelf reclaim", "status": "testing", "ticket": "stock_with_stop"}
    blob = " | ".join(_texts(pro._notebook_card(card, {})))
    assert "no sends yet" in blob
    assert "0.00" not in blob


def test_playbook_card_with_sends_but_no_fills_says_so(pro):
    card = {"name": "shelf reclaim", "status": "working"}
    attrib = {
        "shelf reclaim": {
            "card": "shelf reclaim",
            "sends": 2,
            "attributed_fills": 0,
            "realized_pnl": 0.0,
        }
    }
    blob = " | ".join(_texts(pro._notebook_card(card, attrib)))
    assert "no fills yet" in blob
    assert "$+0.00" not in blob


def test_playbook_card_with_fills_shows_realized(pro):
    card = {"name": "shelf reclaim", "status": "working"}
    attrib = {
        "shelf reclaim": {
            "card": "shelf reclaim",
            "sends": 2,
            "attributed_fills": 2,
            "realized_pnl": 41.5,
        }
    }
    blob = " | ".join(_texts(pro._notebook_card(card, attrib)))
    assert "$+41.50" in blob


def test_card_scores_table_does_not_print_zero_for_an_unjoined_send(pro, monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.load_lab", lambda: {"cards": []})
    monkeypatch.setattr(
        "abcxauto.lab_playbook.card_scores",
        lambda cards=None: [
            {"card": "shelf reclaim", "sends": 3, "attributed_fills": 0, "realized_pnl": 0.0}
        ],
    )
    pro._sync_sc_cards()
    blob = " | ".join(_texts(pro.col_sc_cards))
    assert "no fills yet" in blob
    assert "$+0.00" not in blob


def test_card_scores_table_empty_state_is_honest(pro, monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.load_lab", lambda: {"cards": []})
    monkeypatch.setattr("abcxauto.lab_playbook.card_scores", lambda cards=None: [])
    pro._sync_sc_cards()
    assert "No card-attributed sends yet" in " | ".join(_texts(pro.col_sc_cards))
