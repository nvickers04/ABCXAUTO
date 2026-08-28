"""Wake leads with the worst fact — before vol color, before the think stream."""

from __future__ import annotations

import re

from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.world_state import WAKE_FACT_PREFIX, format_wake, worst_wake_fact
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

_TRAILING_BARE_SEND = re.compile(r"(?:^|\s)send\.\s*$")


def test_system_prompt_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_unprotected_stk_is_the_leading_wake_line():
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=["AAPL STK"],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "open_lots": ["AAPL STK long 20"],
            "vol_bit": "IWM rv=high iv=22.0",
            "session_cap": {"looks_left": 12, "tokens_left": 100000},
            "ibkr_daily_pnl": -80.0,
            "halt_trips_at_usd": -9250.0,
            "ibkr_day_vs_halt": 9170.0,
        },
    )
    lead = text.splitlines()[0]
    assert lead == "unprotected=AAPL STK."
    assert "vol=IWM rv=high" in text
    assert text.index("unprotected=AAPL STK") < text.index("vol=")
    assert "session=regular" in text
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_session_cap_remaining_leads_when_the_book_is_clean():
    text = format_wake(
        cycle=2,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 0,
            "lots": 0,
            "open_lots": [],
            "vol_bit": "IWM rv=mid iv=18.0",
            "session_cap": {
                "looks_left": 12,
                "tokens_left": 100000,
                "look_cap": 160,
                "token_cap": 2_500_000,
            },
            "ibkr_daily_pnl": 0.0,
            "halt_trips_at_usd": -9250.0,
            "ibkr_day_vs_halt": 9250.0,
        },
    )
    lead = text.splitlines()[0]
    assert lead.startswith("session_cap remaining=")
    assert "12 looks" in lead
    assert "vol=IWM rv=mid" in text
    assert text.index("session_cap remaining=") < text.index("vol=")
    assert "session_look_cap" not in text
    assert "session_token_cap" not in SYSTEM_PROMPT


def test_unprotected_beats_session_cap_and_vol():
    fact = worst_wake_fact(
        unprotected=["MSFT STK"],
        day={
            "session_cap": {"looks_left": 3, "tokens_left": 9},
            "vol_bit": "SPY rv=high",
        },
        session="regular",
    )
    assert fact.startswith("unprotected=MSFT STK")
    assert "session_cap" not in fact
    assert "vol=" not in fact


def test_stop_distance_beats_session_cap():
    fact = worst_wake_fact(
        unprotected=[],
        day={
            "stop_dist": {
                "ident": "AAPL STK long 20",
                "last": 180.12,
                "stop": 178.0,
                "dist": 2.12,
            },
            "session_cap": {"looks_left": 40, "tokens_left": 1_000_000},
        },
    )
    assert fact == (
        f"{WAKE_FACT_PREFIX} closest_stop AAPL STK long 20 "
        "dist=2.12 stop=178.0 last=180.12"
    )
    assert not fact.startswith("stop_dist=")
    assert "session_cap" not in fact


def test_working_order_missing_beats_session_cap():
    fact = worst_wake_fact(
        unprotected=[],
        day={
            "working_order_missing": ["QQQ 260918C500 long 1"],
            "session_cap": {"looks_left": 40, "tokens_left": 1_000_000},
        },
    )
    assert fact == f"{WAKE_FACT_PREFIX} working_order_missing QQQ 260918C500 long 1"
    assert not fact.startswith("working_order_missing=")


def test_protected_stop_dist_wake_leads_with_fact_prefix():
    """Protected closest_stop is a desk fact, not a bare order ticket."""
    from abcxauto.park_clock import note_wake

    note_wake(None)
    day = {
        "names": 1,
        "lots": 1,
        "nl": 100_000.0,
        "daily_pnl": 0.0,
        "max_risk_per_trade_pct": 5.0,
        "sizing_floors": False,
        "capacity": {"open_count": 1, "max_open_positions": 15, "nl": 100_000.0},
        "open_lots": ["PYPL STK long 50"],
        "mix": {"stk": 1},
        "vol_bit": "IWM rv=mid iv=18.0",
        "session_cap": {"looks_left": 40, "tokens_left": 1_000_000},
        "stop_dist": {
            "ident": "PYPL STK long 50",
            "last": 54.09,
            "stop": 52.61,
            "dist": 1.48,
        },
    }
    fact = worst_wake_fact(unprotected=[], day=day)
    assert fact == (
        f"{WAKE_FACT_PREFIX} closest_stop PYPL STK long 50 "
        "dist=1.48 stop=52.61 last=54.09"
    )
    assert fact.startswith(f"{WAKE_FACT_PREFIX} ")
    assert not fact.startswith("stop_dist=")
    assert "stop_dist=PYPL" not in fact
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day=day,
    )
    lead = text.splitlines()[0]
    assert lead == fact + "."
    assert lead.startswith(f"{WAKE_FACT_PREFIX} closest_stop PYPL")
    assert not lead.startswith("stop_dist=")
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_format_wake_is_desk_facts_not_a_send_command():
    """Wake is book facts. Trailing send. was leftover tool-naming, not a command."""
    from abcxauto.park_clock import note_wake

    note_wake(None)
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=["AAPL STK"],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "nl": 100_000.0,
            "daily_pnl": 0.0,
            "max_risk_per_trade_pct": 5.0,
            "sizing_floors": False,
            "capacity": {"open_count": 1, "max_open_positions": 15, "nl": 100_000.0},
            "open_lots": ["AAPL STK long 20"],
            "mix": {"stk": 1},
            "vol_bit": "IWM rv=high iv=22.0",
            "session_cap": {"looks_left": 12, "tokens_left": 100000},
        },
    )
    lead = text.splitlines()[0]
    assert lead == "unprotected=AAPL STK."
    assert "session=regular" in text
    assert "flat=False" in text
    assert "open_lots=AAPL STK long 20" in text
    assert "names=1" in text
    assert "lots=1" in text
    assert "max_risk=5.0%" in text
    assert "mix=" in text
    assert "vol=IWM rv=high" in text
    stripped = text.rstrip()
    assert stripped.split()[-1] != "send."
    assert not stripped.endswith("send.")
    assert _TRAILING_BARE_SEND.search(stripped) is None
    assert stripped.splitlines()[-1].strip() != "send."
    lower = stripped.lower()
    assert "you may send" not in lower
    assert "use the send tool" not in lower
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_closest_stop_last_tick_is_not_a_move():
    from abcxauto.world_state import (
        closest_stop_moved_more_than_a_tick,
        desk_fact_is_duplicate,
        omit_duplicate_fact_lead,
        parse_closest_stop,
    )

    a = "fact: closest_stop PYPL STK long 50 dist=1.48 stop=52.61 last=54.09."
    b = "fact: closest_stop PYPL STK long 50 dist=1.47 stop=52.61 last=54.08."
    moved_stop = (
        "fact: closest_stop PYPL STK long 50 dist=2.61 stop=51.50 last=54.11."
    )
    other = "fact: closest_stop ADSK STK long 10 dist=1.00 stop=280.0 last=281.0."
    assert parse_closest_stop(a)["ident"] == "PYPL STK long 50"
    assert parse_closest_stop(a)["stop"] == 52.61
    assert closest_stop_moved_more_than_a_tick(a, b) is False
    assert desk_fact_is_duplicate(a, a) is True
    assert desk_fact_is_duplicate(a, b) is True
    assert desk_fact_is_duplicate(a, moved_stop) is False
    assert closest_stop_moved_more_than_a_tick(a, other) is True
    assert desk_fact_is_duplicate(a, other) is False
    assert closest_stop_moved_more_than_a_tick(a, moved_stop) is True
    assert desk_fact_is_duplicate("session=regular send.", "session=regular send.") is False
    body = a + "\nsession=regular flat=False unprotected=none ibkr=up."
    assert omit_duplicate_fact_lead(a, body) == (
        "session=regular flat=False unprotected=none ibkr=up."
    )
    assert omit_duplicate_fact_lead(a, a) == ""
    assert omit_duplicate_fact_lead(a, moved_stop + "\nsession=regular.") == (
        moved_stop + "\nsession=regular."
    )
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK

