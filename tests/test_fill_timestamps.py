"""A fill is stamped with the instant it happened, in UTC.

2026-08-20: a WMT bracket entry dispatched at 15:42:07Z had its fill written as
20:42:06Z — five hours after the order that caused it. TWS sends execution time
as bare ``YYYYmmdd  HH:MM:SS`` digits in UTC; ib_insync's decoder calls
``astimezone()`` on that naive value, which reads the digits as *this machine's*
local time, so every fill moved by the local UTC offset. Dispatch and
model_usage rows were right because they stamp ``datetime.now(timezone.utc)``
and never touch local time.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from abcxauto.broker.connector import IBKRConnector, fill_ts_iso, new_ib, tws_timezone
from abcxauto.memory.journal import TradeJournal

# What this desk's clock reads in summer (US Central, DST).
CDT = timezone(timedelta(hours=-5))
EDT = timezone(timedelta(hours=-4))
# Pin "now" so these read the same at any hour of any day.
LATER = datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc)


def ib_insync_stamp(true_utc: datetime, local_tz) -> datetime:
    """The value ib_insync hands us for a bare TWS execution time.

    ``naive.astimezone(utc)`` is ``naive.replace(tzinfo=local).astimezone(utc)``,
    so TWS's UTC digits come back shifted by the local offset.
    """
    return true_utc.replace(tzinfo=None).replace(tzinfo=local_tz).astimezone(timezone.utc)


# ---------------------------------------------------------------- the stamp


def test_the_wmt_fill_is_not_five_hours_after_its_own_order():
    """Replay of 2026-08-20: dispatch 15:42:07Z, fill row read 20:42:06Z."""
    true_utc = datetime(2026, 8, 20, 15, 42, 6, tzinfo=timezone.utc)
    mangled = ib_insync_stamp(true_utc, CDT)
    # The skew is the local offset, not a fixed EST constant.
    assert mangled == datetime(2026, 8, 20, 20, 42, 6, tzinfo=timezone.utc)

    ts = fill_ts_iso(
        mangled,
        now=datetime(2026, 8, 20, 15, 42, 20, tzinfo=timezone.utc),
        local_tz=CDT,
    )
    assert ts == "2026-08-20T15:42:06.000Z"


def test_the_skew_tracks_the_local_offset_not_a_constant():
    """Same digits, a zone an hour over: the correction follows the zone."""
    true_utc = datetime(2026, 8, 20, 15, 42, 6, tzinfo=timezone.utc)
    now = datetime(2026, 8, 20, 15, 42, 20, tzinfo=timezone.utc)
    for tz in (CDT, EDT, timezone(timedelta(hours=-8))):
        assert (
            fill_ts_iso(ib_insync_stamp(true_utc, tz), now=now, local_tz=tz)
            == "2026-08-20T15:42:06.000Z"
        )


def test_bare_tws_digits_are_labelled_utc_not_shifted():
    assert fill_ts_iso(datetime(2026, 8, 20, 15, 42, 6), now=LATER) == "2026-08-20T15:42:06.000Z"


def test_an_offset_bearing_stamp_is_converted():
    assert (
        fill_ts_iso(datetime(2026, 8, 20, 11, 42, 6, tzinfo=EDT), now=LATER)
        == "2026-08-20T15:42:06.000Z"
    )


def test_a_stamp_already_in_the_past_is_left_alone():
    """The guard must not touch a fill that is merely old."""
    ts = fill_ts_iso(
        datetime(2026, 8, 20, 15, 42, 6, tzinfo=timezone.utc),
        now=datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc),
        local_tz=CDT,
    )
    assert ts == "2026-08-20T15:42:06.000Z"


def test_a_broker_clock_a_little_ahead_is_not_rewritten():
    """Ordinary clock skew is not a zone bug — leave the stamp as sent."""
    ts = fill_ts_iso(
        datetime(2026, 8, 20, 15, 42, 6, tzinfo=timezone.utc),
        now=datetime(2026, 8, 20, 15, 41, 30, tzinfo=timezone.utc),
        local_tz=CDT,
    )
    assert ts == "2026-08-20T15:42:06.000Z"


def test_a_missing_execution_time_falls_back_to_now():
    now = datetime(2026, 8, 20, 15, 42, 20, tzinfo=timezone.utc)
    assert fill_ts_iso(None, now=now) == "2026-08-20T15:42:20.000Z"


def test_an_unreadable_execution_time_falls_back_to_now():
    now = datetime(2026, 8, 20, 15, 42, 20, tzinfo=timezone.utc)
    assert fill_ts_iso("not a time", now=now) == "2026-08-20T15:42:20.000Z"


def test_a_string_execution_time_is_normalised():
    assert fill_ts_iso("2026-08-20T15:42:06Z", now=LATER) == "2026-08-20T15:42:06.000Z"


# ---------------------------------------------------------------- the session


def test_the_ib_session_names_the_zone_tws_stamps_in(monkeypatch):
    """Unset TimezoneTWS is what makes ib_insync guess with the local zone."""
    monkeypatch.delenv("ABCXAUTO_TWS_TIMEZONE", raising=False)
    assert tws_timezone() == "UTC"
    assert new_ib().TimezoneTWS == "UTC"


def test_the_tws_zone_is_operator_overridable(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TWS_TIMEZONE", "US/Eastern")
    assert tws_timezone() == "US/Eastern"
    assert new_ib().TimezoneTWS == "US/Eastern"


# ---------------------------------------------------------------- the reads


class _OfflineConnector:
    """The real read bodies over a stub session — never opens a socket.

    ``IBKRConnector`` is a singleton, so borrow the methods instead of
    subclassing; both only reach for ``self.ib.fills()``.
    """

    get_fills = IBKRConnector.get_fills
    get_recent_executions = IBKRConnector.get_recent_executions

    def __init__(self, fills):
        self.ib = SimpleNamespace(fills=lambda: list(fills))

    async def _ensure_connected(self):
        return True


def _fake_fill(exec_time, *, symbol="WMT", order_id=4443, sec_type="STK"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            time=exec_time,
            execId="00025b47.6a86a86b.01.01",
            orderId=order_id,
            side="BOT",
            shares=70.0,
            price=103.07,
            avgPrice=103.07,
        ),
        contract=SimpleNamespace(symbol=symbol, secType=sec_type, conId=1),
        commissionReport=SimpleNamespace(commission=1.0, realizedPNL=0.0),
    )


RAW_TWS_TIME = "20260820  15:42:06"  # what TWS sent for the WMT entry
TRUE_INSTANT = datetime(2026, 8, 20, 15, 42, 6, tzinfo=timezone.utc)


def decoded_exec_time(raw: str, tws_zone: str) -> datetime:
    """``ib_insync/decoder.py`` execDetails time handling, verbatim.

    With no zone named it falls through to ``astimezone()`` on a naive value,
    which is the whole bug; naming the zone is the fix this pins.
    """
    from zoneinfo import ZoneInfo

    from ib_insync.util import parseIBDatetime

    t = parseIBDatetime(raw)
    if not t.tzinfo and tws_zone:
        t = t.replace(tzinfo=ZoneInfo(str(tws_zone)))
    return t.astimezone(timezone.utc)


def test_naming_the_zone_is_what_stops_the_decoder_guessing():
    """TWS sends bare digits, so an unnamed zone gets read as local time."""
    from ib_insync.util import parseIBDatetime

    assert parseIBDatetime(RAW_TWS_TIME).tzinfo is None
    assert decoded_exec_time(RAW_TWS_TIME, "UTC") == TRUE_INSTANT
    local = datetime.now().astimezone().tzinfo
    assert decoded_exec_time(RAW_TWS_TIME, "") == ib_insync_stamp(TRUE_INSTANT, local)


@pytest.mark.asyncio
async def test_get_fills_hands_the_journal_the_instant_tws_meant():
    conn = _OfflineConnector([_fake_fill(decoded_exec_time(RAW_TWS_TIME, "UTC"))])
    rows = await conn.get_fills()
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-08-20T15:42:06.000Z"


@pytest.mark.asyncio
async def test_recent_executions_stamp_matches_the_journal_stamp():
    """detect_scrape_from_fills parses these — both reads must agree."""
    fills = [_fake_fill(decoded_exec_time(RAW_TWS_TIME, "UTC"))]
    journal_rows = await _OfflineConnector(fills).get_fills()
    exec_rows = await _OfflineConnector(fills).get_recent_executions()
    assert exec_rows[0]["ts"] == journal_rows[0]["ts"] == "2026-08-20T15:42:06.000Z"
    assert exec_rows[0]["ts"] == exec_rows[0]["time"]


@pytest.mark.asyncio
async def test_a_fill_is_never_written_after_the_order_that_caused_it(tmp_path):
    """The forensics the operator actually runs: line a fill up with its ticket."""
    journal = TradeJournal(path=str(tmp_path / "j.db"), enabled=True)
    dispatch_ts = "2026-08-20T15:42:07.119Z"
    pid = journal.record_proposal(strategy="market_bracket", symbol="WMT", ts=dispatch_ts)
    journal.record_dispatch(pid, True, {"success": True, "order_id": 4443}, ts=dispatch_ts)

    conn = _OfflineConnector([_fake_fill(decoded_exec_time(RAW_TWS_TIME, "UTC"))])
    assert journal.record_fills(await conn.get_fills()) == 1

    with sqlite3.connect(str(tmp_path / "j.db")) as db:
        stored = db.execute("SELECT ts FROM fills WHERE order_id = 4443").fetchone()[0]
    # A bracket entry fills before the desk writes the dispatch row.
    assert stored <= dispatch_ts


# ---------------------------------------------------------------- the journal


def test_journal_canonicalises_an_offset_bearing_fill_stamp(tmp_path):
    journal = TradeJournal(path=str(tmp_path / "j.db"), enabled=True)
    journal.record_fills(
        [
            {"exec_id": "a", "ts": "2026-08-20T11:42:06-04:00", "symbol": "WMT"},
            {"exec_id": "b", "ts": "2026-08-20T15:42:06+00:00", "symbol": "WMT"},
            {"exec_id": "c", "ts": "2026-08-20T15:42:06", "symbol": "WMT"},
        ]
    )
    with sqlite3.connect(str(tmp_path / "j.db")) as db:
        rows = dict(db.execute("SELECT exec_id, ts FROM fills").fetchall())
    assert rows == {
        "a": "2026-08-20T15:42:06.000Z",
        "b": "2026-08-20T15:42:06.000Z",
        "c": "2026-08-20T15:42:06.000Z",
    }


def test_journal_keeps_an_unreadable_stamp_rather_than_dropping_it(tmp_path):
    journal = TradeJournal(path=str(tmp_path / "j.db"), enabled=True)
    journal.record_fills([{"exec_id": "z", "ts": "whenever", "symbol": "WMT"}])
    with sqlite3.connect(str(tmp_path / "j.db")) as db:
        assert db.execute("SELECT ts FROM fills WHERE exec_id='z'").fetchone()[0] == "whenever"


def test_journal_still_stamps_a_fill_that_arrives_without_a_time(tmp_path):
    journal = TradeJournal(path=str(tmp_path / "j.db"), enabled=True)
    journal.record_fills([{"exec_id": "n", "symbol": "WMT"}])
    with sqlite3.connect(str(tmp_path / "j.db")) as db:
        ts = db.execute("SELECT ts FROM fills WHERE exec_id='n'").fetchone()[0]
    assert ts.endswith("Z")
    assert ts <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------- re-stamping old rows


def _journal_with_history(path):
    """One skewed bracket, one fill that was already right, one with no anchor."""
    journal = TradeJournal(path=str(path), enabled=True)
    pid = journal.record_proposal(
        strategy="market_bracket", symbol="WMT", ts="2026-08-20T15:42:07.119Z"
    )
    journal.record_dispatch(
        pid,
        True,
        {"success": True, "bracket_order_id": 4443, "stop_order_id": 4444},
        ts="2026-08-20T15:42:07.119Z",
    )
    pid2 = journal.record_proposal(
        strategy="market_bracket", symbol="IWM", ts="2026-08-17T18:31:02.379Z"
    )
    journal.record_dispatch(
        pid2, True, {"success": True, "order_id": 3700}, ts="2026-08-17T18:31:02.379Z"
    )
    journal.record_fills(
        [
            # Skewed: five hours after the order that caused it.
            {"exec_id": "skewed", "ts": "2026-08-20T20:42:06Z", "order_id": 4443,
             "symbol": "WMT", "side": "BOT"},
            # Already right: a working order that filled four minutes later.
            {"exec_id": "already-right", "ts": "2026-08-17T18:35:41Z", "order_id": 3700,
             "symbol": "IWM", "side": "SLD"},
            # Manual TWS order — nothing to anchor against.
            {"exec_id": "manual", "ts": "2026-08-20T17:57:55Z", "order_id": 0,
             "symbol": "WMT", "side": "SLD"},
        ]
    )
    # Ingest now aligns a +5h fill to its dispatch. The restamp script is for
    # rows written before that — put the skew back on disk.
    with sqlite3.connect(str(path)) as db:
        db.execute(
            "UPDATE fills SET ts=? WHERE exec_id=?",
            ("2026-08-20T20:42:06.000Z", "skewed"),
        )
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return journal


def _stamps(path):
    with sqlite3.connect(str(path)) as db:
        return dict(db.execute("SELECT exec_id, ts FROM fills").fetchall())


def test_restamp_moves_only_what_the_dispatch_rows_prove(tmp_path):
    from scripts.fix_fill_timestamps import main

    db = tmp_path / "j.db"
    _journal_with_history(db)
    assert main(["--db", str(db), "--apply"]) == 0

    stamps = _stamps(db)
    assert stamps["skewed"] == "2026-08-20T15:42:06.000Z"
    # A shift here would put the fill before its own order, so it was already right.
    assert stamps["already-right"] == "2026-08-17T18:35:41.000Z"
    assert stamps["manual"] == "2026-08-20T17:57:55.000Z"


def test_restamp_dry_run_touches_nothing(tmp_path):
    from scripts.fix_fill_timestamps import main

    db = tmp_path / "j.db"
    _journal_with_history(db)
    before = _stamps(db)
    assert main(["--db", str(db)]) == 0
    assert _stamps(db) == before
    assert list(tmp_path.glob("*.bak-*")) == []


def test_restamp_backs_the_journal_up_before_writing(tmp_path):
    from scripts.fix_fill_timestamps import main

    db = tmp_path / "j.db"
    _journal_with_history(db)
    before = _stamps(db)
    main(["--db", str(db), "--apply"])

    backups = list(tmp_path.glob("j.db.bak-*"))
    assert len(backups) == 1
    assert _stamps(backups[0]) == before
    assert len(list(tmp_path.glob("j.db.fill-ts-fix-*.json"))) == 1


def test_restamp_run_twice_does_not_shift_twice(tmp_path):
    """A corrected row would land before its own order, so it is left alone."""
    from scripts.fix_fill_timestamps import main

    db = tmp_path / "j.db"
    _journal_with_history(db)
    main(["--db", str(db), "--apply"])
    once = _stamps(db)
    main(["--db", str(db), "--apply"])
    assert _stamps(db) == once
