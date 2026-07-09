"""Two-round simplification search — lean development-mode heart.

Runs after the order lab each cycle. Does NOT randomly delete trading code.
Safe, audited actions only:
  Round 1 — prune dead runtime state (stale TWEAKS keys, oversized logs)
  Round 2 — structural candidates report + drop empty/no-op tweak residue

Every action is logged for the Logs & Evolution page. PnL remains the truth signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SIMPLIFY_LOG = Path("simplify.log")

# TWEAK keys that are live knobs; anything else is candidate dead weight.
KNOWN_TWEAK_KEYS = frozenset(
    {
        "cycle_sleep_s",
        "max_risk_pct",
        "prefer_bracket_only",
        "lab_min_pass_rate",
        "lab_failed_strategies",
        "require_target_conId",
        "hold_on_inventory_reject",
        "drawdown_slowdown",
    }
)

# Max lines kept in rolling lab/rocket logs (disk lean)
_LOG_TRIM = {
    "rocket.log": 400,
    "order_lab.log": 200,
    "improvements.log": 200,
    "simplify.log": 200,
}


def _append(path: Path, row: dict) -> None:
    try:
        path.open("a", encoding="utf-8").write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _trim_log(name: str, max_lines: int) -> dict | None:
    p = REPO / name
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= max_lines:
            return None
        kept = lines[-max_lines:]
        p.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return {
            "action": "trim_log",
            "file": name,
            "removed_lines": len(lines) - len(kept),
            "kept": len(kept),
        }
    except OSError as e:
        return {"action": "trim_log_fail", "file": name, "detail": str(e)}


def pass_one_runtime_prune() -> list[dict]:
    """Round 1: prune dead TWEAKS + trim rolling logs."""
    from abcxauto.rocket import TWEAKS

    actions: list[dict] = []
    dead = [k for k in list(TWEAKS.keys()) if k not in KNOWN_TWEAK_KEYS]
    for k in dead:
        TWEAKS.pop(k, None)
        actions.append({"action": "drop_dead_tweak", "key": k})
    # Empty list failures
    fails = TWEAKS.get("lab_failed_strategies")
    if isinstance(fails, list) and not fails:
        TWEAKS.pop("lab_failed_strategies", None)
        actions.append({"action": "drop_empty_lab_failed_strategies"})
    for name, n in _LOG_TRIM.items():
        row = _trim_log(name, n)
        if row:
            actions.append(row)
    if not actions:
        actions.append({"action": "round1_noop", "detail": "runtime already lean"})
    return actions


def pass_two_structure_scan(lab: dict | None = None) -> list[dict]:
    """Round 2: report bloat candidates + clear no-op reconfig residue.

    Does not delete source files at runtime (unsafe for live trading process).
    Surfaces explicit simplification_count for the audit trail.
    """
    actions: list[dict] = []
    # Drop transient lab noise keys that shouldn't stick in TWEAKS
    from abcxauto.rocket import TWEAKS

    if TWEAKS.get("prefer_bracket_only") is False:
        TWEAKS.pop("prefer_bracket_only", None)
        actions.append({"action": "inline_clear", "key": "prefer_bracket_only=false"})
    # If lab is perfect, clear failed strategy list (no longer needed)
    rate = float((lab or {}).get("pass_rate") or 0)
    if rate >= 0.99 and "lab_failed_strategies" in TWEAKS:
        TWEAKS.pop("lab_failed_strategies", None)
        actions.append({"action": "clear_lab_fails_after_green", "pass_rate": rate})
    # Count oversized modules as candidates (report only)
    heavy = []
    try:
        for p in (REPO / "abcxauto").rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                n = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if n > 900:
                heavy.append({"path": str(p.relative_to(REPO)), "lines": n})
    except OSError:
        pass
    if heavy:
        actions.append(
            {
                "action": "structure_candidate",
                "detail": "files >900 lines — consider split/inline at next dev pass",
                "files": heavy[:8],
            }
        )
    if not any(a.get("action") != "structure_candidate" for a in actions):
        if not actions:
            actions.append({"action": "round2_noop", "detail": "no structural waste found"})
    return actions


def run_two_simplification_passes(lab: dict | None = None) -> dict:
    """Execute two simplification rounds; return audit payload for Logs page."""
    r1 = pass_one_runtime_prune()
    r2 = pass_two_structure_scan(lab)
    deleted = sum(
        1
        for a in r1 + r2
        if str(a.get("action", "")).startswith(("drop_", "trim_", "clear_", "inline_"))
    )
    report = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "round1": r1,
        "round2": r2,
        "simplification_count": deleted,
        "summary": (
            f"simplify R1={len(r1)} acts R2={len(r2)} acts "
            f"deleted_or_trimmed={deleted}"
        ),
    }
    _append(SIMPLIFY_LOG, report)
    return report
