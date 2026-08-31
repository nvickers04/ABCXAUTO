"""The watch surface: think-stream spine, health strip, inline scan.

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
    format_token_count,
    grok_sub_state,
    last_card_send_label,
    session_cap_idle_line,
    stream_line_kind,
    stream_view_lines,
    think_tail_in_flight,
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
    assert stream_line_kind("--- CLERK ---") == "banner"
    assert stream_line_kind("=== run 2026-08-31 11:56 CT pid=2752 sha=abc1234 ===") == "banner"
    assert stream_line_kind("[think]") == "think"
    assert stream_line_kind("[say]") == "say"
    assert stream_line_kind("[clerk]") == "clerk"
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
    assert stream_line_kind("[stop_dist]") == "poke"
    assert stream_line_kind("hits=3 quoted=2 src=ibkr") == "scan"
    assert stream_line_kind('{"open_lots": [], "nl": 35000}') == "json"
    assert stream_line_kind("WMT is holding the shelf.") == "prose"
    assert stream_line_kind("   ") == "blank"


def test_stream_view_lines_collapses_json_objects_and_keeps_chips():
    """Pane view skips the dump body; [think]/[say]/[tool] chips stay."""
    dump = '{"open_lots": [' + ('{"symbol": "SPY", "qty": 1},' * 80) + "]}"
    args = '{"symbol": "SPY"}'
    body = "\n".join(
        [
            "--- GROK ---",
            "[book]",
            args,
            dump,
            "[playbook]",
            '{"cards": [{"id": 1}]}',
            "[scan]",
            "hits=3 quoted=2 src=ibkr",
            '{"rows": [{"symbol": "NVDA"}]}',
            "[think]",
            "WMT is holding the 103 shelf.",
            "[say]",
            "watching the gap",
            "",
        ]
    )
    view = stream_view_lines(body)
    assert "[book]" in view
    assert "[playbook]" in view
    assert "[scan]" in view
    assert "[think]" in view
    assert "[say]" in view
    assert "--- GROK ---" in view
    assert "hits=3 quoted=2 src=ibkr" in view
    assert "WMT is holding the 103 shelf." in view
    assert "watching the gap" in view
    blob = "\n".join(view)
    assert dump not in blob
    assert args not in blob
    assert '"open_lots"' not in blob
    assert '"cards"' not in blob
    assert '"rows"' not in blob
    assert any(line.startswith("{json ") and line.endswith(" chars}") for line in view)
    # args+result after [book] collapse to one stub, not two dumps.
    book_at = view.index("[book]")
    assert view[book_at + 1].startswith("{json ")
    assert view[book_at + 2] == "[playbook]"
    # No JSON → chips and prose stay verbatim (blanks dropped).
    raw_look = "[think]\nWMT is holding the 103 shelf.\n[say]\nwatching the gap\n"
    assert stream_view_lines(raw_look) == [
        "[think]",
        "WMT is holding the 103 shelf.",
        "[say]",
        "watching the gap",
    ]


def test_pane_and_copy_read_full_session_not_glass_tail(pro, tmp_path, monkeypatch):
    """A day bigger than the 8kb stub and 24k RAM window still paints and copies."""
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "_et_session_day", lambda: "2026-08-28")
    early = "LOOK_ONE\n" + ("a" * 28000) + "\n"
    late = "LOOK_TWO\n" + ("b" * 4000) + "\n"
    session = early + late
    assert len(session) > 24000
    day_dir = tmp_path / "think_session"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "2026-08-28.txt").write_text(session, encoding="utf-8")
    tail = session[-8000:]
    (tmp_path / "think_tail.txt").write_text(tail, encoding="utf-8")
    live = session[-24000:]
    pro.engine.state.think_live = live
    assert "LOOK_ONE" not in live
    assert "LOOK_ONE" not in tail
    assert len(tail) <= 8000

    got = ts.think_session_text(live)
    assert "LOOK_ONE" in got
    assert "LOOK_TWO" in got
    assert len(got) == len(session)

    pro._think_sync_key = None
    pro._sync_think_stream()
    painted = "\n".join(_texts(pro.col_stream))
    assert "LOOK_ONE" in painted
    assert "LOOK_TWO" in painted
    assert painted != live
    assert painted != (pro.engine.state.think_live or "")[-24000:]
    assert "LOOK_ONE" not in (pro.engine.state.think_live or "")[-24000:]
    assert pro.think_live.visible is False
    assert "LOOK_ONE" in pro._pane_stream_text()
    status = (pro.lbl_stream_status.value or "").replace(",", "")
    assert str(len(session)) in status
    copied = pro._copy_stream_text()
    assert "LOOK_ONE" in copied
    assert "LOOK_TWO" in copied
    assert len(copied) == len(session)
    clip: list[str] = []
    pro.page.set_clipboard = lambda t: clip.append(t)
    pro._copy_stream()
    assert clip == [session]


def test_spine_collapses_json_object_lines_and_keeps_chips(pro):
    """Paid JSON stays on Copy/keep-file; the pane keeps the look readable."""
    dump = '{"open_lots": [' + ('{"symbol": "SPY", "qty": 1},' * 80) + "]}"
    body = "\n".join(
        [
            "--- GROK ---",
            "[book]",
            dump,
            "[playbook]",
            '{"cards": [{"id": 1, "name": "gap"}]}',
            "[scan]",
            "hits=3 quoted=2 src=ibkr",
            "[think]",
            "WMT is holding the 103 shelf.",
            "[say]",
            "watching the gap",
            "",
        ]
    )
    pro.engine.state.think_live = body
    pro._sync_think_stream()
    painted = _texts(pro.col_stream)
    for chip in ("[book]", "[playbook]", "[scan]", "[think]", "[say]"):
        assert chip in painted
    assert "WMT is holding the 103 shelf." in painted
    assert "watching the gap" in painted
    blob = "\n".join(painted)
    assert dump not in blob
    assert '"open_lots"' not in blob
    assert '"cards"' not in blob
    assert any(v.startswith("{json ") and v.endswith(" chars}") for v in painted)
    stub = next(v for v in painted if v.startswith("{json "))
    assert _line(pro, stub).color == MUTED
    copied = pro._copy_stream_text()
    assert dump in copied
    assert '"open_lots"' in copied
    assert "watching the gap" in copied


def test_pane_collapses_keep_file_json_without_clipping_disk(pro, tmp_path, monkeypatch):
    """#137 archive stays; this PR only changes what the pane paints."""
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "_et_session_day", lambda: "2026-08-31")
    dump = '{"unique_paid": "SNDK_GAP", "hits": [' + ("1," * 400) + "0]}"
    session = "\n".join(
        [
            "--- GROK ---",
            "[book]",
            dump,
            "[think]",
            "gap still holds.",
            "[say]",
            "watching the gap",
            "",
        ]
    )
    day_dir = tmp_path / "think_session"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "2026-08-31.txt"
    path.write_text(session, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    pro.engine.state.think_live = session[-200:]
    pro._think_sync_key = None
    pro._sync_think_stream()
    painted = "\n".join(_texts(pro.col_stream))
    assert "[book]" in painted
    assert "[think]" in painted
    assert "[say]" in painted
    assert "gap still holds." in painted
    assert "watching the gap" in painted
    assert "SNDK_GAP" not in painted
    assert dump not in painted
    assert path.read_text(encoding="utf-8") == before
    assert "SNDK_GAP" in before
    assert pro._copy_stream_text() == before


def test_spine_paints_every_marker_verbatim(pro):
    pro.engine.state.think_live = BUFFER
    pro._sync_think_stream()
    painted = _texts(pro.col_stream)
    for raw in BUFFER.splitlines():
        if raw.strip():
            assert raw in painted, f"{raw!r} was not painted verbatim"


def test_spine_makes_clerk_and_grok_banners_distinct(pro):
    pro.engine.state.think_live = "\n".join(
        [
            "--- CLERK ---",
            "[clerk]",
            "Wake Grok.",
            "hits=1 screens=1 deepest=-3.8% SNDK src=ibkr",
            "--- GROK ---",
            "[think]",
            "weigh tape",
            "[say]",
            "watching the gap",
            "",
        ]
    )
    pro._sync_think_stream()
    assert _line(pro, "--- CLERK ---").color == MUTED
    assert _line(pro, "[clerk]").color == MUTED
    assert _line(pro, "--- GROK ---").color != MUTED
    assert _line(pro, "[say]").color != MUTED
    assert _line(pro, "watching the gap").color == TEXT


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
    assert "Bought WMT 70" in "\n".join(_texts(pro.col_stream))
    assert "Bought WMT 70" in pro._copy_stream_text()


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


def test_health_strip_shows_the_fail_streak_without_a_sit(pro):
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
    assert "next look" not in (pro.lbl_hs_next.value or "")
    assert "360s" not in (pro.lbl_hs_next.value or "")


def test_health_strip_does_not_invent_a_park_clock_sit(pro):
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    pro.engine._fail_streak = 2
    s.backoff_wait_s = 0.0
    pro._sync_health_strip()
    shown = pro.lbl_hs_next.value or ""
    assert "x2" in shown
    assert "next look" not in shown


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
    assert "9 look(s) since a ticket" not in (pro.lbl_hs_burn.value or "")
    assert "look(s) since a ticket" not in (pro.lbl_hs_burn.value or "")
    assert "3 tool(s)" in (pro.lbl_hs_look.value or "")
    assert "0 send(s)" in (pro.lbl_hs_look.value or "")
    assert not hasattr(pro, "lbl_hs_cost")
    assert pro.health_box.border is None


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
    assert grok_sub_state(
        running=True, status="On", fail_streak=1, tail_moved=False, tail_live=True
    ) == "looking"
    assert grok_sub_state(
        running=True, status="On", fail_streak=1, tail_moved=False, tail_live=False
    ) == "look failed"
    assert grok_sub_state(
        running=True, status="On", fail_streak=0, tail_moved=False, tail_live=False
    ) == "sat"
    assert grok_sub_state(
        running=True, status="Idle", fail_streak=0, tail_live=True
    ) == "idle"
    assert grok_sub_state(
        running=False, paused=True, status="Paused", tail_live=True
    ) == "paused"
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
    pro._tail_fp = s.think_live[-512:]
    pro._tail_moved_mono = 0.0
    pro._sync_health_strip()
    # Open [think] is in flight even when length did not grow this tick.
    assert think_tail_in_flight(s.think_live)
    assert pro.lbl_hs_state.value == "looking"
    s.think_live += "[book]\n[status]\n[playbook]\n[scan]\n"
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "looking"
    assert pro.lbl_desk_sub.value == "looking"


def test_strip_fail_streak_when_the_look_is_actually_idle(pro):
    """fail_streak/sat only win when the tail is idle — a finished say, no motion."""
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    done = (
        "--- GROK ---\n"
        "[say]\n"
        "Holding for the open. Nothing to chase premarket.\n"
    )
    s.think_live = done
    pro.engine._fail_streak = 1
    pro._tail_len = len(done)
    pro._tail_fp = done[-512:]
    pro._tail_moved_mono = 0.0
    pro._sync_health_strip()
    assert not think_tail_in_flight(done)
    assert pro.lbl_hs_state.value == "look failed"
    pro.engine._fail_streak = 0
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "sat"


FRIDAY_0800 = "\n".join(
    [
        "--- GROK ---",
        "[think]",
        "premarket, 34 minutes to open",
        "[say]",
        "Premarket, 34 minutes to open - pulling the book, playbook, "
        "and option facts before any ticket.",
        "[book]",
        "[status]",
        "[playbook]",
        "[option_facts]",
        "[fills]",
        "[scan]",
        "[quote]",
        "[news]",
        "--- GROK ---",
        "[say]",
        "Premarket dump is PYPL/MRVL - quoting those names and checking "
        "whether any flush card is actually on before the open.",
        "--- GROK ---",
        "[think]",
        "Let me analyze the situation carefully. Got it - thanks for "
        "laying out the full setup...",
        "",
    ]
)


def test_strip_looking_friday_0800_moving_tail_and_recent_say(pro):
    """Friday 8:00 CT glass: live think_tail + PYPL/MRVL say paints looking, not sat.

    Engine status is On (think returned or not yet Thinking) and fail_streak
    may be set. The tail is mid-look: chips, a recent say, an open [think].
    That is looking. Sat is only correct when the look is actually idle.
    """
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    s.think_live = FRIDAY_0800
    s.tool_trace = []
    s.sends_last_look = 0
    pro.engine._fail_streak = 1
    s.backoff_wait_s = 0.0
    # Already-painted frozen tick: length did not grow, 2.5s hold expired.
    pro._tail_len = len(FRIDAY_0800)
    pro._tail_fp = FRIDAY_0800[-512:]
    pro._tail_moved_mono = 0.0
    pro._sync_health_strip()
    assert think_tail_in_flight(FRIDAY_0800)
    assert "PYPL/MRVL" in think_tail_last_say(FRIDAY_0800)
    assert think_tail_tool_chips(FRIDAY_0800) == [
        "book",
        "status",
        "playbook",
        "option_facts",
        "fills",
        "scan",
        "quote",
        "news",
    ]
    assert grok_sub_state(
        running=True, status="On", fail_streak=1, tail_moved=False, tail_live=True
    ) == "looking"
    assert pro.lbl_hs_state.value == "looking"
    assert pro.lbl_desk_sub.value == "looking"
    assert pro.lbl_hs_state.color == GREEN
    assert "look failed" not in (pro.lbl_hs_next.value or "")
    assert "8 tool(s)" in (pro.lbl_hs_look.value or "")
    assert "PYPL/MRVL" in (pro.lbl_last_send.value or "")
    # Moving tail + recent say stays looking.
    s.think_live += " quoting PYPL/MRVL before the open\n"
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "looking"
    pro.engine._fail_streak = 0
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "looking"


def test_strip_idle_cap_is_idle_even_with_a_leftover_think(pro):
    """Session cap / idle beats a leftover open think. That look is not live."""
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "Idle"
    s.think_live = FRIDAY_0800
    pro.engine._session_capped = True
    pro.engine._fail_streak = 0
    pro._sync_health_strip()
    assert grok_sub_state(
        running=True, status="Idle", fail_streak=0, tail_live=True
    ) == "idle"
    assert pro.lbl_hs_state.value == "idle"
    assert pro.lbl_desk_sub.value == "idle"
    assert "sat" not in (pro.lbl_hs_state.value or "")
    assert "idle" in (pro.lbl_hs_next.value or "")


def test_session_cap_idle_line_names_the_token_cap():
    assert format_token_count(2_533_000) == "2.533M"
    assert format_token_count(2_500_000) == "2.5M"
    line = session_cap_idle_line(
        {
            "why": "tokens",
            "tokens": 2_533_000,
            "token_cap": 2_500_000,
            "looks": 40,
            "look_cap": 160,
            "hit": True,
        }
    )
    assert line == "token cap 2.533M/2.5M — idle"
    assert "sat" not in line
    looks = session_cap_idle_line(
        {
            "why": "looks",
            "looks": 160,
            "look_cap": 160,
            "tokens": 10,
            "token_cap": 2_500_000,
            "hit": True,
        }
    )
    assert line != looks
    assert looks == "look cap 160/160 — idle"


def test_cap_idle_paints_idle_and_token_numbers_not_sat(pro, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.session_caps.usage",
        lambda session="": {
            "why": "tokens",
            "tokens": 2_533_000,
            "token_cap": 2_500_000,
            "looks": 40,
            "look_cap": 160,
            "hit": True,
        },
    )
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.paused = False
    s.status = "Idle"
    s.think_live = FRIDAY_0800
    pro.engine._session_capped = True
    pro.engine._fail_streak = 0
    pro._sync_health_strip()
    assert pro.lbl_hs_state.value == "idle"
    assert pro.lbl_desk_sub.value == "idle"
    assert "sat" not in (pro.lbl_hs_state.value or "")
    assert "sat" not in (pro.lbl_desk_sub.value or "")
    shown = pro.lbl_hs_next.value or ""
    assert "2.533M/2.5M" in shown
    assert "token cap" in shown
    assert "idle" in shown


def test_paused_is_operator_stop_not_sat(pro):
    s = pro.engine.state
    s.running = False
    s.autonomous = False
    s.paused = True
    s.status = "Paused"
    s.think_live = FRIDAY_0800
    pro._refresh_run_btn()
    pro._sync_health_strip()
    assert pro.lbl_desk.value == "Paused"
    assert pro.lbl_hs_state.value == "paused"
    assert "sat" not in (pro.lbl_hs_state.value or "")
    assert "sat" not in (pro.lbl_desk_sub.value or "")


def test_lots_chip_is_open_book_count_not_slot_cap(pro, monkeypatch):
    class _Cfg25:
        xai_api_key = "test-key"
        model = "grok-4.6"
        trading_mode = "paper"
        ibkr_port = 7497
        max_open_positions = 25

        @property
        def is_paper(self) -> bool:
            return True

    monkeypatch.setattr("abcxauto.pro_desktop.get_config", lambda: _Cfg25())
    s = pro.engine.state
    s.equity = 35_000.0
    s.positions = [
        {"symbol": "HPQ", "quantity": 1, "sec_type": "OPT", "right": "C",
         "strike": 28, "expiration": "20260918", "conId": 1},
        {"symbol": "IWM", "quantity": 1, "sec_type": "OPT", "right": "C",
         "strike": 306, "expiration": "20260821", "conId": 2},
        {"symbol": "QQQ", "quantity": 1, "sec_type": "OPT", "right": "C",
         "strike": 735, "expiration": "20260821", "conId": 3},
        {"symbol": "SPY", "quantity": 0, "sec_type": "STK", "conId": 4},
    ]
    pro._sync_widgets()
    assert pro.lbl_lot_count.value == "3"
    assert "/25" not in (pro.lbl_lot_count.value or "")
    assert "25" not in (pro.lbl_lot_count.value or "")
    assert "/" not in (pro.lbl_lot_count.value or "")


def test_scorecard_page_has_no_path_mix(pro):
    page = pro._page_scorecard()
    blob = " | ".join(_texts(page))
    assert "Path:" not in blob
    assert "Mix:" not in blob
    assert "Path" not in blob
    assert "Mix" not in blob
    assert not hasattr(pro, "lbl_sc_path")
    assert not hasattr(pro, "lbl_sc_mix")
    painted = list(_visible_walk(page))
    assert pro.lbl_path not in painted
    assert pro.lbl_mix not in painted


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


def test_looks_since_send_is_not_a_tally():
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    base = {"cycle": 1, "pnl": 0.0, "equity": 1000.0}
    eng._on_cycle({**base, "sends": 0})
    eng._on_cycle({**base, "sends": 0})
    assert eng.state.looks_since_send == 0
    assert eng.state.sends_last_look == 0
    eng._on_cycle({**base, "sends": 1})
    assert eng.state.looks_since_send == 0
    assert eng.state.sends_last_look == 1


# ------------------------------------------------------------------ book strip (gone from chrome)


def test_chrome_does_not_mount_book_strip(pro):
    """Lot chips used to pin above every tab. The Positions blotter owns them."""
    assert not hasattr(pro, "col_book_strip")
    assert not hasattr(pro, "lbl_book_strip")
    assert not hasattr(pro, "_sync_book_strip")
    strip = pro._status_strip()
    painted = list(_visible_walk(strip))
    for kept in (pro.lbl_halt, pro.lbl_lot_count, pro.lbl_unprotected):
        assert kept in painted
    pos = list(_walk(pro._page_positions()))
    assert pro.col_lots in pos


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
    # Liveness and burn stay on screen while reviewing a drill-down.
    assert pro.lbl_hs_state in mounted
    assert pro.lbl_hs_burn in mounted
    assert pro.lbl_halt in mounted
    assert pro.lbl_lot_count in mounted
    assert pro.lbl_unprotected in mounted
    assert not hasattr(pro, "col_book_strip")


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
    assert pro.lbl_why not in painted
    assert pro.lbl_path not in painted
    assert pro.lbl_mix not in painted


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
    assert "$+0.00" not in blob
    assert "hit: none" in blob
    assert "avg R" in blob
    assert "cost-alloc" in blob
    # No hit-column dash farm — the header is hidden when nothing is calibrated.
    assert " | hit | " not in f" | {blob} | "


def test_card_scores_table_empty_state_is_honest(pro, monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.load_lab", lambda: {"cards": []})
    monkeypatch.setattr("abcxauto.lab_playbook.card_scores", lambda cards=None: [])
    pro._sync_sc_cards()
    assert "No card-attributed sends yet" in " | ".join(_texts(pro.col_sc_cards))


def test_scorecard_windows_paint_vs_spy_blank_not_invented(pro):
    pro._sync_sc_windows({
        "windows": {
            "15m": {
                "book_return_pct": 0.1,
                "edge_usd": 12.0,
                "beating_model": True,
                "coverage": "ok",
                "spy_return_pct": None,
            },
            "inception": {
                "book_return_pct": -4.0,
                "edge_usd": -1544.0,
                "beating_model": False,
                "coverage": "ok",
                "spy_return_pct": None,
            },
        }
    })
    blob = " | ".join(_texts(pro.col_sc_windows))
    assert "vs SPY" in blob
    assert "15m" in blob
    assert "inception" in blob
    assert "-4.00%" in blob
    assert "—" in blob


def test_card_scores_hit_column_only_when_calibrated(pro, monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.load_lab", lambda: {"cards": []})
    monkeypatch.setattr(
        "abcxauto.lab_playbook.attach_card_honesty",
        lambda rows, **_k: rows,
    )
    monkeypatch.setattr(
        "abcxauto.lab_playbook.card_scores",
        lambda cards=None: [
            {
                "card": "flush bounce",
                "sends": 8,
                "resolved": 8,
                "resolved_wins": 4,
                "resolved_pnl": 40.0,
                "retire_if": {"max_loss_usd": 10.0},
                "calibration": {"hit_rate": 50.0, "hit_rate_gap": -20.0},
                "honesty": {"cost_allocated_pnl": 12.5},
            }
        ],
    )
    pro._sync_sc_cards()
    blob = " | ".join(_texts(pro.col_sc_cards))
    assert "hit: none" not in blob
    assert "50%" in blob
    assert "0.50R" in blob
    assert "$+12.50" in blob
