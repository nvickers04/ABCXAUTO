"""Event push + Grok-set alarm. No clerk decision checklist."""

from datetime import datetime, timedelta, timezone

from abcxauto.wake_bus import (
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
    from abcxauto.wake_bus import ensure_next_look, load_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_DEFAULT_LOOK_S", "30")
    # Overnight silent still seeds a look. Paper premarket stays up (no sit clock).
    alarm = ensure_next_look(previous_set_at="", session="closed")
    assert alarm.wake_at
    assert load_alarm().wake_at == alarm.wake_at


def test_ensure_next_look_rth_silent_gets_a_backstop_not_a_spin(tmp_path, monkeypatch):
    """A silent RTH turn must leave a clock behind.

    With no clock the alarm stays past-due, ``due()`` is instantly true, and the
    engine thinks again with no gap. The backstop is what makes "Grok owns the
    cadence" safe when Grok declines to choose one.
    """
    from abcxauto.wake_bus import ensure_next_look, load_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_DEFAULT_LOOK_S", "30")
    alarm = ensure_next_look(
        previous_set_at="",
        flat=False,
        session="regular",
    )
    assert alarm.wake_at
    assert load_alarm().wake_at == alarm.wake_at


def test_ensure_next_look_keeps_grok_alarm(tmp_path, monkeypatch):
    from abcxauto.wake_bus import ensure_next_look, load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    set_wake(wake_in_s=120, wake_if=["fill"], session="closed")
    before = load_alarm()
    out = ensure_next_look(previous_set_at="", session="closed")
    assert out.wake_if == ["fill"]
    assert out.set_at == before.set_at
    assert out.wake_at


def test_set_wake_still_parks_overnight(tmp_path, monkeypatch):
    """set_wake park outside RTH is a real shutdown clock (new think next session)."""
    from abcxauto.wake_bus import _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    alarm = set_wake(wake_in_s=8 * 3600, session="closed", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert 8 * 3600 - 30 <= remaining <= 8 * 3600 + 30


def test_live_interrupt_note_and_take():
    from abcxauto.wake_bus import (
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


def test_paper_rth_set_wake_writes_a_real_clock(tmp_path, monkeypatch):
    """Grok owns the cadence in every session.

    Withholding the clock in paper RTH left the engine re-arming a think the
    instant the last one ended — a hunt treadmill for a setup that only appears
    in the opening window.
    """
    from abcxauto.wake_bus import _parse_iso, _utc_now, load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    # A running lab, so the idle-nap cap is not what is under test here.
    monkeypatch.setattr(
        "abcxauto.lab_playbook.lab_facts",
        lambda *_a, **_k: {"resolved_trades": 4, "entry_trunks_untried": []},
    )
    alarm = set_wake(wake_in_s=600, session="regular", flat=True, wake_if=["fill"])
    assert alarm.wake_at
    assert load_alarm().wake_at == alarm.wake_at
    assert alarm.wake_if == ["fill"]
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    assert 540 <= (at - _utc_now()).total_seconds() <= 660


def test_paper_premarket_set_wake_writes_a_real_clock(tmp_path, monkeypatch):
    from abcxauto.wake_bus import load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    alarm = set_wake(wake_in_s=3600, session="premarket", flat=True)
    assert alarm.wake_at
    assert load_alarm().wake_at == alarm.wake_at


def test_an_idle_lab_cannot_be_napped_through(tmp_path, monkeypatch):
    """A park is Grok's clock, not a way to skip building the book."""
    from abcxauto.wake_bus import (
        LAB_IDLE_PARK_CAP_S,
        _parse_iso,
        _utc_now,
        lab_idle_park_cap_s,
        set_wake,
    )

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    idle = {"resolved_trades": 0, "entry_trunks_untried": ["vertical_spread"]}
    monkeypatch.setattr("abcxauto.lab_playbook.lab_facts", lambda *_a, **_k: idle)

    assert lab_idle_park_cap_s(flat=True, session="regular") == LAB_IDLE_PARK_CAP_S
    alarm = set_wake(wake_in_s=4 * 3600, session="regular", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    assert (at - _utc_now()).total_seconds() <= LAB_IDLE_PARK_CAP_S + 5

    # A long nap is the right answer when there is no lab work to do: holding
    # risk, or outside tradeable hours.
    assert lab_idle_park_cap_s(flat=False, session="regular") is None
    assert lab_idle_park_cap_s(flat=True, session="closed") is None
    assert lab_idle_park_cap_s(flat=True, session="postmarket") is None


def test_a_running_lab_keeps_its_full_clock(tmp_path, monkeypatch):
    """One resolved trade, or a card under every entry structure, lifts the cap."""
    from abcxauto.wake_bus import _parse_iso, _utc_now, lab_idle_park_cap_s, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    monkeypatch.setattr(
        "abcxauto.lab_playbook.lab_facts",
        lambda *_a, **_k: {"resolved_trades": 3, "entry_trunks_untried": ["butterfly"]},
    )
    assert lab_idle_park_cap_s(flat=True, session="regular") is None
    alarm = set_wake(wake_in_s=3600, session="regular", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    assert (at - _utc_now()).total_seconds() > 1800

    monkeypatch.setattr(
        "abcxauto.lab_playbook.lab_facts",
        lambda *_a, **_k: {"resolved_trades": 0, "entry_trunks_untried": []},
    )
    assert lab_idle_park_cap_s(flat=True, session="regular") is None


def test_set_wake_offered_in_every_session():
    from abcxauto.wake_bus import set_wake_offered

    for sess in ("regular", "premarket", "postmarket", "closed", ""):
        assert set_wake_offered(session=sess) is True, sess


def test_set_wake_live_premarket_allows_long_park(tmp_path, monkeypatch):
    from abcxauto.wake_bus import _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    alarm = set_wake(wake_in_s=3600, session="premarket", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert 3590 <= remaining <= 3610


def test_set_wake_live_honors_long_nap(tmp_path, monkeypatch):
    from abcxauto.wake_bus import _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    alarm = set_wake(wake_in_s=3600, session="regular", flat=False)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert 3590 <= remaining <= 3610


def test_set_wake_paper_halted_honors_long_nap(tmp_path, monkeypatch):
    from abcxauto.wake_bus import _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": True})(),
    )
    alarm = set_wake(wake_in_s=3600, session="regular", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert 3590 <= remaining <= 3610


def test_set_wake_floors_tiny_nap(tmp_path, monkeypatch):
    from abcxauto.wake_bus import MIN_LOOK_S, _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    alarm = set_wake(wake_in_s=1, session="closed", flat=False)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert remaining >= MIN_LOOK_S - 1


def test_book_move_wakes_on_mark_bucket(monkeypatch):
    from abcxauto.wake_bus import book_fingerprint, events_from_diff

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
    from abcxauto.wake_bus import GrokAlarm, load_alarm, should_wake_grok

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    quiet = GrokAlarm(wake_at=None, wake_if=[], set_at="")
    assert should_wake_grok([], alarm=quiet) is None
    assert load_alarm().wake_at is None


def test_failed_look_backoff_honors_the_pinned_cadence(monkeypatch):
    """ABCXAUTO_STAY_UP_RETRY_S still floors the failed-look backoff."""
    from abcxauto.wake_bus import failed_look_backoff_s

    monkeypatch.setenv("ABCXAUTO_STAY_UP_RETRY_S", "0.25")
    assert failed_look_backoff_s(1) == 0.25
