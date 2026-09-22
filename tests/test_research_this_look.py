"""This-look research is gathered color, not a stale expectancy dump."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from abcxauto.desk_mode import (
    THIS_LOOK_NEED,
    load_research_brief,
    note_research_tool,
    research_brief_look_payload,
    write_research_brief,
)

_FIXTURE_BRIEF = (
    Path(__file__).resolve().parent / "fixtures" / "research_brief_2026_09_09.json"
)


def test_empty_snap_need_capability_line():
    payload = research_brief_look_payload({}, snap={})
    assert payload["use"] == "color, never a live trigger"
    assert payload["send_geometry"] is False
    assert payload["missing"] is True
    assert payload["stale"] is True
    assert payload["prior_session"] == {}
    assert payload["this_look"]["tools"] == []
    assert payload["this_look"]["facts"] == []
    assert payload["need"] == THIS_LOOK_NEED
    assert "scan|news|candles|web|odds|recall" in payload["need"]


def test_this_look_tools_after_scan_and_news():
    snap: dict = {}
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "AMD", "open_gap_pct": 4.2}], "source": "ibkr"},
    )
    note_research_tool(
        snap,
        "news",
        {"items": [{"symbol": "NVDA", "headline": "NVDA prints after hours"}]},
    )
    payload = research_brief_look_payload({}, snap=snap)
    tools = payload["this_look"]["tools"]
    assert "scan" in tools
    assert "news" in tools
    assert "need" not in payload
    opens = payload["this_look"]["open"]
    assert "scan: arena|scan_code|symbols[]" not in opens
    assert "news: symbols[]" not in opens
    assert "candles: symbol+resolution" in opens
    assert "web: url" in opens
    assert "odds: query|symbols[]" in opens
    assert "recall: notes|cards" in opens


def test_fresh_prior_session_omits_hold_conclusion():
    now = datetime.now(timezone.utc)
    brief = {
        "as_of": now.isoformat(),
        "session": "premarket",
        "mode": "research",
        "symbols": ["SPY"],
        "facts": [{"source": "marks", "text": "leftover $22433.74 69.37%"}],
        "uncertainties": [],
        "conclusion": "Idle 85% cash is not a ticket",
    }
    payload = research_brief_look_payload(brief, snap={}, now=now)
    assert payload["stale"] is False
    prior = payload["prior_session"]
    assert "conclusion" not in prior
    assert prior.get("as_of") == brief["as_of"]
    assert prior.get("session") == "premarket"
    assert prior.get("symbols") == ["SPY"]
    assert prior.get("facts") == brief["facts"]


def test_stale_prior_file_does_not_leak_expectancy(tmp_path, monkeypatch):
    dest = tmp_path / "research_brief.json"
    dest.write_bytes(_FIXTURE_BRIEF.read_bytes())
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(dest))
    brief = load_research_brief()
    assert brief.get("expectancy")
    payload = research_brief_look_payload(brief, snap={}, now=datetime.now(timezone.utc))
    assert payload["stale"] is True
    prior = payload["prior_session"]
    assert "expectancy" not in prior
    assert "tickets" not in prior
    assert "prove_window_id" not in prior
    assert "gate_verdict" not in prior
    assert set(prior) <= {"as_of", "session"}
    assert payload["need"] == THIS_LOOK_NEED


def test_write_omits_expectancy_and_tickets(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    out = write_research_brief(session="premarket", snap={}, now=datetime.now(timezone.utc))
    assert "expectancy" not in out
    assert "tickets" not in out
    assert out["facts"] == []
    assert any("nothing was gathered" in str(u) for u in out["uncertainties"])
    disk = load_research_brief()
    assert "expectancy" not in disk
    assert "tickets" not in disk


def test_scan_fact_line_ignores_aliased_gap_pct():
    snap: dict = {}
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "FOO", "gap_pct": 12.0}], "source": "scan"},
    )
    text = snap["_research_bag"]["facts"][0]["text"]
    assert "deepest=" not in text
    assert "12" not in text
    note_research_tool(
        snap,
        "scan",
        {"rows": [{"symbol": "AMD", "open_gap_pct": 4.2}], "source": "scan"},
    )
    open_line = snap["_research_bag"]["facts"][1]["text"]
    assert "deepest=AMD 4.2%" in open_line


def test_scan_fact_line_names_quoted_hits():
    snap: dict = {}
    note_research_tool(
        snap,
        "scan",
        {
            "source": "ibkr",
            "rows": [
                {"symbol": "NFLX", "last": 982.1, "skip_class": ""},
                {"symbol": "INTC", "last": 109.89, "skip_class": ""},
                {"symbol": "MEDS", "last": 3.2, "skip_class": "micro"},
            ],
        },
    )
    text = snap["_research_bag"]["facts"][0]["text"]
    assert "NFLX 982.1" in text
    assert "INTC 109.89" in text
    assert "skip=1" in text
    assert "MEDS" not in text


def test_candle_series_keeps_last_not_empty_n():
    snap: dict = {}
    note_research_tool(
        snap,
        "candles",
        {
            "source": "ibkr",
            "use": "ibkr_rth_structure",
            "bars": [],
            "series": [
                {
                    "symbol": "AMZN",
                    "bars": [{"c": 251.19}],
                    "session": {"last": 251.19, "vs_open": -0.4},
                },
                {
                    "symbol": "NVDA",
                    "bars": [{"c": 219.51}],
                    "session": {"last": 219.51, "vs_open": 1.1},
                },
            ],
        },
    )
    text = snap["_research_bag"]["facts"][0]["text"]
    assert "bars n=0" not in text
    assert "AMZN 251.19" in text
    assert "NVDA 219.51" in text
    assert "vs_open=-0.4" in text


def test_empty_candle_payload_writes_no_fact():
    snap: dict = {}
    note_research_tool(
        snap,
        "candles",
        {"source": "ibkr", "use": "ibkr_rth_structure", "bars": [], "series": []},
    )
    assert snap["_research_bag"]["facts"] == []


def test_news_fact_one_headline_per_symbol():
    snap: dict = {}
    note_research_tool(
        snap,
        "news",
        {
            "items": [
                {"symbol": "NVDA", "headline": "Huang doubles chip volume"},
                {"symbol": "NVDA", "headline": "A $100 Monthly Investment in QQQ"},
                {"symbol": "AMZN", "headline": "Amazon AI warrant color"},
            ]
        },
    )
    text = snap["_research_bag"]["facts"][0]["text"]
    assert "Huang doubles chip volume" in text
    assert "QQQ" not in text
    assert "AMZN" in text


def test_brief_stamps_marks_and_spoken_conclusion(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    now = datetime.now(timezone.utc)
    snap = {
        "net_liquidation": 32340.39,
        "capital_liquidity": {"total_cash": 22433.74},
        "ibkr_live_quotes": {"AMZN": 252.24, "NVDA": 221.07, "QCOM": 190.26, "SPY": 762.28},
        "positions": [
            {"symbol": "AMZN", "qty": 10, "avg": 251.76, "mkt": 252.19},
            {"symbol": "NVDA", "qty": 11, "avg": 219.22, "mkt": 220.95},
            {"symbol": "QCOM", "qty": 26, "avg": 190.10, "mkt": 190.11},
        ],
        "working_orders": [
            {"symbol": "AMZN", "type": "STP", "stop": 248.9, "role": "exit"},
            {"symbol": "AMZN", "type": "LMT", "lmt": 256.0, "role": "exit"},
            {"symbol": "NVDA", "type": "STP", "stop": 216.9, "role": "exit"},
            {"symbol": "QCOM", "type": "STP", "stop": 187.5, "role": "exit"},
        ],
    }
    class _Turn:
        text = "Lots protected. AMZN 252.24 vs stop 248.9. No send."
        tool_trace = ["book", "quote", "news"]

    out = write_research_brief(
        session="premarket", snap=snap, turn=_Turn(), now=now
    )
    assert out["conclusion"] == _Turn.text
    texts = [row["text"] for row in out["facts"] if row.get("source") == "marks"]
    blob = " ".join(texts)
    assert "69.37" in blob
    assert "22433.74" in blob or "cash 69.37" in blob
    assert "QCOM" in blob
    assert blob.index("QCOM26") < blob.index("AMZN10")
    assert "AMZN10" in blob and "252.24" in blob
    assert "NVDA11" in blob and "221.07" in blob
    assert "QCOM26" in blob and "190.26" in blob
    assert "AMZN stp 248.9" in blob
    assert "SPY 762.28" in blob
    assert "expectancy" not in out


def test_brief_skips_pulling_opener_and_uses_marks(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    snap = {
        "ibkr_live_quotes": {"AMZN": 252.24},
        "positions": [{"symbol": "AMZN", "qty": 10, "avg": 251.76}],
        "working_orders": [{"symbol": "AMZN", "type": "STP", "stop": 248.9, "role": "exit"}],
    }
    class _Turn:
        text = "IBKR is back; three overnight STK lots are protected. Pulling blotter, quotes, and structure — send still blocked."
        tool_trace = ["book", "quote"]

    out = write_research_brief(session="premarket", snap=snap, turn=_Turn())
    assert "Pulling blotter" not in out["conclusion"]
    assert "AMZN10 252.24" in out["conclusion"]
    assert "no spoken conclude" in out["conclusion"]
