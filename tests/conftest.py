"""Shared test helpers."""

import json
import re
from pathlib import Path

import pytest

# Operator paint: leftover sit-loop counter. Not the word "recycle" / "lifecycle".
_CYCLE_COUNTER_RE = re.compile(
    r"(?i)(?:\bcycle\s+\d+\b|\bCYCLE\s+\d+\b|·\s*c\d+\s*·|\b\d+\s+wakes\b)"
)


def assert_no_cycle_counter(text: str) -> None:
    """UI / last_turn / brief / think stream must not number the think."""
    blob = text or ""
    assert not _CYCLE_COUNTER_RE.search(blob), blob


def assert_no_cycle_keys(payload: dict) -> None:
    assert "cycle" not in payload
    assert "previous_cycle" not in payload
    assert_no_cycle_counter(json.dumps(payload, default=str))

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")


class _Cfg:
    xai_api_key = "test-key"
    monitor_enabled = False


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path):
    """Keep the trade journal out of the real journal.db during tests."""
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"))
    yield
    reset_journal(path=str(tmp_path / "journal.db"))


@pytest.fixture(autouse=True)
def _clear_risk_overrides(tmp_path, monkeypatch):
    """Risk overrides must not leak; use a temp settings file per test."""
    from abcxauto.config import (
        clear_risk_settings,
        clear_runtime_overrides,
        get_config,
        load_risk_settings,
    )

    path = tmp_path / "risk_settings.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent_state.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    clear_runtime_overrides()
    get_config.cache_clear()
    yield
    clear_risk_settings(path=path)
    clear_runtime_overrides()
    get_config.cache_clear()


def fake_grok_turn(act: dict, *, wakes: list | None = None):
    """Pretend Grok sent ``act`` through the send clerk."""
    from abcxauto.agent_loop import BLOCKED_STRAT, execute_ticket
    from abcxauto.brain import BrainTurn

    async def grok_turn(g, *, connector, world, snap, wake=""):
        if wakes is not None:
            wakes.append(wake)
        ticket = dict(act)
        if isinstance(act.get("params"), dict):
            ticket["params"] = dict(act["params"])
        result = await execute_ticket(ticket, connector, world, snap)
        status = str((result or {}).get("status") or "").lower()
        strat = str(ticket.get("strategy") or act.get("strategy") or "")
        turn = BrainTurn(last_act=ticket, last_result=result or {})
        if (
            status in ("blocked", "rejected", "validated_block")
            or strat == BLOCKED_STRAT
        ):
            turn.last_strat = BLOCKED_STRAT
            turn.last_act["strategy"] = turn.last_act["action"] = BLOCKED_STRAT
        elif strat == "hold" or status == "hold":
            turn.last_strat = "hold"
        else:
            turn.last_strat = strat
            turn.sends = [{"act": ticket, "result": result, "strat": strat}]
        return turn

    return grok_turn


def grok_json_as_turn(fake_grok):
    """Adapt a ticket JSON stub to grok_turn."""
    import json as _json

    async def grok_turn(g, *, connector, world, snap, wake=""):
        raw = await fake_grok(g, wake, stage="act")
        try:
            payload = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            payload = {}
        if "strategy" not in payload and "action" not in payload:
            payload = {
                "action": "hold",
                "strategy": "hold",
                "rationale": str(payload.get("thesis") or "no ticket"),
            }
        return await fake_grok_turn(payload)(
            g, connector=connector, world=world, snap=snap, wake=wake
        )

    return grok_turn


@pytest.fixture(autouse=True)
def _stub_opportunity_scan(monkeypatch):
    """Avoid live MDA candle fan-out during unit tests."""

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr(
        "abcxauto.opportunity_scan.scan_opportunities",
        _empty,
    )


@pytest.fixture(autouse=True)
def _isolate_open_risk_and_structure_files(tmp_path, monkeypatch):
    """Keep trade plan / structure lessons out of the live workspace files."""
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "active_trade_plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat_book_streak.json"))
    monkeypatch.setenv(
        "ABCXAUTO_STRUCTURE_EVENTS_PATH", str(tmp_path / "structure_events.jsonl")
    )


@pytest.fixture(autouse=True)
def _isolate_desk_state(tmp_path, monkeypatch):
    """Pytest must not clobber the live last_turn / wake / playbook."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "grok_wake.json"))
    monkeypatch.setenv("ABCXAUTO_DESK_BRIEF_PATH", str(tmp_path / "desk_brief.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "playbook_lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "playbook_live.json"))
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "THINK_PREV_PATH", tmp_path / "think_prev.txt")
    monkeypatch.setattr(ts, "RUN_PATH", tmp_path / "run.json")
