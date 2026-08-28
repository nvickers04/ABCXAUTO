"""Wake leads with the worst fact — before vol color, before the think stream."""

from __future__ import annotations

from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.world_state import format_wake, worst_wake_fact
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


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
    assert "stop_dist=AAPL STK long 20 2.12 to 178.0" in fact
    assert "session_cap" not in fact


def test_working_order_missing_beats_session_cap():
    fact = worst_wake_fact(
        unprotected=[],
        day={
            "working_order_missing": ["QQQ 260918C500 long 1"],
            "session_cap": {"looks_left": 40, "tokens_left": 1_000_000},
        },
    )
    assert fact.startswith("working_order_missing=QQQ")
