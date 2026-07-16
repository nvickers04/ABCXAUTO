"""Quick Phase 5 pulse: judgments, decisions, structure, fills."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from abcxauto.config import get_config
from abcxauto.memory import get_journal
from abcxauto.structure_grade import recent_structure_lessons
from abcxauto.trade_plan import load_trade_plan
from abcxauto.world_state import load_idle_streak


def main() -> None:
    cfg = get_config()
    j = get_journal()
    print(
        f"posture={getattr(cfg, 'risk_posture', None)} "
        f"mode={getattr(cfg, 'trading_mode', None)}"
    )
    print(f"thesis: {(j.get_working_thesis() or '')[:180]}")
    print(f"idle={load_idle_streak()}")
    plan = load_trade_plan()
    print(f"plan={plan.symbol if plan else None} {plan.direction if plan else ''}".strip())
    print("--- JUDGE (6) ---")
    for x in j.recent_judgments(6):
        intent = x.get("intent") or {}
        print(
            f"{x.get('ts')} c{x.get('cycle')} {x.get('stance')} "
            f"{intent.get('kind')}/{intent.get('symbol')}/{intent.get('direction')}"
        )
        foc = (x.get("focus") or "")[:90]
        if foc:
            print(f"  focus: {foc}")
    print("--- ACT (8) ---")
    for d in j.recent_decisions(8):
        o = d.get("outcome") or {}
        if isinstance(o, dict):
            if o.get("success") is True:
                st = f"ok fill={o.get('filled')} entry={o.get('entry_price')}"
            else:
                st = o.get("status") or o.get("note") or o.get("error") or "?"
        else:
            st = o
        print(f"{d.get('ts')} c{d.get('cycle')} {d.get('strategy')} -> {st}")
        rat = (d.get("rationale") or "")[:110]
        if rat:
            print(f"  {rat}")
    print("--- STRUCTURE (6) ---")
    for e in recent_structure_lessons(6):
        print(
            f"{e.get('ts')} {e.get('symbol')} {e.get('outcome')}/{e.get('reason_code')} "
            f"{(e.get('message') or '')[:90]}"
        )
    db = Path(__file__).resolve().parents[1] / "journal.db"
    if db.is_file():
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        print("--- FILLS (6) ---")
        for r in conn.execute(
            "SELECT ts,symbol,side,quantity,price FROM fills ORDER BY id DESC LIMIT 6"
        ):
            print(dict(r))
        print("--- DISPATCH (4) ---")
        for r in conn.execute(
            "SELECT ts,ok,substr(result_json,1,160) AS r FROM dispatches ORDER BY id DESC LIMIT 4"
        ):
            print(f"{r['ts']} ok={r['ok']} {r['r']}")
        conn.close()


if __name__ == "__main__":
    main()
