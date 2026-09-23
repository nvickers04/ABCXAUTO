"""Session work card: undeployed cash, no-ticket, rule-out, pace."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from abcxauto import session_work as sw

_ET = ZoneInfo("America/New_York")


def test_load_save_roundtrip(tmp_path: Path):
    path = tmp_path / "session_work.json"
    card = sw.empty_card()
    card.update(
        {
            "session": "regular",
            "asof": "2026-09-23T10:00:00-04:00",
            "cash": 24624.0,
            "held": [{"symbol": "AVGO", "qty": 22}],
            "ruled_out": ["INTC", "MU"],
            "candidate": {"symbol": "AMD", "sizeable": True},
            "gaps": ["chain"],
            "cash_undeployed": True,
            "pace_usd_per_h": 12.5,
            "idle_h": 1.5,
        }
    )
    saved = sw.save(card, path=path)
    assert "verdict" not in saved
    loaded = sw.load(path=path)
    assert loaded["cash"] == 24624.0
    assert loaded["held"] == [{"symbol": "AVGO", "qty": 22.0}]
    assert loaded["ruled_out"] == ["INTC", "MU"]
    assert loaded["candidate"]["symbol"] == "AMD"
    assert loaded["cash_undeployed"] is True
    assert "verdict" not in loaded


def test_undeployed_cash_means_card_is_open():
    card = sw.empty_card()
    card["cash_undeployed"] = True
    assert sw.card_is_open(card) is True


def test_sizeable_candidate_means_card_is_open():
    card = sw.empty_card()
    card["cash_undeployed"] = False
    card["candidate"] = {"symbol": "AMD", "sizeable": True}
    assert sw.card_is_open(card) is True


def test_note_no_ticket_does_not_rule_out_and_does_not_close():
    card = {
        "session": "regular",
        "asof": "",
        "cash": 24624.0,
        "held": [{"symbol": "AVGO", "qty": 22}],
        "ruled_out": [],
        "candidate": {"symbol": "AMD", "sizeable": True},
        "gaps": [],
        "cash_undeployed": True,
        "pace_usd_per_h": 0.0,
        "idle_h": 2.0,
    }
    assert sw.card_is_open(card) is True
    out = sw.note_no_ticket(card)
    assert out["ruled_out"] == []
    assert out["cash_undeployed"] is True
    assert out["candidate"]["symbol"] == "AMD"
    assert sw.card_is_open(out) is True


def test_rule_out_removes_that_name_from_candidate():
    card = {
        "session": "regular",
        "asof": "",
        "cash": 1000.0,
        "held": [],
        "ruled_out": [],
        "candidate": {"symbol": "INTC", "sizeable": True},
        "gaps": [],
        "cash_undeployed": True,
        "pace_usd_per_h": 0.0,
        "idle_h": 0.0,
    }
    out = sw.rule_out(card, "INTC")
    assert "INTC" in out["ruled_out"]
    assert out["candidate"] is None
    # Ruled-out name is gone; next candidate must not be that symbol.
    assert (out.get("candidate") or {}).get("symbol") != "INTC"


def test_mark_sent_closes_that_candidate():
    card = sw.empty_card()
    card["candidate"] = {"symbol": "AMD", "sizeable": True}
    card["cash_undeployed"] = True
    out = sw.mark_sent(card, "AMD")
    assert out["candidate"] is None


def test_pace_is_daily_pnl_over_hours():
    # Regular: 10:30 ET → 1.0 hour since 9:30.
    now = datetime(2026, 9, 23, 10, 30, tzinfo=_ET)
    pace = sw.pace_usd_per_h(50.0, "regular", now=now)
    assert pace == 50.0

    # Premarket: 6:00 ET → 2.0 hours since 4:00.
    now_pm = datetime(2026, 9, 23, 6, 0, tzinfo=_ET)
    pace_pm = sw.pace_usd_per_h(40.0, "premarket", now=now_pm)
    assert pace_pm == 20.0

    # Closed uses the same 4:00 clock.
    now_cl = datetime(2026, 9, 23, 8, 0, tzinfo=_ET)
    assert sw.pace_usd_per_h(8.0, "closed", now=now_cl) == 2.0

    # Negative pnl → negative pace.
    assert sw.pace_usd_per_h(-30.0, "regular", now=now) == -30.0

    # Near the open: floor hours at 1.0 (not 1/60).
    now_open = datetime(2026, 9, 23, 9, 30, 0, tzinfo=_ET)
    pace_floor = sw.pace_usd_per_h(-20.0, "regular", now=now_open)
    assert pace_floor == -20.0
    assert sw.session_hours("regular", now=now_open) == 1.0


def test_no_ticket_save_leaves_card_open_without_ruled_out(tmp_path: Path):
    """Spoken no-ticket path: cash_undeployed stays, card open, no rule_out."""
    path = tmp_path / "session_work.json"
    card = sw.empty_card()
    card["cash"] = 32515.0
    card["session"] = "regular"
    card["cash_undeployed"] = True
    card["idle_since"] = "2026-09-23T09:35:00-04:00"
    out = sw.note_no_ticket(card)
    assert out["ruled_out"] == []
    assert out["cash_undeployed"] is True
    assert sw.card_is_open(out) is True
    saved = sw.save(out, path=path)
    loaded = sw.load(path=path)
    assert loaded["ruled_out"] == []
    assert loaded["cash_undeployed"] is True
    assert loaded["idle_since"] == "2026-09-23T09:35:00-04:00"
    assert sw.card_is_open(loaded) is True
    assert "verdict" not in saved


def test_idle_h_uses_idle_since_iso():
    now = datetime(2026, 9, 23, 11, 0, tzinfo=_ET)
    since = "2026-09-23T09:00:00-04:00"
    assert sw.idle_h(idle_since=since, now=now) == 2.0


def test_continuing_line_compact_no_advice_words():
    card = {
        "cash": 24624.4,
        "held": [{"symbol": "AVGO", "qty": 22}],
        "candidate": None,
        "ruled_out": ["INTC", "MU"],
        "pace_usd_per_h": 0.0,
        "idle_h": 3,
        "cash_undeployed": True,
        "gaps": [],
        "session": "regular",
        "asof": "",
    }
    line = sw.continuing_line(card)
    assert line == (
        "continuing cash=$24624 held=AVGO22 candidate=none "
        "ruled_out=INTC,MU pace=$0/h idle_h=3"
    )
    for banned in ("sell", "rotate", "should", "must"):
        assert banned not in line.lower()


def test_fingerprint_cash_stops_qtys():
    card = {"cash": 24624.4, "session": "regular"}
    lots = [
        {"symbol": "AVGO", "qty": 22, "stop": 160.5},
        {"symbol": "MU", "qty": 10, "stop": 95.0},
    ]
    fp = sw.fingerprint(card, lots)
    assert fp[0] == 24624
    assert fp[1] == "regular"
    assert ("AVGO", 22.0, 160.5) in fp[2]
    assert ("MU", 10.0, 95.0) in fp[2]
    # Same to the dollar / stop / qty → same fingerprint.
    card2 = {"cash": 24624.1, "session": "regular"}
    assert sw.fingerprint(card2, lots) == fp
