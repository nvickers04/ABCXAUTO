"""Look continuity: wake lead lines + model-look skip gate."""

from __future__ import annotations

from abcxauto.pro_engine import skip_model_look
from abcxauto.world_state import format_wake


def test_skip_same_print_card_closed():
    assert skip_model_look(
        card_open=False,
        same_print=True,
        book_unreliable=False,
        prior_snap_age_s=None,
    )


def test_skip_card_open_does_not_skip():
    assert not skip_model_look(
        card_open=True,
        same_print=True,
        book_unreliable=False,
        prior_snap_age_s=10,
    )


def test_no_ticket_open_card_blocks_unchanged_skip():
    """After a no-ticket save, cash_undeployed keeps the card open — no skip."""
    from abcxauto import session_work as sw

    card = sw.empty_card()
    card["cash_undeployed"] = True
    card = sw.note_no_ticket(card)
    assert sw.card_is_open(card) is True
    assert card["ruled_out"] == []
    assert not skip_model_look(
        card_open=sw.card_is_open(card),
        same_print=True,
        book_unreliable=False,
        prior_snap_age_s=10,
    )


def test_skip_book_unreliable_fresh_prior_does_not_skip():
    assert not skip_model_look(
        card_open=False,
        same_print=True,
        book_unreliable=True,
        prior_snap_age_s=30,
    )


def test_skip_book_unreliable_stale_prior_skips():
    assert skip_model_look(
        card_open=False,
        same_print=True,
        book_unreliable=True,
        prior_snap_age_s=120,
    )


def test_format_wake_work_line_before_closest_stop():
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "work_line": "continuing cash=$24624 held=AVGO22 candidate=none",
            "names": 1,
            "lots": 1,
            "nl": 100_000.0,
            "daily_pnl": 0.0,
            "max_risk_per_trade_pct": 5.0,
            "capacity": {"open_count": 1, "max_open_positions": 0},
            "stop_dist": {
                "ident": "AVGO STK long 22",
                "dist": 1.5,
                "stop": 300.0,
                "last": 301.5,
            },
        },
    )
    lines = text.splitlines()
    assert lines[0].startswith("continuing cash=")
    assert "closest_stop" in lines[1]
    low = text.lower()
    for banned in ("sell", "rotate", "should", "must"):
        assert banned not in low


def test_format_wake_pace_line_first_then_work_line():
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "pace_line": "pace=$0/h idle_h=1.2",
            "work_line": "continuing cash=$24624 held=AVGO22 candidate=none",
            "names": 1,
            "lots": 1,
            "nl": 100_000.0,
            "daily_pnl": 0.0,
            "max_risk_per_trade_pct": 5.0,
            "capacity": {"open_count": 1, "max_open_positions": 0},
            "stop_dist": {
                "ident": "AVGO STK long 22",
                "dist": 1.5,
                "stop": 300.0,
                "last": 301.5,
            },
        },
    )
    lines = text.splitlines()
    assert lines[0].startswith("pace=")
    assert lines[1].startswith("continuing cash=")
    assert "closest_stop" in lines[2]


def test_format_wake_book_stale_bit():
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "book_stale": True,
            "names": 0,
            "lots": 0,
            "nl": 50_000.0,
            "daily_pnl": 0.0,
            "max_risk_per_trade_pct": 5.0,
            "capacity": {"open_count": 0, "max_open_positions": 0},
        },
    )
    assert "book_stale" in text
