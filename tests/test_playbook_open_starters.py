"""Locked OPEN-type starters fill empty trunks without wiping live cards."""

from __future__ import annotations

import json
import re

from abcxauto.lab_playbook import (
    OPEN_TYPE_STARTERS,
    PROTECTED_CARD_NAMES,
    clamp_update,
    load_lab,
    load_live,
    open_playbook_types,
    playbook_type_keys,
    save_lab,
    type_cards,
    type_schema_echo_keys,
    walk_cards,
)
from abcxauto.order_examples import ORDER_EXAMPLES

_TICKER_RE = re.compile(
    r"\b(SPY|QQQ|IWM|DIA|NVDA|AAPL|TSLA|AMZN|MSFT|META|GOOGL|GOOG|WMT|"
    r"COST|SNDK|MU|ALB|TGT|XLE|XLF|BABA|AMD|NFLX|INTC|BA|F|GM)\b"
)
_HIT_RATE_RE = re.compile(r"(?i)hit[\s-]?rate|expect_hit_rate|win rate")
_NAP_RE = re.compile(r"(?i)\bnap\b|next_look_s|wake_at|10:30\s*ET|diary")
_SKIP_TRUNKS = (
    "cancel_order",
    "modify_stop",
    "modify_target",
    "market_order",
    "limit_order",
    "stop_order",
    "stop_limit",
    "close_option",
    "trailing_stop",
    "trailing_stop_limit",
    "market_on_close",
    "limit_on_close",
    "market_on_open",
    "limit_on_open",
    "adaptive",
    "midprice",
    "relative",
    "fill_or_kill",
    "immediate_or_cancel",
    "vwap",
    "twap",
    "iceberg",
    "snap_to_midpoint",
    "roll_option",
    "set_risk",
    "self_tune",
    "ratio_spread",
    "jade_lizard",
    "buy_option",
)


def _flush(name: str, **over) -> dict:
    row = {
        "name": name,
        "thesis": "structure: flush into support then bounce",
        "when_on": "liquid large/mega gap holding the open low",
        "scan": "most_active + top_losers; large/mega only",
        "shape": "LONG STK market_bracket. Stop under opening low.",
        "invalidation": "stop through opening low",
        "status": "testing",
    }
    row.update(over)
    return row


def _todays_three_type_book() -> dict:
    """Collapsed paper book: three live flush cards, empty vertical, skip retired."""
    return {
        "mode": "explore",
        "revision": 9,
        "instructions": "flush book",
        "types": {
            "market_bracket": {
                "cards": [
                    _flush(
                        "mega-cap earnings-flush bounce",
                        thesis="mega-cap sales-miss gap retraces into the opening range",
                        when_on="mega/large >=6% earnings-miss gap holding the open low",
                    ),
                    _flush(
                        "large-cap 3pct gap hold",
                        thesis="large-cap open gap holds above the open",
                        when_on="mega/large >=3% open gap, hold above the open",
                    ),
                    _flush(
                        "news-miss large-cap flush",
                        thesis="news-miss flush in a large-cap, defined-risk bounce",
                        when_on="large-cap news miss with a gap that holds",
                    ),
                    {
                        "name": "levered-crypto and micro gap chase",
                        "when_on": "Never.",
                        "scan": "top_gainers, high_open_gap",
                        "shape": "none",
                        "invalidation": "n/a",
                        "status": "retired",
                        "note": "Not an edge.",
                    },
                ]
            },
            "vertical_spread": {"cards": []},
            "buy_option": {
                "cards": [
                    {
                        "name": "naked / short-dated option spray",
                        "when_on": "Never.",
                        "scan": "n/a",
                        "shape": "none",
                        "invalidation": "n/a",
                        "status": "retired",
                        "note": "Options were the inception drawdown.",
                    }
                ]
            },
        },
    }


def test_empty_lab_stays_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    assert load_lab() == {}


def test_open_starters_have_required_fields_and_no_tickers_or_fake_pct():
    required = ("name", "thesis", "when_on", "scan", "shape", "invalidation", "tool_order")
    for type_name in open_playbook_types():
        spec = OPEN_TYPE_STARTERS[type_name]
        for key in required:
            assert spec.get(key), (type_name, key)
        blob = json.dumps(spec)
        assert _TICKER_RE.search(blob) is None, (type_name, blob)
        assert _HIT_RATE_RE.search(blob) is None, (type_name, blob)
        assert _NAP_RE.search(blob) is None, (type_name, blob)
        assert "expect_hit_rate" not in spec
        assert "next_look_s" not in spec
        assert "diary" not in spec
        assert spec["name"].lower() not in {n.lower() for n in PROTECTED_CARD_NAMES}


def test_loading_todays_three_type_book_seeds_missing_open_types(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import _lab_path, _write

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    raw = _todays_three_type_book()
    _write(_lab_path(), raw)

    lab = load_lab()
    types = lab["types"]
    assert type_schema_echo_keys(types) == []

    mb_names = [c["name"] for c in type_cards(types, "market_bracket")]
    assert mb_names[:3] == [
        "mega-cap earnings-flush bounce",
        "large-cap 3pct gap hold",
        "news-miss large-cap flush",
    ]
    assert "levered-crypto and micro gap chase" in mb_names
    assert "generic STK market bracket" not in mb_names

    flush = type_cards(types, "market_bracket")[0]
    assert flush["thesis"].startswith("mega-cap sales-miss")
    assert flush["when_on"].startswith("mega/large >=6%")
    assert flush.get("locked") is not True

    naked = type_cards(types, "buy_option")
    assert [c["name"] for c in naked] == ["naked / short-dated option spray"]
    assert naked[0]["status"] == "retired"

    vs_names = [c["name"] for c in type_cards(types, "vertical_spread")]
    assert vs_names == ["defined-risk debit/credit vertical"]
    assert type_cards(types, "vertical_spread")[0]["locked"] is True
    assert type_cards(types, "vertical_spread")[0]["status"] == "testing"

    for type_name in open_playbook_types():
        live = [
            c
            for c in type_cards(types, type_name)
            if str(c.get("status") or "").lower() != "retired"
        ]
        assert live, type_name
        if type_name == "market_bracket":
            continue
        starter = OPEN_TYPE_STARTERS[type_name]
        names = {c["name"] for c in type_cards(types, type_name)}
        assert starter["name"] in names, type_name
        got = next(c for c in type_cards(types, type_name) if c["name"] == starter["name"])
        assert got["status"] == "testing"
        assert got["locked"] is True
        assert got["thesis"] == starter["thesis"]
        assert got["when_on"] == starter["when_on"]
        assert got["scan"] == starter["scan"]
        assert got["shape"] == starter["shape"]
        assert got["invalidation"] == starter["invalidation"]
        assert got["tool_order"] == starter["tool_order"]
        assert "expect_hit_rate" not in got
        assert "next_look_s" not in got
        assert not (got.get("note") or "").strip()
        blob = json.dumps(got)
        assert _TICKER_RE.search(blob) is None, (type_name, blob)
        assert _HIT_RATE_RE.search(blob) is None, (type_name, blob)
        assert _NAP_RE.search(blob) is None, (type_name, blob)

    seeded_types = set(types)
    for skip in _SKIP_TRUNKS:
        if skip == "buy_option":
            continue
        assert skip not in seeded_types, skip
    assert "ratio_spread" not in seeded_types
    assert "jade_lizard" not in seeded_types
    assert "buy_option" in seeded_types


def test_defined_risk_flush_debit_kept_and_generic_vertical_added(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import _lab_path, _write

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    _write(
        _lab_path(),
        {
            "types": {
                "market_bracket": {
                    "cards": [_flush("mega-cap earnings-flush bounce")]
                },
                "vertical_spread": {
                    "cards": [
                        {
                            "name": "defined-risk flush debit",
                            "thesis": "same flush tape as a debit vertical",
                            "when_on": "same flush tape, gap holds, defined-risk only",
                            "scan": "most_active + top_losers; option_facts first",
                            "shape": "debit vertical. option_facts first.",
                            "invalidation": "debit no longer defined",
                            "status": "testing",
                        }
                    ]
                },
            }
        },
    )
    lab = load_lab()
    vs = [c["name"] for c in type_cards(lab["types"], "vertical_spread")]
    assert vs[0] == "defined-risk flush debit"
    assert "defined-risk debit/credit vertical" in vs
    debit = type_cards(lab["types"], "vertical_spread")[0]
    assert debit["shape"] == "debit vertical. option_facts first."
    assert debit.get("locked") is not True


def test_flat_three_card_collapse_does_not_wipe_starters(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "types": {
                    "market_bracket": {
                        "cards": [
                            _flush("mega-cap earnings-flush bounce"),
                            _flush("large-cap 3pct gap hold"),
                            _flush("news-miss large-cap flush"),
                        ]
                    },
                    "iron_condor": {
                        "cards": [
                            {
                                **OPEN_TYPE_STARTERS["iron_condor"],
                                "status": "testing",
                                "locked": True,
                            }
                        ]
                    },
                }
            }
        )
    )
    # The wipe that happened in the lab: a flat cards list of the three flush names.
    save_lab(
        clamp_update(
            {
                "cards": [
                    {
                        **_flush("mega-cap earnings-flush bounce"),
                        "ticket": "market_bracket",
                    },
                    {
                        **_flush("large-cap 3pct gap hold"),
                        "ticket": "market_bracket",
                    },
                    {
                        **_flush("news-miss large-cap flush"),
                        "ticket": "market_bracket",
                    },
                ]
            }
        )
    )
    lab = load_lab()
    mb = [c["name"] for c in type_cards(lab["types"], "market_bracket")]
    assert mb[:3] == [
        "mega-cap earnings-flush bounce",
        "large-cap 3pct gap hold",
        "news-miss large-cap flush",
    ]
    for type_name in open_playbook_types():
        if type_name == "market_bracket":
            continue
        names = {c["name"] for c in type_cards(lab["types"], type_name)}
        assert OPEN_TYPE_STARTERS[type_name]["name"] in names, type_name


def test_grok_card_on_a_type_is_not_joined_with_a_duplicate_starter(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "types": {
                    "iron_condor": {
                        "cards": [
                            {
                                "name": "condor grind",
                                "thesis": "range-bound listed wings",
                                "status": "testing",
                            }
                        ]
                    }
                }
            }
        )
    )
    names = [c["name"] for c in type_cards(load_lab()["types"], "iron_condor")]
    assert names == ["condor grind"]


def test_live_snapshot_is_not_seeded(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import _live_path, _write

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    _write(
        _live_path(),
        {
            "promoted": True,
            "types": {
                "market_bracket": {
                    "cards": [_flush("mega-cap earnings-flush bounce")]
                }
            },
            "graduated": ["mega-cap earnings-flush bounce"],
        },
    )
    live = load_live()
    assert set(live["types"]) == {"market_bracket"}
    assert [c["name"] for c in type_cards(live["types"], "market_bracket")] == [
        "mega-cap earnings-flush bounce"
    ]
    assert "iron_condor" not in live["types"]


def test_starters_are_not_order_example_schema_echoes(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(clamp_update({"types": {"bracket": {"gotchas": "limit entry can hang"}}}))
    lab = load_lab()
    assert type_schema_echo_keys(lab["types"]) == []
    blob = json.dumps(OPEN_TYPE_STARTERS)
    assert "NVDA" not in blob
    assert "SPY" not in blob
    assert "AAPL" not in blob
    for type_name, spec in OPEN_TYPE_STARTERS.items():
        assert type_name in ORDER_EXAMPLES
        assert "open_shape" not in spec
        assert "close_tp_sl" not in spec


def test_locked_starters_are_catalog_not_a_parallel_hunt(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import lab_facts, playbook_run_sheets

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "types": {
                    "market_bracket": {
                        "cards": [_flush("mega-cap earnings-flush bounce")]
                    }
                }
            }
        )
    )
    lab = load_lab()
    sheets = playbook_run_sheets(lab, flat=True)
    assert [row["card"] for row in sheets] == ["mega-cap earnings-flush bounce"]
    awaiting = [r["card"] for r in lab_facts(lab)["cards_awaiting_first_trade"]]
    assert awaiting == ["mega-cap earnings-flush bounce [market_bracket]"]
    assert any(
        c.get("locked") is True for _t, c in walk_cards(lab)
    )


def test_locked_starters_do_not_clear_a_live_gap_floor(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import _session_gap_mag, _tightest_matching_card

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "types": {
                    "market_bracket": {
                        "cards": [
                            _flush(
                                "mega-cap earnings-flush bounce",
                                when_on="mega/large ≥6% earnings-miss gap",
                            )
                        ]
                    }
                }
            }
        )
    )
    session = {
        "today": True,
        "low": 900.0,
        "last": 910.0,
        "above_low": True,
        "open_gap_pct": -3.3,
    }
    mag = _session_gap_mag(session)
    assert mag == 3.3
    assert _tightest_matching_card(load_lab(), mag) is None


def test_open_types_are_entry_trunks_not_exits():
    allowed = set(playbook_type_keys())
    for name in open_playbook_types():
        assert name in allowed
        assert name not in _SKIP_TRUNKS
    for skip in ("ratio_spread", "jade_lizard", "trailing_stop", "cancel_order"):
        assert skip not in open_playbook_types()


def test_seeded_book_still_has_no_schema_echo_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(clamp_update({"types": {"bracket": {"gotchas": "limit entry can hang"}}}))
    disk = json.loads((tmp_path / "lab.json").read_text(encoding="utf-8"))
    assert type_schema_echo_keys(disk.get("types")) == []
    assert "open_shape" not in json.dumps(disk)
    assert "close_tp_sl" not in json.dumps(disk)
    assert disk["types"]["bracket"]["gotchas"] == "limit entry can hang"
