"""Live Grok thinking stream."""

import json
from types import SimpleNamespace

import pytest

from abcxauto.think_stream import (
    ascii_text,
    bind_engine,
    emit,
    subscribe,
    unsubscribe,
)


def test_ascii_text_is_cp1252_safe():
    out = ascii_text("idle -> hold  thesis: AAPL — wait")
    assert all(ord(c) < 128 for c in out)
    assert "—" not in out
    assert ascii_text("I'll inspect") == "I'll inspect"
    assert "?" not in ascii_text("I'll inspect")
    assert ascii_text("wait \u2014 crash") == "wait - crash"
    assert ascii_text("no \u22656% flush") == "no >=6% flush"
    assert "?" not in ascii_text("CVX \u22121.4%")
    assert ascii_text("CVX \u22121.4%") == "CVX -1.4%"


def test_emit_reaches_subscriber():
    got: list[tuple[str, str]] = []

    def cap(kind: str, text: str) -> None:
        got.append((kind, text))

    subscribe(cap)
    try:
        emit("stage", "judge")
        emit("say", "hello")
    finally:
        unsubscribe(cap)
    assert ("stage", "judge") in got
    assert ("say", "hello") in got


def test_bind_engine_appends_think_live():
    from abcxauto.think_stream import reset_speaker

    reset_speaker()
    st = SimpleNamespace(think_live="")
    bind_engine(SimpleNamespace(state=st))
    try:
        emit("stage", "grok")
        emit("say", '{"stance":"idle"}')
    finally:
        bind_engine(None)
    assert "--- GROK ---" in st.think_live
    assert '{"stance":"idle"}' in st.think_live


def test_clerk_facts_use_clerk_banner_not_grok():
    """hits= / book snap / Wake Grok are clerk. [think]/[say] stay Grok."""
    from abcxauto.think_stream import reset_speaker

    reset_speaker()
    st = SimpleNamespace(think_live="")
    bind_engine(SimpleNamespace(state=st))
    try:
        emit("clerk", "Book snap done — Grok has the tools.\n")
        emit("clerk", "Wake Grok.\n")
        emit("clerk", "[scan]\n")
        emit("clerk", "hits=3 screens=2 deepest=-6.5% SNDK src=ibkr\n")
        emit("stage", "grok")
        emit("say", "\n[think]\n")
        emit("think", "weigh tape ")
        emit("say", "\n[say]\n")
        emit("say", "watching the gap\n")
    finally:
        bind_engine(None)
        reset_speaker()
    live = st.think_live
    assert "--- CLERK ---" in live
    assert "[clerk]" in live
    assert "Book snap done" in live
    assert "Wake Grok." in live
    assert "hits=3 screens=2" in live
    assert "card_gap=" not in live
    assert "--- GROK ---" in live
    assert "[think]" in live
    assert "[say]" in live
    assert "watching the gap" in live
    clerk_at = live.find("--- CLERK ---")
    grok_at = live.find("--- GROK ---")
    assert clerk_at >= 0 and grok_at > clerk_at
    clerk_block = live[clerk_at:grok_at]
    grok_block = live[grok_at:]
    assert "hits=3" in clerk_block
    assert "Wake Grok." in clerk_block
    assert "[think]" not in clerk_block
    assert "[say]" not in clerk_block
    assert "watching the gap" in grok_block
    assert "hits=3" not in grok_block


def test_think_tail_and_last_turn_files(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    monkeypatch.setattr(ts, "_TAIL_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(ts, "_last_tail_write", 0.0)
    st = SimpleNamespace(think_live="")
    ts.bind_engine(SimpleNamespace(state=st))
    try:
        ts.emit("say", "Wake Grok.\n")
    finally:
        ts.bind_engine(None)
    assert "Wake Grok" in (tmp_path / "think_tail.txt").read_text(encoding="utf-8")
    ts.write_last_turn({
        "cycle": 3,
        "strat": "skipped",
        "rationale": "skipped_grok: book_unreliable",
        "validation": "skipped_grok: book_unreliable",
        "tool_trace": ["book", "quote"],
        "scan_fetched": ["NVDA"],
        "book_unreliable": True,
        "equity": 0,
        "reality_pulse": {
            "session": {"status": "premarket"},
            "data_freshness": {"ibkr_connected": False},
        },
        "positions": [
            {
                "symbol": "IWM",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260821",
                "right": "C",
                "strike": 306,
            }
        ],
        "world_state": {"flat": True, "net_liquidation": 0, "gates": {"book_unreliable": True}},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    from tests.conftest import assert_no_cycle_counter, assert_no_cycle_keys

    assert_no_cycle_keys(last)
    assert_no_cycle_counter(json.dumps(last))
    assert last["tool_trace"] == ["book", "quote"]
    assert last["session"]["status"] == "premarket"
    assert last["ibkr_connected"] is False
    assert last["book_unreliable"] is True
    assert last["skip_reason"] == "book_unreliable"
    assert last["flat"] is True
    assert last["open_lots"] == ["IWM 260821C306 long 1"]
    ts.write_last_turn({
        "cycle": 4,
        "strat": "in_progress",
        "rationale": "grok_turn",
        "reality_pulse": {"ibkr_connected": True},
        "positions": [
            {
                "symbol": "QQQ",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260821",
                "right": "C",
                "strike": 735,
            }
        ],
        "world_state": {"flat": False, "net_liquidation": 36000},
    })
    live = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert_no_cycle_keys(live)
    assert live["strat"] == "in_progress"
    assert live["stale"] is False
    assert live["ibkr_connected"] is True
    assert live["open_lots"] == ["QQQ 260821C735 long 1"]
    assert live["mix"].get("long_c") == 1
    assert live.get("previous_strat") == "skipped"
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    from tests.conftest import assert_no_cycle_keys

    assert_no_cycle_keys(brief)
    assert brief["strat"] == "skipped"
    assert brief["open_lots"] == ["IWM 260821C306 long 1"]


def test_write_last_turn_skips_junk_failed_look(tmp_path, monkeypatch):
    """A failed empty/? look must not blank last_turn / desk_brief."""
    from abcxauto import think_stream as ts
    from abcxauto.think_stream import last_turn_look_failed

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}

    good = {
        "strat": "",
        "rationale": "Flat. No ticket.",
        "tool_trace": ["book", "status", "playbook"],
        "world_state": {"flat": True, "net_liquidation": 35000},
        "_failed": False,
    }
    ts.write_last_turn(good)
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert last["rationale"] == "Flat. No ticket."
    assert last["tool_trace"] == ["book", "status", "playbook"]
    assert brief["rationale"] == "Flat. No ticket."
    assert last_turn_look_failed(good) is False

    empty_fail = {
        "strat": "",
        "rationale": "",
        "tool_trace": [],
        "_failed": True,
        "world_state": {"flat": True, "net_liquidation": 35000},
    }
    assert last_turn_look_failed(empty_fail) is True
    ts.write_last_turn(empty_fail)
    last2 = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief2 = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert last2["rationale"] == "Flat. No ticket."
    assert last2["tool_trace"] == ["book", "status", "playbook"]
    assert brief2["rationale"] == "Flat. No ticket."
    assert last2["ts"] == last["ts"]

    q_fail = {
        "strat": "",
        "rationale": "?",
        "tool_trace": [],
        "_failed": True,
    }
    assert last_turn_look_failed(q_fail) is True
    ts.write_last_turn(q_fail)
    last3 = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last3["rationale"] == "Flat. No ticket."
    assert last3["tool_trace"] == ["book", "status", "playbook"]

    trailing = {
        "strat": "",
        "rationale": "Standing down. Watching IWM.\n?",
        "tool_trace": ["book", "status"],
        "_failed": False,
        "world_state": {"flat": True, "net_liquidation": 35000},
    }
    assert last_turn_look_failed(trailing) is False
    ts.write_last_turn(trailing)
    last4 = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert "Standing down" in last4["rationale"]

    ts.write_last_turn({
        "strat": "",
        "rationale": "Watching IWM. No ticket.",
        "tool_trace": ["book", "scan"],
        "_failed": False,
        "world_state": {"flat": True, "net_liquidation": 35100},
    })
    last5 = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief5 = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert last5["rationale"] == "Watching IWM. No ticket."
    assert last5["tool_trace"] == ["book", "scan"]
    assert brief5["rationale"] == "Watching IWM. No ticket."


def test_write_last_turn_park_and_overnight_still_write(tmp_path, monkeypatch):
    """Overnight skip and park are completed looks — they still persist."""
    from abcxauto import think_stream as ts
    from abcxauto.think_stream import last_turn_look_failed

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "",
        "rationale": "Flat. No ticket.",
        "tool_trace": ["book"],
        "world_state": {"flat": True, "net_liquidation": 1},
    })

    park = {
        "strat": "",
        "rationale": "Gate off. Parking.",
        "tool_trace": ["playbook", "book", "scan", "set_wake"],
        "_parked": True,
        "_failed": False,
        "world_state": {"flat": True, "net_liquidation": 1},
    }
    assert last_turn_look_failed(park) is False
    ts.write_last_turn(park)
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert "Parking" in last["rationale"]
    assert last["tool_trace"] == ["playbook", "book", "scan", "set_wake"]
    assert "Parking" in brief["rationale"]

    overnight = {
        "strat": "skipped",
        "rationale": "skipped_grok: session_closed",
        "validation": "skipped_grok: session_closed",
        "_failed": False,
        "world_state": {"flat": True, "net_liquidation": 1},
    }
    assert last_turn_look_failed(overnight) is False
    ts.write_last_turn(overnight)
    last2 = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last2["skip_reason"] == "session_closed"
    assert last2["strat"] == "skipped"

    in_progress = {
        "strat": "in_progress",
        "rationale": "grok_turn",
        "_failed": True,
        "world_state": {"flat": True, "net_liquidation": 1},
    }
    assert last_turn_look_failed(in_progress) is False
    ts.write_last_turn(in_progress)
    last3 = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last3["strat"] == "in_progress"
    brief3 = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert brief3["strat"] == "skipped"


def test_last_turn_reads_scan_fetched_from_world(tmp_path, monkeypatch):
    """Pro _host_think used to omit the top-level key; last_turn then said []."""
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "hold",
        "rationale": "flat",
        "tool_trace": ["scan", "set_wake"],
        "reality_pulse": {"ibkr_connected": True},
        "world_state": {
            "flat": True,
            "net_liquidation": 35000,
            "scan_fetched": ["NVDA", "MU"],
            "candle_source": "ibkr",
        },
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["scan_fetched"] == ["NVDA", "MU"]
    assert last["candle_source"] == "ibkr"


def test_last_turn_keeps_open_gap_rows_and_the_gate_table(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    table = (
        "Flat. No send. Gate OFF at 11:56 ET.\n"
        "| Check | Result |\n"
        "| mega TOP_PERC_LOSE | SNDK -7% / open_gap -6.5% memory-rally |\n"
        "| MU | gap -3.3% |"
    )
    ts.write_last_turn({
        "strat": "hold",
        "rationale": table,
        "tool_trace": ["scan"],
        "scan_hits": {
            "source": "ibkr",
            "arena": "mega_cap",
            "scan_code": "TOP_PERC_LOSE",
            "quoted": 12,
            "rows": [
                {"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5, "change_pct": -7.0},
                {"symbol": "MU", "last": 911.0, "open_gap_pct": -3.3},
            ],
        },
        "world_state": {"flat": True, "net_liquidation": 35000},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert "open_gap -6.5%" in last["rationale"]
    assert last["scan_hits"]["rows"][0]["symbol"] == "SNDK"
    assert last["scan_hits"]["rows"][0]["open_gap_pct"] == -6.5
    snap: dict = {}
    ts.seed_snap_from_last_turn(snap)
    assert snap["scan_hits"]["rows"][0]["open_gap_pct"] == -6.5
    assert snap["ibkr_live_quotes"]["SNDK"] == 1485.0
    snap_none = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap_none)
    assert snap_none["scan_hits"]["rows"][0]["open_gap_pct"] == -6.5
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert "open_gap -6.5%" in brief["rationale"]


def test_merge_scan_hits_keeps_the_gap_row_after_a_junk_screen():
    from abcxauto.think_stream import last_look_wake_bit, merge_scan_hits

    mega = {
        "source": "ibkr",
        "arena": "mega_cap",
        "scan_code": "TOP_PERC_LOSE",
        "quoted": 12,
        "rows": [
            {"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5},
            {"symbol": "MU", "last": 911.0, "open_gap_pct": -3.3},
        ],
    }
    junk = {
        "source": "ibkr",
        "arena": "all",
        "scan_code": "MOST_ACTIVE",
        "quoted": 12,
        "rows": [
            {"symbol": "DFNS", "last": 8.0},
            {"symbol": "QBTX", "last": 8.07},
        ],
    }
    merged = merge_scan_hits(mega, junk)
    assert merged["rows"][0]["symbol"] == "SNDK"
    assert merged["rows"][0]["open_gap_pct"] == -6.5
    assert merged["arena"] == "mega_cap"
    assert {r["symbol"] for r in merged["rows"]} >= {"SNDK", "MU", "DFNS", "QBTX"}
    bit = last_look_wake_bit({
        "send_calls": 0,
        "tool_trace": ["scan"],
        "scan_hits": merged,
    })
    assert bit == ""
    assert "last_scan" not in bit
    assert last_look_wake_bit({
        "last_say": "still no ticket unless news-miss actually fires",
        "rationale": "still no ticket unless news-miss actually fires",
        "send_calls": 0,
        "tool_trace": ["scan"],
        "scan_hits": merged,
    }) == ""
    assert last_look_wake_bit({
        "rationale": "Next look: same mega/large loser screens + news first.",
        "send_calls": 0,
    }) == ""


def test_merge_scan_hits_puts_the_down_gap_ahead_of_a_green_active_page():
    from abcxauto.think_stream import merge_scan_hits

    merged = merge_scan_hits(
        {
            "arena": "mega_cap",
            "scan_code": "MOST_ACTIVE",
            "rows": [{"symbol": "AMD", "last": 472.0, "open_gap_pct": 4.2}],
        },
        {
            "arena": "large_cap",
            "scan_code": "TOP_PERC_LOSE",
            "rows": [{"symbol": "ALB", "last": 134.0, "open_gap_pct": -3.8}],
        },
    )
    assert merged["rows"][0]["symbol"] == "ALB"
    assert merged["scan_code"] == "TOP_PERC_LOSE"


def test_seed_snap_from_last_turn_skips_none(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    (tmp_path / "last_turn.json").write_text(
        json.dumps({"strat": "hold", "candle_source": "none", "stale": False}),
        encoding="utf-8",
    )
    snap: dict = {}
    ts.seed_snap_from_last_turn(snap)
    assert "candle_source" not in snap

    (tmp_path / "last_turn.json").write_text(
        json.dumps({"strat": "hold", "candle_source": "ibkr", "stale": False}),
        encoding="utf-8",
    )
    ts.seed_snap_from_last_turn(snap)
    assert snap["candle_source"] == "ibkr"

    snap = {}
    (tmp_path / "last_turn.json").write_text(
        json.dumps({"strat": "hold", "candle_source": "ibkr", "stale": True}),
        encoding="utf-8",
    )
    ts.seed_snap_from_last_turn(snap)
    assert snap["candle_source"] == "ibkr"


def test_stale_last_turn_does_not_seed_yesterday_scan_hits(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "strat": "hold",
            "stale": True,
            "scan_hits": {
                "quoted": 12,
                "rows": [{"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5}],
            },
        }),
        encoding="utf-8",
    )
    snap: dict = {}
    ts.seed_snap_from_last_turn(snap)
    assert "scan_hits" not in snap
    assert not snap.get("ibkr_live_quotes")


def test_seed_snap_carries_fresh_ibkr_quotes(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "strat": "hold",
            "stale": False,
            "ts": datetime.now(timezone.utc).isoformat(),
            "ibkr_live_quotes": {"SNDK": 91.5},
            "scan_hits": {
                "quoted": 1,
                "rows": [{"symbol": "MU", "last": 910.0}],
            },
        }),
        encoding="utf-8",
    )
    snap: dict = {"ibkr_live_quotes": {"SPY": 500.0}}
    ts.seed_snap_from_last_turn(snap)
    assert snap["ibkr_live_quotes"]["SPY"] == 500.0
    assert snap["ibkr_live_quotes"]["SNDK"] == 91.5
    assert snap["ibkr_live_quotes"]["MU"] == 910.0
    assert snap["scan_hits"]["rows"][0]["symbol"] == "MU"


def test_overnight_last_turn_does_not_seed_scan_hits(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "strat": "hold",
            "stale": False,
            "ts": (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat(),
            "ibkr_live_quotes": {"SNDK": 1485.0},
            "scan_hits": {
                "quoted": 12,
                "rows": [{"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5}],
            },
        }),
        encoding="utf-8",
    )
    snap: dict = {}
    ts.seed_snap_from_last_turn(snap)
    assert "scan_hits" not in snap
    assert not snap.get("ibkr_live_quotes")


def test_seed_snap_carries_today_session_range_not_yesterday(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    now = datetime.now(timezone.utc)
    ts.write_last_turn({
        "strat": "hold",
        "tool_trace": ["book", "scan", "candles"],
        "session_range": {
            "SNDK": {
                "date": today,
                "open": 90.0,
                "low": 88.0,
                "last": 91.5,
                "retrace_30": 93.0,
                "ticket": {"card": "flush bounce", "stop_price": 88.0},
            }
        },
        "world_state": {"flat": True, "net_liquidation": 35000},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    last["ts"] = (now - timedelta(minutes=8)).isoformat()
    (tmp_path / "last_turn.json").write_text(json.dumps(last), encoding="utf-8")
    snap: dict = {}
    ts.seed_snap_from_last_turn(snap)
    assert snap["session_range"]["SNDK"]["low"] == 88.0
    assert snap["session_range"]["SNDK"]["ticket"]["card"] == "flush bounce"

    # snap() stamps candle_source="none" before seed — that must not drop the tape.
    snap_none = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap_none)
    assert snap_none["session_range"]["SNDK"]["low"] == 88.0
    assert snap_none["candle_source"] == "none"

    last["session_range"] = {
        "SNDK": {"date": "2020-01-01", "open": 90.0, "low": 88.0, "today": True}
    }
    last["stale"] = False
    (tmp_path / "last_turn.json").write_text(json.dumps(last), encoding="utf-8")
    snap = {}
    ts.seed_snap_from_last_turn(snap)
    assert "session_range" not in snap

    last["session_range"] = {
        "SNDK": {"date": today, "open": 90.0, "low": 88.0}
    }
    last["stale"] = False
    last["ts"] = (now - timedelta(hours=18)).isoformat()
    (tmp_path / "last_turn.json").write_text(json.dumps(last), encoding="utf-8")
    snap = {}
    ts.seed_snap_from_last_turn(snap)
    assert "session_range" not in snap


def test_seed_snap_from_in_progress_keeps_today_session(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "strat": "in_progress",
            "stale": False,
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_range": {
                "SNDK": {
                    "date": today,
                    "open": 90.0,
                    "low": 88.0,
                    "last": 91.5,
                    "ticket": {"card": "flush bounce", "stop_price": 88.0},
                }
            },
        }),
        encoding="utf-8",
    )
    snap = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap)
    assert snap["session_range"]["SNDK"]["low"] == 88.0


def test_seed_snap_keeps_hits_but_does_not_reuse_last_look_screens(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "stale": False,
            "ts": datetime.now(timezone.utc).isoformat(),
            "scan_screens": [
                "mega_cap:LOW_OPEN_GAP",
                "mega_cap:TOP_PERC_LOSE",
            ],
            "scan_calls": 2,
            "scan_hits": {
                "rows": [{"symbol": "ALB", "open_gap_pct": -3.8}],
            },
        }),
        encoding="utf-8",
    )
    snap: dict = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap)
    assert "scan_screens" not in snap
    assert "scan_calls" not in snap
    assert snap["scan_hits"]["rows"][0]["symbol"] == "ALB"


def test_seed_snap_does_not_slide_manage_reuse_off_look_ts(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    now = datetime.now(timezone.utc)
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "stale": False,
            "flat": False,
            "ts": now.isoformat(),
            "scan_at": (now - timedelta(seconds=20 * 60)).isoformat(),
            "scan_screens": ["mega_cap:LOW_OPEN_GAP"],
            "scan_calls": 1,
        }),
        encoding="utf-8",
    )
    snap: dict = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap)
    assert "scan_screens" not in snap
    assert snap["scan_at"]


def test_seed_snap_does_not_reuse_when_row_asof_is_stale(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    now = datetime.now(timezone.utc)
    (tmp_path / "last_turn.json").write_text(
        json.dumps({
            "stale": False,
            "flat": False,
            "ts": now.isoformat(),
            "scan_screens": ["mega_cap:LOW_OPEN_GAP"],
            "scan_calls": 1,
            "scan_hits": {
                "rows": [{
                    "symbol": "NKE",
                    "last": 39.615,
                    "ibkr": {"asof_iso": (now - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                }]
            },
        }),
        encoding="utf-8",
    )
    snap: dict = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap)
    assert "scan_screens" not in snap


def test_seed_snap_does_not_reuse_manage_screens_as_this_look(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    age = datetime.now(timezone.utc) - timedelta(seconds=240)
    row = {
        "stale": False,
        "ts": age.isoformat(),
        "scan_screens": ["mega_cap:LOW_OPEN_GAP"],
        "scan_calls": 1,
        "scan_hits": {"rows": [{"symbol": "NKE", "open_gap_pct": -2.1}]},
    }
    (tmp_path / "last_turn.json").write_text(json.dumps(row), encoding="utf-8")
    hunt: dict = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(hunt)
    assert "scan_screens" not in hunt
    assert "scan_calls" not in hunt
    row["flat"] = False
    (tmp_path / "last_turn.json").write_text(json.dumps(row), encoding="utf-8")
    manage: dict = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(manage)
    assert "scan_screens" not in manage
    assert "scan_calls" not in manage
    assert manage["scan_hits"]["rows"][0]["symbol"] == "NKE"


def test_last_look_for_hunt_drops_an_overnight_brief():
    from datetime import datetime, timedelta, timezone

    from abcxauto.think_stream import last_look_facts, last_look_for_hunt

    now = datetime.now(timezone.utc)
    old = {
        "ts": (now - timedelta(hours=18)).isoformat(),
        "tool_trace": ["book", "scan", "news"],
        "send_calls": 0,
        "scan_hits": {
            "quoted": 12,
            "rows": [{"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5}],
        },
    }
    facts = last_look_facts(old)
    assert facts["fresh"] is False
    assert facts.get("tools") == []
    assert not facts.get("scan_hits")
    assert not facts.get("rationale")
    assert last_look_for_hunt(old) == {}
    from abcxauto.think_stream import last_look_wake_bit

    bit = last_look_wake_bit(old)
    assert bit == ""
    assert "last_scan" not in bit
    assert "book,scan,news" not in bit

    recent = dict(old)
    recent["ts"] = (now - timedelta(minutes=10)).isoformat()
    assert last_look_facts(recent)["fresh"] is True
    assert last_look_for_hunt(recent)["tools"][:3] == ["book", "scan", "news"]


def test_last_look_facts_omit_leftover_say():
    from datetime import datetime, timezone

    from abcxauto.think_stream import last_look_facts

    now = datetime.now(timezone.utc)
    facts = last_look_facts(
        {
            "ts": now.isoformat(),
            "rationale": "loser-scan — only send if a card has room",
            "tool_trace": ["scan"],
            "send_calls": 0,
            "scan_hits": {"rows": [{"symbol": "HPQ", "open_gap_pct": -4.2}]},
        }
    )
    assert facts.get("fresh") is True
    assert "rationale" not in facts
    assert "only send if a card has room" not in str(facts)
    assert "loser-scan" not in str(facts)
    assert last_look_facts(
        {
            "ts": now.isoformat(),
            "rationale": "only send if a card has room",
        }
    ) == {}


def test_last_turn_operator_paint_omits_cycle(tmp_path, monkeypatch):
    from tests.conftest import assert_no_cycle_keys
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "cycle": 12,
        "strat": "hold",
        "rationale": "flat",
        "tool_trace": ["book"],
        "world_state": {"flat": True, "net_liquidation": 1},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert_no_cycle_keys(last)
    assert_no_cycle_keys(brief)
    assert last["strat"] == "hold"


def test_run_identity_stale_last_turn(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "RUN_PATH", tmp_path / "run.json")
    ts._run = {}
    first = ts.begin_run()
    ts.write_last_turn({
        "cycle": 7,
        "strat": "hold",
        "tool_trace": ["book"],
        "scan_fetched": ["NVDA"],
        "scan_hits": {
            "arena": "mega_cap",
            "scan_code": "TOP_PERC_LOSE",
            "rows": [{"symbol": "NVDA", "open_gap_pct": -1.2}],
        },
        "world_state": {"candle_source": "ibkr", "flat": True, "net_liquidation": 1},
    })
    live = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    from tests.conftest import assert_no_cycle_keys

    assert_no_cycle_keys(live)
    assert live["run_id"] == first["run_id"]
    assert live["stale"] is False
    assert ts.last_turn_is_live(live) is True
    second = ts.begin_run()
    assert second["run_id"] != first["run_id"]
    kept = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert_no_cycle_keys(kept)
    assert kept["stale"] is False
    assert kept["scan_fetched"] == ["NVDA"]
    assert kept["scan_hits"]["rows"][0]["open_gap_pct"] == -1.2
    assert kept["candle_source"] == "ibkr"
    assert ts.last_turn_is_live(kept) is False
    assert ts.last_turn_is_live(live) is False


def test_begin_run_stales_an_overnight_last_turn(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "RUN_PATH", tmp_path / "run.json")
    ts._run = {}
    ts.begin_run()
    ts.write_last_turn({
        "strat": "hold",
        "tool_trace": ["book"],
        "world_state": {"flat": True, "net_liquidation": 1},
    })
    live = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    live["ts"] = (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat()
    (tmp_path / "last_turn.json").write_text(json.dumps(live), encoding="utf-8")
    ts.begin_run()
    stale = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert stale["stale"] is True


def test_stop_keeps_think_tail_new_run_archives(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "THINK_PREV_PATH", tmp_path / "think_prev.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "RUN_PATH", tmp_path / "run.json")
    monkeypatch.setattr(ts, "_TAIL_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(ts, "_last_tail_write", 0.0)
    ts._run = {}
    st = SimpleNamespace(think_live="mid-turn think about IWM\n")
    ts.bind_engine(SimpleNamespace(state=st))
    try:
        ts.emit("say", "still working\n")
        ts.mark_review_stale()
        assert "still working" in (tmp_path / "think_tail.txt").read_text(encoding="utf-8")
        ts.begin_run()
        assert "still working" in (tmp_path / "think_prev.txt").read_text(encoding="utf-8")
        assert (tmp_path / "think_tail.txt").read_text(encoding="utf-8") == ""
    finally:
        ts.bind_engine(None)


def test_bind_engine_keeps_prior_think_on_new_wake():
    from abcxauto.think_stream import reset_speaker

    reset_speaker()
    st = SimpleNamespace(think_live="snapping book, then Grok...\n")
    bind_engine(SimpleNamespace(state=st))
    try:
        emit("stage", "grok")
        emit("say", "weigh tape")
    finally:
        bind_engine(None)
    assert "snapping book" in st.think_live
    assert "--- GROK ---" in st.think_live
    assert "weigh tape" in st.think_live


@pytest.mark.asyncio
async def test_grok_streams_think_and_say(monkeypatch):
    from abcxauto.agent_loop import grok

    class Ch:
        def __init__(self, content="", reasoning_content=""):
            self.content = content
            self.reasoning_content = reasoning_content

    class Chat:
        async def stream(self):
            yield None, Ch(reasoning_content="weigh tape ")
            yield None, Ch(content='{"stance":"idle"}')

    created: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            created.update(k)
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )
    got: list[tuple[str, str]] = []

    def cap(kind: str, text: str) -> None:
        got.append((kind, text))

    subscribe(cap)
    try:
        out = await grok(g, "prompt", stage="judge")
    finally:
        unsubscribe(cap)
    assert out == '{"stance":"idle"}'
    assert created.get("include") == ["verbose_streaming"]
    assert any(k == "think" and t == "weigh tape " for k, t in got)
    assert any(k == "say" and '{"stance":"idle"}' in t for k, t in got)
    assert any(k == "stage" and t == "judge" for k, t in got)


@pytest.mark.asyncio
async def test_grok_streams_cumulative_reasoning_as_delta(monkeypatch):
    from abcxauto.agent_loop import grok

    class Ch:
        def __init__(self, content="", reasoning_content=""):
            self.content = content
            self.reasoning_content = reasoning_content

    class Chat:
        async def stream(self):
            yield None, Ch(reasoning_content="weigh")
            yield None, Ch(reasoning_content="weigh tape")
            yield None, Ch(content='{"stance":"idle"}')

    class _ChatNS:
        @staticmethod
        def create(**_k):
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )
    thinks: list[str] = []

    def cap(kind: str, text: str) -> None:
        if kind == "think":
            thinks.append(text)

    subscribe(cap)
    try:
        await grok(g, "prompt", stage="judge")
    finally:
        unsubscribe(cap)
    assert thinks == ["weigh", " tape"]


@pytest.mark.asyncio
async def test_grok_uses_client_max_tokens_not_2048_cap():
    from abcxauto.agent_loop import grok

    class Ch:
        def __init__(self, content="", reasoning_content=""):
            self.content = content
            self.reasoning_content = reasoning_content

    class BoomResp:
        @property
        def reasoning_content(self):
            raise AssertionError("stream must not read Response.reasoning_content")

        @property
        def content(self):
            raise AssertionError("stream must not read Response.content")

    class Chat:
        async def stream(self):
            yield BoomResp(), Ch(reasoning_content="weigh tape ")
            yield BoomResp(), Ch(content='{"stance":"idle"}')

    created: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            created.update(k)
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
    )
    out = await grok(g, "prompt", stage="judge")
    assert out == '{"stance":"idle"}'
    assert created.get("max_tokens") == 8192
