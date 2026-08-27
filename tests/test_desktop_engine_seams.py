"""Seams between the Pro cockpit and the linear-think / setup-card engine.

The cockpit is a separate refactor from the engine work it reads. These are the
joints that broke silently once: a lifecycle note whose message never painted, a
scan screen the UI could not see, a desk lock nothing released.
"""

from __future__ import annotations

import json

import pytest

from abcxauto.pro_desktop import NOTE_COLOR, ProTerminal


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
    return ProTerminal(_Page())


def _row_text(control) -> str:
    """Flatten a blotter row / label tree into its text."""
    bits: list[str] = []
    val = getattr(control, "value", None)
    if isinstance(val, str) and val:
        bits.append(val)
    content = getattr(control, "content", None)
    if content is not None:
        bits.append(_row_text(content))
    for child in getattr(control, "controls", None) or []:
        bits.append(_row_text(child))
    return " ".join(b for b in bits if b)


def _activity_text(pro_ui) -> str:
    return " | ".join(_row_text(c) for c in pro_ui.col_activity.controls)


# ---------------------------------------------------------------- lifecycle notes


def test_retry_note_message_reaches_the_activity_log(pro):
    """``_note_backoff`` is the only place the operator learns xAI refused."""
    eng = pro.engine
    eng._fail_streak = 3
    eng._note_backoff({"_stream_error": "RESOURCE_EXHAUSTED: model at capacity"}, 360.0)
    pro._sync_activity()
    blob = _activity_text(pro)
    assert "RETRY" in blob
    assert "xAI at capacity (x3)" in blob
    assert "next look 360s" in blob
    assert "retry" in NOTE_COLOR


def test_every_note_kind_paints_its_message(pro):
    """Kind whitelists rot. PARK / UNIVERSE / ERR must not blank out."""
    eng = pro.engine
    for kind, msg in (
        ("PARK", "Overnight park - Grok down"),
        ("UNIVERSE", "sandbox refreshed n=40"),
        ("ERR", "TWS not listening"),
        ("CONNECT", "IBKR linked"),
    ):
        eng._note(kind, msg)
    pro._sync_activity()
    blob = _activity_text(pro)
    for msg in (
        "Overnight park",
        "sandbox refreshed n=40",
        "TWS not listening",
        "IBKR linked",
    ):
        assert msg in blob
    text = pro._cycle_log_text(eng.state.records)
    assert "sandbox refreshed n=40" in text


def test_cycle_rows_are_not_treated_as_notes(pro):
    s = pro.engine.state
    s.records = [
        {
            "type": "cycle",
            "ts": "2026-08-20T14:00:00",
            "strat": "vertical_spread",
            "result": {"status": "ok"},
        }
    ]
    pro._sync_activity()
    blob = _activity_text(pro)
    assert "vertical_spread" in blob


# -------------------------------------------------------------------- scan tape


def test_scan_tape_paints_rank_and_live_last(pro):
    s = pro.engine.state
    s.scan_hits = {
        "source": "ibkr",
        "scan_code": "TOP_PERC_GAIN",
        "ranked": True,
        "rank_meaning": "IBKR scanCode sort order",
        "quoted": 2,
        "rows": [
            {"symbol": "NVDA", "on_book": False, "rank": 1, "last": 182.5,
             "quote_source": "ibkr_live", "distance": "12.4"},
            {"symbol": "SPY", "on_book": True, "rank": 2, "last": 641.02,
             "quote_source": "ibkr_live"},
            {"symbol": "IWM", "on_book": False, "rank": 3},
        ],
    }
    pro._sync_scan_tape()
    head = pro.lbl_scan_head.value or ""
    assert "3 hits" in head
    assert "2 quoted" in head
    assert "TOP_PERC_GAIN" in head
    assert "scanCode sort order" in head
    blob = " | ".join(_row_text(c) for c in pro.col_scan.controls)
    assert "NVDA" in blob and "182.50" in blob
    assert "641.02" in blob
    assert "on book" in blob
    # No quote on the third hit — say so, do not invent a price.
    assert "IWM" in blob


def test_scan_tape_hides_rank_when_the_screen_is_not_ranked(pro):
    s = pro.engine.state
    s.scan_hits = {
        "source": "symbols",
        "ranked": False,
        "rank_meaning": "not ranked",
        "quoted": 0,
        "rows": [{"symbol": "QQQ", "rank": 9, "on_book": False}],
    }
    pro._sync_scan_tape()
    assert "not ranked" in (pro.lbl_scan_head.value or "")
    blob = " | ".join(_row_text(c) for c in pro.col_scan.controls)
    # Row index, never IBKR's rank field, when the source did not rank.
    assert " 9 " not in f" {blob} "


def test_scan_tape_empty_says_grok_runs_the_scanner(pro):
    pro._sync_scan_tape()
    assert "Grok runs the scanner" in (pro.lbl_scan_head.value or "")
    assert pro.col_scan.controls == []


def test_scan_hits_survive_the_engine_payload():
    """brain stamps the snap, _host_think forwards it, _apply_cycle keeps it."""
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    eng._apply(
        "cycle",
        {
            "cycle": 1,
            "pnl": 0.0,
            "equity": 100_000.0,
            "scan_hits": {"source": "ibkr", "ranked": True, "rows": [{"symbol": "SPY"}]},
            "world_state": {"opportunities": [{"symbol": "SPY", "source": "scan"}]},
        }
    )
    assert eng.state.scan_hits["source"] == "ibkr"
    # opportunities used to be dropped whenever the payload omitted the key.
    assert [o["symbol"] for o in eng.state.opportunities] == ["SPY"]


def test_news_items_survive_the_engine_payload():
    """Think-fetched headlines must land on engine state even when only on world_state."""
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    eng._apply(
        "cycle",
        {
            "cycle": 1,
            "pnl": 0.0,
            "equity": 100_000.0,
            "scan_fetched": ["INTU", "FIG"],
            "world_state": {
                "news_items": [
                    {"symbol": "INTU", "headline": "Intuit beats"},
                    {"symbol": "FIG", "headline": "Figma tape"},
                ],
                "scan_fetched": ["INTU", "FIG"],
            },
        },
    )
    assert [n["symbol"] for n in eng.state.news_items] == ["INTU", "FIG"]
    assert eng.state.news_items[0]["headline"] == "Intuit beats"
    assert eng.state.scan_fetched == ["INTU", "FIG"]


def test_scan_tool_stamps_hits_on_the_snap(monkeypatch):
    """The rich rows go to Grok in the tool result; the snap carries them to the UI."""
    import asyncio

    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def _fake_scan(**_kw):
        return {
            "ok": True,
            "source": "ibkr",
            "arena": 12,
            "scan_code": "HIGH_OPT_VOLUME",
            "symbols": ["NVDA"],
            "hits": [{"symbol": "NVDA", "on_book": False, "rank": 1, "last": 182.5}],
            "applied": {},
            "persisted": False,
            "ranked": True,
            "rank_meaning": "IBKR scanCode sort order",
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    snap: dict = {}
    blob = asyncio.run(
        _run_tool(
            "scan",
            {"scan_code": "HIGH_OPT_VOLUME"},
            connector=None,
            world=world,
            snap=snap,
            turn=BrainTurn(),
        )
    )
    assert json.loads(blob)["ranked"] is True
    hits = snap["scan_hits"]
    assert hits["ranked"] is True
    assert hits["quoted"] == 1
    assert hits["scan_code"] == "HIGH_OPT_VOLUME"
    assert hits["rows"][0]["symbol"] == "NVDA"
    assert hits["rows"][0]["last"] == 182.5
    # The MDA tape stays symbol stubs — an IBKR last must never paint as mda_last.
    assert all("last" not in row for row in world.opportunities)


# --------------------------------------------------------------- structure lessons


def test_structure_lessons_paint_on_the_dashboard(pro):
    pro.engine.state.structure_lessons = [
        {
            "strategy": "vertical_spread",
            "symbol": "SPY",
            "reason_code": "legs_not_defined_risk",
            "message": "long leg missing",
        }
    ]
    pro._sync_lessons_line()
    assert pro.lbl_lessons.visible is True
    assert "vertical_spread SPY" in (pro.lbl_lessons.value or "")
    assert "legs_not_defined_risk" in (pro.lbl_lessons.value or "")
    pro.engine.state.structure_lessons = []
    pro._sync_lessons_line()
    assert pro.lbl_lessons.visible is False


# ----------------------------------------------------------------- setup cards


def test_notebook_page_flags_an_unsendable_card_ticket(pro, monkeypatch, tmp_path):
    lab = tmp_path / "playbook_lab.json"
    lab.write_text(
        json.dumps(
            {
                "revision": 4,
                "mode": "explore",
                "written_at": "2026-08-20T13:00:00+00:00",
                "instructions": "CARD gap fade",
                "cards": [
                    {"name": "gap fade", "ticket": "bracket", "status": "working"},
                    {"name": "moon shot", "ticket": "naked_call", "status": "testing"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(lab))
    pro._sync_notebook_page(force=True)
    assert "2 setup card(s)" in (pro.lbl_notebook_meta.value or "")
    assert "UNSENDABLE ticket: naked_call" in (pro.lbl_notebook_meta.value or "")
    names = " | ".join(_row_text(c) for c in pro.col_notebook_cards.controls)
    assert "gap fade" in names and "moon shot" in names
    assert pro.notebook_raw_panel.visible is False


def test_notebook_page_survives_a_cleared_book(pro, monkeypatch, tmp_path):
    """``clear_lab`` leaves revision 0 with empty cards / types — not a crash."""
    lab = tmp_path / "playbook_lab.json"
    lab.write_text(
        json.dumps(
            {
                "mode": "explore",
                "instructions": "",
                "cards": [],
                "types": {},
                "ready_to_promote": False,
                "promoted": False,
                "revision": 0,
                "written_at": "2026-08-20T16:04:01+00:00",
                "ledger": [],
                "paper_score": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(lab))
    pro._sync_notebook_page(force=True)
    # rev 0 is a real revision, not a missing one.
    assert "rev 0" in (pro.lbl_notebook_head.value or "")
    assert "0 setup card(s)" in (pro.lbl_notebook_meta.value or "")
    assert "UNSENDABLE" not in (pro.lbl_notebook_meta.value or "")
    assert "lots at write: none" in (pro.lbl_notebook_lots.value or "")
    names = " | ".join(_row_text(c) for c in pro.col_notebook_cards.controls)
    assert "No setup cards yet" in names
    assert pro.notebook_raw_panel.visible is True
    pro._sync_scorecard_page(force=True)
    cards = " | ".join(_row_text(c) for c in pro.col_sc_cards.controls)
    ledger = " | ".join(_row_text(c) for c in pro.col_sc_ledger.controls)
    assert "No card-attributed sends yet" in cards
    assert "No notebook revisions yet" in ledger


def test_notebook_page_shows_every_sendable_order_type(pro, monkeypatch, tmp_path):
    """Operator coverage: filled trunks and untouched ones, side by side."""
    from abcxauto.lab_playbook import playbook_type_keys

    lab = tmp_path / "playbook_lab.json"
    lab.write_text(
        json.dumps(
            {
                "revision": 3,
                "mode": "explore",
                "types": {
                    "market_bracket": {
                        "gotchas": "stop must be the wrong side of live last",
                        "cards": [{"name": "flush bounce", "status": "testing"}],
                    },
                    "vertical_spread": {"tool_order": ["quote", "option_chain", "send"]},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(lab))
    pro._sync_notebook_page(force=True)
    rows = " | ".join(_row_text(c) for c in pro.col_notebook_types.controls)
    # Every sendable trunk is listed, not only the two Grok has touched.
    for name in playbook_type_keys():
        assert name in rows, name
    assert "gotchas" in rows
    assert "tool_order" in rows
    assert "untouched" in rows
    assert "2/" in (pro.lbl_notebook_types.value or "")


def test_notebook_page_falls_back_to_prose(pro, monkeypatch, tmp_path):
    lab = tmp_path / "playbook_lab.json"
    lab.write_text(
        json.dumps({"revision": 1, "instructions": "watch the open, no cards yet"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(lab))
    pro._sync_notebook_page(force=True)
    assert pro.notebook_raw_panel.visible is True
    assert "watch the open" in (pro.lbl_notebook_body.value or "")
    assert "0 setup card(s)" in (pro.lbl_notebook_meta.value or "")


# -------------------------------------------------------------------- desk lock


def test_cleanup_releases_the_desk_lock(monkeypatch, tmp_path):
    import os
    import sys

    from abcxauto import supervisor
    from abcxauto.__main__ import main

    lock = tmp_path / "desk.lock"
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))
    monkeypatch.setattr(supervisor, "mark_operator_stop", lambda: None)
    monkeypatch.setattr("abcxauto.__main__._cleanup", lambda **_kw: 0)
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": "now"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["abcxauto", "--cleanup"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert not lock.is_file()


def test_supervised_child_does_not_claim_a_second_desk(monkeypatch, tmp_path):
    """The Pro child inherits ABCXAUTO_SUPERVISED — it must not re-supervise."""
    import sys

    from abcxauto import supervisor

    lock = tmp_path / "desk.lock"
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(lock))
    monkeypatch.setenv("ABCXAUTO_SUPERVISED", "1")
    monkeypatch.delenv("ABCXAUTO_LAUNCH_PROBE", raising=False)
    monkeypatch.setattr(sys, "argv", ["abcxauto"])
    calls: list[str] = []
    monkeypatch.setattr(supervisor, "supervise", lambda *_a, **_k: calls.append("supervise"))
    monkeypatch.setattr("abcxauto.think_stream.begin_run", lambda: None)
    monkeypatch.setattr("abcxauto.pro_desktop.run_app", lambda: calls.append("run_app"))
    monkeypatch.setattr("abcxauto.cursor_env.should_autostart", lambda: False)

    from abcxauto.__main__ import main

    main()
    assert calls == ["run_app"]
    assert not lock.is_file()
