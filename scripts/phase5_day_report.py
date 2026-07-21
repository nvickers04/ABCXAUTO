"""Phase 5 daily / weekly KPI report for TRAINING_LOG debriefs.

Usage:
  python scripts/phase5_day_report.py
  python scripts/phase5_day_report.py --week
  python scripts/phase5_day_report.py --day 2026-07-16
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

from abcxauto.config import get_config
from abcxauto.memory import get_journal
from abcxauto.phase5_kpis import (
    build_day_report,
    build_week_report,
    format_day_report,
    format_week_report,
)
from abcxauto.trade_plan import format_open_risk_line, load_trade_plan


def main() -> None:
    ap = argparse.ArgumentParser(description="ABCXAUTO Phase 5 day/week report")
    ap.add_argument("--week", action="store_true", help="7-day rollup ending today UTC")
    ap.add_argument("--day", type=str, default="", help="UTC date YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=7, help="Window length for --week")
    args = ap.parse_args()

    cfg = get_config()
    journal = get_journal()
    print(
        f"posture={getattr(cfg, 'risk_posture', '') or '(none)'} "
        f"mode={getattr(cfg, 'trading_mode', '')} "
        f"gates={getattr(cfg, 'risk_gates_enabled', None)}"
    )
    plan = load_trade_plan()
    if plan:
        print(format_open_risk_line(plan))
    else:
        print("open_risk: (flat / no plan)")

    if args.week:
        end = date.fromisoformat(args.day) if args.day else datetime.now(timezone.utc).date()
        rep = build_week_report(journal=journal, end=end, days=args.days)
        print(format_week_report(rep))
    else:
        day = (
            date.fromisoformat(args.day)
            if args.day
            else datetime.now(timezone.utc).date()
        )
        rep = build_day_report(journal=journal, day=day)
        print(format_day_report(rep))


if __name__ == "__main__":
    main()
