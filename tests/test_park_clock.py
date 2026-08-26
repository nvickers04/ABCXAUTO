"""Event push + overnight park clock. RTH has no sit clock."""

from datetime import datetime, timedelta, timezone

from abcxauto.park_clock import (
    BookEvent,
    GrokAlarm,
    book_fingerprint,
    events_from_diff,
    set_wake,
    should_wake_grok,
)


def test_first_boot_wakes_once():
    ev = should_wake_grok([], first_boot=True)
    assert ev is not None
    assert ev.kind == "boot"


def test_quiet_book_does_not_wake():
    ev = should_wake_grok([], first_boot=False, operator=False, alarm=GrokAlarm())
    assert ev is None


def test_fill_wakes_when_alarm_empty():
    ev = should_wake_grok(
        [BookEvent("fill", "QQQ")],
        alarm=GrokAlarm(),
        first_boot=False,
    )
    assert ev is not None
    assert ev.kind == "fill"


def test_grok_can_narrow_wake_if(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    set_wake(wake_if=["fill"])
    alarm = GrokAlarm(wake_if=["fill"])
    assert should_wake_grok(
        [BookEvent("session_change", "premarket->regular")],
        alarm=alarm,
    ) is None
    got = should_wake_grok([BookEvent("fill", "x")], alarm=alarm)
    assert got is not None and got.kind == "fill"


def test_unprotected_always_wakes():
    alarm = GrokAlarm(wake_if=["fill"])
    got = should_wake_grok(
        [BookEvent("unprotected", "NVDA")],
        alarm=alarm,
    )
    assert got is not None and got.kind == "unprotected"


def test_alarm_due():
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    alarm = GrokAlarm(wake_at=past, wake_if=["fill"])
    got = should_wake_grok([], alarm=alarm)
    assert got is not None and got.kind == "alarm"


def test_diff_detects_fill_and_session():
    a = book_fingerprint({
        "fills": [],
        "open_orders": [],
        "positions": [{"conId": 1}],
        "protection": {"unprotected_symbols": []},
        "market_hours": {"session": {"status": "premarket"}},
        "ibkr_connected": True,
    })
    b = book_fingerprint({
        "fills": [{"symbol": "QQQ", "quantity": 1, "price": 2.0, "exec_id": "e1"}],
        "open_orders": [],
        "positions": [{"conId": 1}],
        "protection": {"unprotected_symbols": []},
        "market_hours": {"session": {"status": "regular"}},
        "ibkr_connected": True,
    })
    kinds = {e.kind for e in events_from_diff(a, b)}
    assert "fill" in kinds
    assert "session_change" in kinds


def test_ensure_next_look_if_grok_silent(tmp_path, monkeypatch):
    from abcxauto.park_clock import ensure_next_look, load_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_DEFAULT_LOOK_S", "30")
    # Overnight silent still seeds a park. Paper premarket stays up (no sit clock).
    alarm = ensure_next_look(previous_set_at="", session="closed")
    assert alarm.wake_at
    assert load_alarm().wake_at == alarm.wake_at


def test_finished_rth_look_does_not_write_a_sit_clock(tmp_path, monkeypatch):
    """Clerk is not a runner. A finished regular look must not seed grok_wake.json."""
    from abcxauto.park_clock import ensure_next_look, load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_DEFAULT_LOOK_S", "30")
    alarm = ensure_next_look(
        previous_set_at="",
        flat=False,
        session="regular",
    )
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None
    replaced = ensure_next_look(session="regular", flat=True, replace=True)
    assert replaced.wake_at is None
    assert load_alarm().wake_at is None
    # Even an explicit set_wake in RTH is not a sit clock.
    assert set_wake(wake_in_s=90, session="regular", flat=True).wake_at is None
    assert load_alarm().wake_at is None


def test_ensure_next_look_keeps_grok_alarm(tmp_path, monkeypatch):
    from abcxauto.park_clock import ensure_next_look, load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    set_wake(wake_in_s=120, wake_if=["fill"], session="closed")
    before = load_alarm()
    out = ensure_next_look(previous_set_at="", session="closed")
    assert out.wake_if == ["fill"]
    assert out.set_at == before.set_at
    assert out.wake_at


def test_set_wake_still_parks_overnight(tmp_path, monkeypatch):
    """set_wake park outside RTH is a real shutdown clock (new think next session)."""
    from abcxauto.park_clock import _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    alarm = set_wake(wake_in_s=8 * 3600, session="closed", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert 8 * 3600 - 30 <= remaining <= 8 * 3600 + 30


def test_live_interrupt_note_and_take():
    from abcxauto.park_clock import (
        BookEvent,
        clear_interrupt,
        note_interrupt,
        peek_interrupt,
        take_interrupt,
    )

    clear_interrupt()
    note_interrupt(BookEvent("fill", "QQQ filled"))
    assert peek_interrupt() is not None
    assert peek_interrupt().kind == "fill"
    got = take_interrupt()
    assert got is not None and got.kind == "fill"
    assert take_interrupt() is None
    note_interrupt(BookEvent("alarm", "nope"))
    assert peek_interrupt() is None
    note_interrupt(BookEvent("unprotected", "SPY"))
    assert take_interrupt().kind == "unprotected"
    clear_interrupt()


def test_paper_rth_set_wake_writes_no_sit_clock(tmp_path, monkeypatch):
    """RTH has no sit clock. Clerk is not a runner."""
    from abcxauto.park_clock import load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    alarm = set_wake(wake_in_s=600, session="regular", flat=True, wake_if=["fill"])
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_paper_premarket_set_wake_writes_no_sit_clock(tmp_path, monkeypatch):
    from abcxauto.park_clock import load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    alarm = set_wake(wake_in_s=3600, session="premarket", flat=True)
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_overnight_park_honors_a_long_wait(tmp_path, monkeypatch):
    from abcxauto.park_clock import _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    alarm = set_wake(wake_in_s=4 * 3600, session="closed", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    assert (at - _utc_now()).total_seconds() > 3 * 3600


def test_set_wake_offered_in_no_session():
    from abcxauto.park_clock import set_wake_offered

    for sess in ("regular", "premarket", "postmarket", "closed", ""):
        assert set_wake_offered(session=sess) is False, sess


def test_set_wake_live_premarket_writes_no_sit_clock(tmp_path, monkeypatch):
    from abcxauto.park_clock import load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    alarm = set_wake(wake_in_s=3600, session="premarket", flat=True)
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_set_wake_live_rth_writes_no_sit_clock(tmp_path, monkeypatch):
    from abcxauto.park_clock import load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    alarm = set_wake(wake_in_s=3600, session="regular", flat=False)
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_set_wake_paper_halted_rth_writes_no_sit_clock(tmp_path, monkeypatch):
    from abcxauto.park_clock import load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": True})(),
    )
    alarm = set_wake(wake_in_s=3600, session="regular", flat=True)
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_set_wake_floors_tiny_nap(tmp_path, monkeypatch):
    from abcxauto.park_clock import MIN_LOOK_S, _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    alarm = set_wake(wake_in_s=1, session="closed", flat=False)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert remaining >= MIN_LOOK_S - 1


def test_book_move_wakes_on_mark_bucket(monkeypatch):
    from abcxauto.park_clock import book_fingerprint, events_from_diff

    monkeypatch.setenv("ABCXAUTO_MTM_BUCKET_PCT", "8")
    a = book_fingerprint({
        "positions": [{"conId": 1, "avg": 10.0, "mkt": 10.2, "quantity": 1}],
        "ibkr_connected": True,
    })
    b = book_fingerprint({
        "positions": [{"conId": 1, "avg": 10.0, "mkt": 11.0, "quantity": 1}],
        "ibkr_connected": True,
    })
    kinds = {e.kind for e in events_from_diff(a, b)}
    assert "book_move" in kinds


def test_first_snap_is_not_a_flood():
    b = book_fingerprint({
        "fills": [{"symbol": "QQQ", "exec_id": "e1"}],
        "positions": [{"conId": 1}],
        "market_hours": {"session": {"status": "regular"}},
        "ibkr_connected": True,
    })
    assert events_from_diff(None, b) == []


def test_should_wake_grok_is_what_the_engine_asks_now(tmp_path, monkeypatch):
    """The decision surface that used to be tests-only is the live gate.

    Nothing changed on the book and no clock is due, so the answer is None and
    the engine sleeps instead of thinking again.
    """
    from abcxauto.park_clock import GrokAlarm, load_alarm, should_wake_grok

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    quiet = GrokAlarm(wake_at=None, wake_if=[], set_at="")
    assert should_wake_grok([], alarm=quiet) is None
    assert load_alarm().wake_at is None


def test_failed_look_backoff_honors_the_pinned_cadence(monkeypatch):
    """ABCXAUTO_STAY_UP_RETRY_S still floors the failed-look backoff."""
    from abcxauto.park_clock import failed_look_backoff_s

    monkeypatch.setenv("ABCXAUTO_STAY_UP_RETRY_S", "0.25")
    assert failed_look_backoff_s(1) == 0.25


def test_clerk_look_s_open_book_is_short(monkeypatch):
    from abcxauto.park_clock import DEFAULT_LOOK_OPEN_S, clerk_look_s

    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    monkeypatch.setattr("abcxauto.lab_playbook.playbook_next_look_s", lambda: None)
    assert clerk_look_s(flat=False, session="regular") == DEFAULT_LOOK_OPEN_S


def test_clerk_look_s_flat_rth_is_a_hunt(monkeypatch):
    from abcxauto.park_clock import DEFAULT_LOOK_HUNT_S, clerk_look_s

    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    monkeypatch.setattr("abcxauto.lab_playbook.playbook_next_look_s", lambda: None)
    assert clerk_look_s(flat=True, session="regular", next_look_s=None) == DEFAULT_LOOK_HUNT_S


def test_clerk_look_s_honors_card_hint(monkeypatch):
    from abcxauto.park_clock import clerk_look_s

    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    monkeypatch.setattr("abcxauto.lab_playbook.playbook_next_look_s", lambda: None)
    assert clerk_look_s(flat=False, session="regular", next_look_s=120) == 300
    assert clerk_look_s(flat=True, session="regular", next_look_s=300) == 300


def _save_session_card():
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "when_on": "mega/large ≥6% earnings-miss gap, hold above the opening low",
                            "shape": "LONG STK. Stop under opening low.",
                            "next_look_s": 1800,
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)


def test_clerk_look_s_last_hour_to_open(monkeypatch):
    from abcxauto.park_clock import LAST_HOUR_LOOK_S, clerk_look_s

    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    assert clerk_look_s(
        flat=True, session="premarket", minutes_to_open=45
    ) == LAST_HOUR_LOOK_S


def test_clerk_look_s_session_card_does_not_park_until_the_open(monkeypatch):
    """Gap cards cannot sit the think loop until 9:30. Hunt / last-hour cadence."""
    from abcxauto.park_clock import DEFAULT_LOOK_HUNT_S, LAST_HOUR_LOOK_S, clerk_look_s

    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    _save_session_card()
    assert clerk_look_s(
        flat=True,
        session="premarket",
        minutes_to_open=32,
        next_look_s=1800,
    ) == LAST_HOUR_LOOK_S
    assert clerk_look_s(
        flat=True,
        session="premarket",
        minutes_to_open=90,
        next_look_s=1800,
    ) == DEFAULT_LOOK_HUNT_S


def test_clerk_look_s_overnight_closed_still_parks(monkeypatch):
    from abcxauto.park_clock import clerk_look_s

    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    _save_session_card()
    assert clerk_look_s(
        flat=True,
        session="closed",
        minutes_to_open=5 * 60,
        next_look_s=1800,
    ) == (5 * 60 - 60.0) * 60.0


def test_ensure_next_look_premarket_writes_no_sit_clock(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto.park_clock import GrokAlarm, ensure_next_look, load_alarm, save_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    save_alarm(GrokAlarm(wake_at=soon, set_at=soon))
    alarm = ensure_next_look(
        session="premarket",
        flat=True,
        minutes_to_open=5,
    )
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_ensure_next_look_premarket_drops_a_remaining_to_bell_park(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto.park_clock import GrokAlarm, ensure_next_look, load_alarm, save_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    bell = (datetime.now(timezone.utc) + timedelta(minutes=32)).isoformat()
    save_alarm(GrokAlarm(wake_at=bell, set_at=bell))
    alarm = ensure_next_look(
        session="premarket",
        flat=True,
        minutes_to_open=32,
    )
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None


def test_remaining_to_bell_and_start_looks_now(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto.park_clock import (
        GrokAlarm,
        remaining_to_bell_s,
        save_alarm,
        start_looks_now,
    )

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    assert remaining_to_bell_s(32 * 60, 32) is True
    assert remaining_to_bell_s(90, 32) is False
    assert remaining_to_bell_s(30 * 60, 90) is True
    assert remaining_to_bell_s(600, 90) is False

    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    save_alarm(GrokAlarm(wake_at=soon, set_at=soon))
    # Premarket leftover is not an overnight park — Start looks now.
    assert start_looks_now(minutes_to_open=32) is True
    bell = (datetime.now(timezone.utc) + timedelta(minutes=32)).isoformat()
    save_alarm(GrokAlarm(wake_at=bell, set_at=bell))
    assert start_looks_now(minutes_to_open=32) is True
    assert start_looks_now(minutes_to_open=6 * 60) is False


def test_infer_session_before_open_splits_overnight_from_premarket():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from abcxauto.park_clock import infer_session_before_open

    et = ZoneInfo("America/New_York")
    sess, mins = infer_session_before_open(
        now=datetime(2026, 8, 26, 3, 0, tzinfo=et)
    )
    assert sess == "closed"
    assert mins is not None and mins > 5.5 * 60
    sess, mins = infer_session_before_open(
        now=datetime(2026, 8, 26, 8, 32, tzinfo=et)
    )
    assert sess == "premarket"
    assert mins is not None and 50 < mins < 65


def test_begin_run_premarket_writes_no_sit_clock(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts
    from abcxauto.park_clock import load_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "RUN_PATH", tmp_path / "run.json")
    monkeypatch.delenv("ABCXAUTO_DEFAULT_LOOK_S", raising=False)
    monkeypatch.setattr(
        "abcxauto.park_clock.et_minutes_to_rth_open", lambda **_k: 58.0
    )
    _save_session_card()
    ts._run = {}
    ts.begin_run()
    assert load_alarm().wake_at is None


def test_clamp_next_look_s_floors_and_caps():
    from abcxauto.park_clock import MIN_LOOK_S, NEXT_LOOK_S_MAX, clamp_next_look_s

    assert clamp_next_look_s(1) == MIN_LOOK_S
    assert clamp_next_look_s(9 * 3600) == NEXT_LOOK_S_MAX
    assert clamp_next_look_s("nope") is None


def test_ensure_next_look_rth_clears_a_spent_or_standing_clock(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto.park_clock import (
        GrokAlarm,
        ensure_next_look,
        load_alarm,
        save_alarm,
    )

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    save_alarm(GrokAlarm(wake_at=past, set_at=past))
    alarm = ensure_next_look(session="regular", flat=True)
    assert alarm.wake_at is None
    assert load_alarm().wake_at is None
    soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    save_alarm(GrokAlarm(wake_at=soon, set_at=soon))
    kept = ensure_next_look(session="regular", flat=True)
    assert kept.wake_at is None
    replaced = ensure_next_look(session="regular", flat=True, replace=True)
    assert replaced.wake_at is None


def test_paper_stay_up_is_regular_and_premarket(monkeypatch):
    from abcxauto.park_clock import paper_stay_up, session_is_park

    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: True),
    )
    assert paper_stay_up(session="regular") is True
    assert paper_stay_up(session="premarket") is True
    assert paper_stay_up(session="closed") is False
    assert paper_stay_up(session="postmarket") is False
    assert session_is_park("regular") is False
    assert session_is_park("premarket") is False
    assert session_is_park("closed") is True
    assert session_is_park("postmarket") is True
    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: False),
    )
    assert paper_stay_up(session="regular") is False


def test_wake_bus_module_is_gone():
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("abcxauto.wake_bus")


def test_bind_optional_kw_skips_resume_on_older_mocks():
    from abcxauto.brain import bind_optional_kw

    def old_grok_turn(g, *, connector, world, snap, wake=""):
        return (g, connector, world, snap, wake)

    kw = bind_optional_kw(
        old_grok_turn,
        dict(connector="c", world="w", snap="s", wake="hi"),
        resume=True,
    )
    assert "resume" not in kw
    assert old_grok_turn("g", **kw)[4] == "hi"

    def new_grok_turn(g, *, connector, world, snap, wake="", resume=False):
        return resume

    kw2 = bind_optional_kw(
        new_grok_turn,
        dict(connector="c", world="w", snap="s", wake="hi"),
        resume=True,
    )
    assert kw2["resume"] is True
    assert new_grok_turn("g", **kw2) is True
