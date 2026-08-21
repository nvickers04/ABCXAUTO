"""Re-stamp fill rows that were written in the wrong timezone.

Until 2026-08-20 the desk stored ``execution.time`` as ib_insync handed it over.
TWS sends that time as bare ``YYYYmmdd  HH:MM:SS`` digits in UTC, and
ib_insync's decoder calls ``astimezone()`` on the naive value, which reads the
digits as *this machine's* local time. Every affected fill therefore sits in the
journal one local UTC offset in the future — five hours on a US Central desk in
summer. Dispatch rows were never affected: they stamp
``datetime.now(timezone.utc)``, so they are the anchor this script corrects
against.

A fill cannot precede the order that caused it, so a row is only re-stamped when
the shift leaves it at or after its own dispatch. Rows with no dispatch to anchor
against are reported and left alone; so are rows the shift would push before
their order, because those were already right.

Usage:
  python scripts/fix_fill_timestamps.py                 # dry run, prints the plan
  python scripts/fix_fill_timestamps.py --apply         # back up, then re-stamp
  python scripts/fix_fill_timestamps.py --apply --hours 6
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# The journal's own extractor: a bracket result names its legs bracket_order_id /
# stop_order_id / target_order_id, so a plain "order_id" scan misses most sends.
from abcxauto.memory.journal import _order_ids_from_result_json as order_ids  # noqa: E402

DEFAULT_DB = REPO / "journal.db"

# The desk writes the dispatch row after the send returns, so a bracket entry can
# legitimately fill a moment before its own dispatch is recorded.
DISPATCH_GRACE = timedelta(seconds=90)


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_z(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def earliest_dispatch_by_order(conn: sqlite3.Connection) -> dict[int, datetime]:
    anchors: dict[int, datetime] = {}
    rows = conn.execute(
        "SELECT ts, result_json FROM dispatches WHERE result_json IS NOT NULL"
    ).fetchall()
    for ts_raw, blob in rows:
        ts = parse_ts(ts_raw)
        if ts is None:
            continue
        for oid in order_ids(blob):
            if oid not in anchors or ts < anchors[oid]:
                anchors[oid] = ts
    return anchors


def plan(conn: sqlite3.Connection, shift: timedelta) -> tuple[list[dict], list[dict]]:
    """(rows to re-stamp, rows left alone with the reason why)."""
    anchors = earliest_dispatch_by_order(conn)
    fix: list[dict] = []
    keep: list[dict] = []
    rows = conn.execute(
        "SELECT id, ts, order_id, symbol, side FROM fills ORDER BY id"
    ).fetchall()
    for fid, ts_raw, order_id, symbol, side in rows:
        ts = parse_ts(ts_raw)
        entry = {"id": fid, "symbol": symbol, "side": side, "ts": ts_raw}
        if ts is None:
            keep.append({**entry, "reason": "unreadable timestamp"})
            continue
        anchor = anchors.get(order_id) if order_id else None
        if anchor is None:
            keep.append({**entry, "reason": "no dispatch to anchor against"})
            continue
        shifted = ts - shift
        if shifted + DISPATCH_GRACE < anchor:
            keep.append({**entry, "reason": "shift would precede its own order"})
            continue
        fix.append({**entry, "new_ts": iso_z(shifted), "dispatch_ts": iso_z(anchor)})
    return fix, keep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="journal.db path")
    ap.add_argument("--hours", type=float, default=5.0, help="offset the rows were shifted by")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"no journal at {db}")
        return 1
    shift = timedelta(hours=args.hours)

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        fix, keep = plan(conn, shift)

    print(f"journal: {db}")
    print(f"shift  : -{args.hours}h\n")
    for row in fix:
        print(f"  re-stamp #{row['id']:>3} {row['symbol']:<6} {row['side']:<4} "
              f"{row['ts']} -> {row['new_ts']}  (order dispatched {row['dispatch_ts']})")
    for row in keep:
        print(f"  keep     #{row['id']:>3} {row['symbol']:<6} {row['side']:<4} "
              f"{row['ts']}  ({row['reason']})")
    print(f"\n{len(fix)} to re-stamp, {len(keep)} left alone")

    if not fix:
        print("nothing to do")
        return 0
    if not args.apply:
        print("\ndry run - pass --apply to write it")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"{db.name}.bak-{stamp}")
    shutil.copy2(db, backup)
    report = db.with_name(f"{db.name}.fill-ts-fix-{stamp}.json")
    report.write_text(
        json.dumps(
            {"shift_hours": args.hours, "backup": str(backup), "re_stamped": fix, "kept": keep},
            indent=2,
        ),
        encoding="utf-8",
    )

    with sqlite3.connect(str(db)) as conn:
        conn.executemany(
            "UPDATE fills SET ts = ? WHERE id = ?",
            [(row["new_ts"], row["id"]) for row in fix],
        )
        conn.commit()

    print(f"\nbacked up to {backup.name}")
    print(f"audit trail  {report.name}")
    print(f"re-stamped   {len(fix)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
