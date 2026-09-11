"""Freeze-then-score window: locked constitution vs cash zero plus model cost.

Jane Street analog: lock the book, then grade N RTH sessions that have not
happened yet. Promote is this verdict. Inception beating_model stays display.
This module does not launch the desk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from abcxauto.config import _REPO_ROOT, _risk_settings_path, get_config

_CONFIG_KEYS = (
    "model",
    "model_rth",
    "model_research",
    "defined_risk_only",
    "cash_only",
    "sizing_floors",
    "daily_loss_limit_pct",
    "max_position_pct",
    "max_risk_per_trade_pct",
    "max_symbol_concentration_pct",
    "max_arena_concentration_pct",
    "max_open_positions",
    "session_look_cap",
    "session_token_cap",
)
_AGENT_STATE_KEYS = ("size_pct_nl", "scan_fetch_cap")
_UNIVERSE_KEYS = ("enabled_arenas", "custom_symbols", "exclude_symbols")
_CASH_BASELINE_PNL = 0.0


def _iso_z(dt: datetime | None = None) -> str:
    clock = dt or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return (
        clock.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _agent_state_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_AGENT_STATE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _REPO_ROOT / "agent_state.json"


def _read_json_file(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw


def constitution_payload() -> dict[str, Any]:
    """Dict that will be hashed. Chat is dropped overnight — use the constant."""
    from abcxauto.llm import SYSTEM_PROMPT
    from abcxauto.universe import load_allowlist

    risk_raw = _read_json_file(_risk_settings_path())
    risk_settings = risk_raw if isinstance(risk_raw, dict) else None

    agent_raw = _read_json_file(_agent_state_path())
    agent_state: dict[str, Any] = {}
    if isinstance(agent_raw, dict):
        for key in _AGENT_STATE_KEYS:
            if key in agent_raw:
                agent_state[key] = agent_raw[key]

    allow = load_allowlist()
    universe = {key: list(allow.get(key) or []) for key in _UNIVERSE_KEYS}

    cfg = get_config()
    out: dict[str, Any] = {
        "system_prompt": SYSTEM_PROMPT,
        "risk_settings": risk_settings,
        "agent_state": agent_state,
        "universe": universe,
    }
    for key in _CONFIG_KEYS:
        out[key] = getattr(cfg, key, None)
    return out


def constitution_hash(payload: dict[str, Any] | None = None) -> str:
    blob = payload if payload is not None else constitution_payload()
    digest = hashlib.sha256(_canonical_json(blob).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def freeze_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_FREEZE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _REPO_ROOT / "data" / "state" / "freeze.json"


def load_freeze() -> dict[str, Any] | None:
    raw = _read_json_file(freeze_path())
    return raw if isinstance(raw, dict) else None


def freeze_armed() -> bool:
    state = load_freeze()
    return bool(state) and str(state.get("status") or "") == "armed"


def _write_freeze(state: dict[str, Any]) -> Path:
    path = freeze_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def assert_unchanged() -> tuple[bool, list[str]]:
    """Rehash against the stored constitution_hash. Drifted top-level keys."""
    state = load_freeze()
    if not state:
        return True, []
    current = constitution_payload()
    stored_hash = str(state.get("constitution_hash") or "")
    ok = constitution_hash(current) == stored_hash
    stored_payload = state.get("hash_payload")
    if not isinstance(stored_payload, dict):
        stored_payload = {}
    drifted = [
        key
        for key in sorted(set(current) | set(stored_payload))
        if current.get(key) != stored_payload.get(key)
    ]
    return ok, drifted


def _resolve_start_nl(journal: Any, start_nl: float | None) -> float | None:
    if journal is not None:
        try:
            from abcxauto.scorecard import rth_session_start

            _bell, session_date = rth_session_start()
            fn = getattr(journal, "session_start_marker", None)
            if callable(fn):
                marker = fn(session_date)
                if isinstance(marker, dict) and marker.get("net_liquidation") is not None:
                    cand = float(marker["net_liquidation"])
                    if cand > 0:
                        return cand
        except Exception:
            pass
        try:
            fn = getattr(journal, "first_snapshot", None)
            if callable(fn):
                nl, _ts = fn()
                if nl is not None:
                    cand = float(nl)
                    if cand > 0:
                        return cand
        except Exception:
            pass
    if start_nl is None:
        return None
    try:
        cand = float(start_nl)
    except (TypeError, ValueError):
        return None
    return cand if cand > 0 else None


def _journal(journal: Any = None) -> Any:
    if journal is not None:
        return journal
    try:
        from abcxauto.memory import get_journal

        return get_journal()
    except Exception:
        return None


def open_freeze(
    n_sessions: int = 20,
    *,
    unseen_required: bool = True,
    start_nl: float | None = None,
    freeze_id: str | None = None,
    journal: Any = None,
) -> dict[str, Any]:
    if freeze_armed():
        raise RuntimeError("freeze already armed")
    journal = _journal(journal)
    resolved = _resolve_start_nl(journal, start_nl)
    opened_at = _iso_z()
    fid = str(freeze_id or "").strip() or f"js-eval-{opened_at[:10].replace('-', '')}"
    payload = constitution_payload()
    state = {
        "freeze_id": fid,
        "opened_at": opened_at,
        "close_after_rth_sessions": int(n_sessions),
        "start_nl": resolved,
        "constitution_hash": constitution_hash(payload),
        "hash_payload": payload,
        "unseen_required": bool(unseen_required),
        "cash_baseline_pnl": _CASH_BASELINE_PNL,
        "status": "armed",
        "closed_at": None,
        "verdict": None,
    }
    _write_freeze(state)
    return state


def _end_nl(journal: Any, equity: float | None, blob: Any) -> float | None:
    if equity is not None:
        try:
            return float(equity)
        except (TypeError, ValueError):
            pass
    if isinstance(blob, dict) and "constitution_hash" not in blob:
        for key in ("net_liquidation", "equity", "NetLiquidation"):
            if blob.get(key) is not None:
                try:
                    return float(blob[key])
                except (TypeError, ValueError):
                    continue
    if journal is not None:
        try:
            fn = getattr(journal, "account_performance", None)
            if callable(fn):
                nl = (fn() or {}).get("net_liquidation")
                if nl is not None:
                    return float(nl)
        except Exception:
            pass
    return None


def _model_cost_since(journal: Any, opened_at: str) -> float:
    if journal is None:
        return 0.0
    try:
        fn = getattr(journal, "model_usage_since", None)
        if not callable(fn):
            return 0.0
        usage = fn(opened_at) or {}
        return float(usage.get("cost_usd") or 0.0)
    except Exception:
        return 0.0


def _sessions_done(journal: Any, opened_at: str) -> int:
    if journal is None:
        return 0
    try:
        fn = getattr(journal, "session_dates_since", None)
        if not callable(fn):
            return 0
        dates = fn(opened_at) or []
        return len(set(dates))
    except Exception:
        return 0


def _unseen_ok(journal: Any, opened_at: str) -> bool:
    if journal is None:
        return False
    try:
        before_fn = getattr(journal, "symbols_filled_before", None)
        since_fn = getattr(journal, "symbols_filled_since", None)
        if not callable(before_fn) or not callable(since_fn):
            return False
        before = set(before_fn(opened_at) or set())
        since = set(since_fn(opened_at) or set())
        return bool(since - before)
    except Exception:
        return False


def _verdict(
    *,
    hash_ok: bool,
    sessions_done: int,
    n_sessions: int,
    beating_cash: bool | None,
    unseen_required: bool,
    unseen_ok: bool,
) -> str:
    if not hash_ok:
        return "HASH_DRIFT"
    if sessions_done < n_sessions:
        return "OPEN"
    if (
        hash_ok
        and beating_cash is True
        and sessions_done >= n_sessions
        and (not unseen_required or unseen_ok)
    ):
        return "PASS"
    return "FAIL"


def score(
    journal: Any = None,
    *,
    equity: float | None = None,
    blob: Any = None,
) -> dict[str, Any] | None:
    """Freeze facts only. Does not replace compute_scorecard math."""
    state: dict[str, Any] | None
    if isinstance(blob, dict) and blob.get("constitution_hash"):
        state = blob
    else:
        state = load_freeze()
    if not state:
        return None
    journal = _journal(journal)
    opened_at = str(state.get("opened_at") or "")
    n_sessions = int(state.get("close_after_rth_sessions") or 0)
    start_nl = state.get("start_nl")
    try:
        start_nl = float(start_nl) if start_nl is not None else None
    except (TypeError, ValueError):
        start_nl = None
    end_nl = _end_nl(journal, equity, blob)
    model_cost = _model_cost_since(journal, opened_at)
    vs_cash_usd: float | None = None
    vs_cash_pct: float | None = None
    beating_cash: bool | None = None
    if end_nl is not None and start_nl is not None:
        vs_cash_usd = float(end_nl) - float(start_nl) - model_cost - _CASH_BASELINE_PNL
        if float(start_nl) != 0:
            vs_cash_pct = (vs_cash_usd / float(start_nl)) * 100.0
        beating_cash = vs_cash_usd > 0
    hash_ok, drifted = assert_unchanged()
    sessions_done = _sessions_done(journal, opened_at)
    unseen_required = bool(state.get("unseen_required"))
    unseen_ok = _unseen_ok(journal, opened_at)
    verdict = _verdict(
        hash_ok=hash_ok,
        sessions_done=sessions_done,
        n_sessions=n_sessions,
        beating_cash=beating_cash,
        unseen_required=unseen_required,
        unseen_ok=unseen_ok,
    )
    return {
        "freeze_id": state.get("freeze_id"),
        "status": state.get("status"),
        "opened_at": opened_at,
        "close_after_rth_sessions": n_sessions,
        "n_sessions": n_sessions,
        "sessions_done": sessions_done,
        "start_nl": start_nl,
        "end_nl": end_nl,
        "model_cost_usd": model_cost,
        "cash_baseline_pnl": _CASH_BASELINE_PNL,
        "vs_cash_usd": vs_cash_usd,
        "vs_cash_pct": vs_cash_pct,
        "beating_cash": beating_cash,
        "hash_ok": hash_ok,
        "drifted": drifted,
        "unseen_required": unseen_required,
        "unseen_ok": unseen_ok,
        "verdict": verdict,
    }


def close_freeze(
    *,
    journal: Any = None,
    equity: float | None = None,
) -> dict[str, Any]:
    state = load_freeze()
    if not state:
        raise RuntimeError("no freeze to close")
    facts = score(journal=journal, equity=equity, blob=state)
    state["status"] = "closed"
    state["closed_at"] = _iso_z()
    state["verdict"] = None if facts is None else facts.get("verdict")
    state["close_score"] = facts
    _write_freeze(state)
    return state


def format_freeze_line(blob: dict[str, Any] | None) -> Optional[str]:
    """One line starting with '- freeze', or None if there is nothing to show."""
    if not isinstance(blob, dict) or not blob:
        return None
    if not (blob.get("freeze_id") or blob.get("status") or blob.get("constitution_hash")):
        return None
    fid = blob.get("freeze_id") or "?"
    status = blob.get("status") or "?"
    done = blob.get("sessions_done")
    n = blob.get("n_sessions")
    if n is None:
        n = blob.get("close_after_rth_sessions")
    hash_s = "hash_ok" if blob.get("hash_ok") else "hash_drift"
    vs = blob.get("vs_cash_usd")
    vs_s = f"{float(vs):+.2f}" if isinstance(vs, (int, float)) else "n/a"
    unseen_s = "unseen_ok" if blob.get("unseen_ok") else "unseen_missing"
    verdict = blob.get("verdict") or ""
    done_s = "?" if done is None else str(int(done))
    n_s = "?" if n is None else str(int(n))
    return (
        f"- freeze {fid} {status} {done_s}/{n_s} {hash_s} "
        f"vs_cash={vs_s} {unseen_s} {verdict}"
    ).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m abcxauto.freeze",
        description="Freeze-then-score window. Does not launch the desk.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    open_p = sub.add_parser("open", help="Arm a freeze window")
    open_p.add_argument("n", nargs="?", type=int, default=20, help="RTH sessions (default 20)")
    open_p.add_argument("--no-unseen", action="store_true", help="Do not require an unseen-name fill")
    open_p.add_argument("--id", dest="freeze_id", default=None, help="Override freeze_id")
    sub.add_parser("status", help="Print the freeze line")
    sub.add_parser("close", help="Close the armed freeze and write the verdict")
    args = parser.parse_args(argv)
    if args.cmd == "open":
        state = open_freeze(
            int(args.n),
            unseen_required=not bool(args.no_unseen),
            freeze_id=args.freeze_id,
        )
        facts = score()
        line = format_freeze_line(facts) or f"- freeze {state['freeze_id']} armed"
        print(line)
        return 0
    if args.cmd == "status":
        facts = score()
        line = format_freeze_line(facts)
        if line:
            print(line)
        else:
            print("no freeze")
        return 0
    if args.cmd == "close":
        state = close_freeze()
        facts = state.get("close_score")
        line = format_freeze_line(facts if isinstance(facts, dict) else None)
        if line:
            print(line)
        else:
            print(f"- freeze {state.get('freeze_id')} closed {state.get('verdict')}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
