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
    alarm = ensure_next_look(previous_set_at="")
    assert alarm.wake_at
    assert load_alarm().wake_at == alarm.wake_at


def test_ensure_next_look_keeps_grok_alarm(tmp_path, monkeypatch):
    from abcxauto.wake_bus import ensure_next_look, load_alarm, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    set_wake(wake_in_s=120, wake_if=["fill"])
    before = load_alarm()
    out = ensure_next_look(previous_set_at="")
    assert out.wake_if == ["fill"]
    assert out.set_at == before.set_at
    assert out.wake_at


def test_set_wake_always_has_a_clock(tmp_path, monkeypatch):
    from abcxauto.wake_bus import set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    alarm = set_wake(wake_if=["fill"], session="regular", flat=False)
    assert alarm.wake_at
    assert alarm.wake_if == ["fill"]


def test_set_wake_paper_rth_caps_long_nap(tmp_path, monkeypatch):
    from abcxauto.wake_bus import PAPER_MAX_LOOK_S, _parse_iso, _utc_now, set_wake

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": False})(),
    )
    alarm = set_wake(wake_in_s=150 * 60, session="regular", flat=True)
    at = _parse_iso(alarm.wake_at or "")
    assert at is not None
    remaining = (at - _utc_now()).total_seconds()
    assert PAPER_MAX_LOOK_S - 10 <= remaining <= PAPER_MAX_LOOK_S + 10


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
    alarm = set_wake(wake_in_s=1, session="regular", flat=False)
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
