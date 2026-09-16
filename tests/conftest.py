"""Shared test helpers."""

import json
import logging
import os
import re
import tempfile
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


class _Cfg:
    xai_api_key = "test-key"
    monitor_enabled = False


REPO_LOGS = (Path(__file__).resolve().parents[1] / "logs").resolve()


def _drop_repo_log_handlers() -> list[str]:
    """Detach any file handler writing into the repo's logs/ directory.

    ``run_headless()`` calls ``setup_file_logging()``, so a test that exercises it
    attaches a handler on the real logs/app.log for the rest of the session and
    every later WARNING+ record — fake halts, fake AUTO-PANIC — lands in the file
    the operator reads as evidence.
    """
    from logging.handlers import RotatingFileHandler

    names = ["", *logging.root.manager.loggerDict]
    dropped: list[str] = []
    for name in names:
        lg = logging.getLogger(name)
        for handler in list(getattr(lg, "handlers", [])):
            if not isinstance(handler, RotatingFileHandler):
                continue
            try:
                target = Path(getattr(handler, "baseFilename", "")).resolve()
            except OSError:
                continue
            if target.parent == REPO_LOGS:
                lg.removeHandler(handler)
                handler.close()
                dropped.append(str(target))
    return dropped


@pytest.fixture(autouse=True)
def _no_os_desk_kills(monkeypatch):
    """Unit tests must not taskkill / SIGKILL the operator's paper Pro."""
    import abcxauto.supervisor as supervisor

    monkeypatch.setattr(supervisor, "kill_pid", lambda *_a, **_k: False)


@pytest.fixture(autouse=True)
def _isolate_desk_evidence_and_latches(tmp_path, monkeypatch):
    """A test run must not touch what the live desk reads and writes.

    logs/app.log is the operator's evidence. The operator-stop file is worse than
    evidence: the desk shuts down when it appears, so a test that writes the real
    one kills a running desk.
    """
    monkeypatch.setenv("ABCXAUTO_LOG_PATH", str(tmp_path / "app.log"))
    monkeypatch.setenv("ABCXAUTO_DESK_OUT_PATH", str(tmp_path / "desk.out"))
    monkeypatch.setenv("ABCXAUTO_OPERATOR_STOP_PATH", str(tmp_path / "operator_stop.json"))
    monkeypatch.setenv("ABCXAUTO_DESK_LOCK_PATH", str(tmp_path / "desk.lock"))
    monkeypatch.setenv("ABCXAUTO_START_PRO_PATH", str(tmp_path / "logs" / "_start_pro.py"))
    _drop_repo_log_handlers()
    yield
    _drop_repo_log_handlers()


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path, monkeypatch):
    """Keep the trade journal out of the real journal.db during tests."""
    from abcxauto.memory import reset_journal

    path = tmp_path / "journal.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(path))
    reset_journal(path=str(path))
    yield
    reset_journal(path=str(path))


def pytest_configure(config):
    """Redirect settings before test modules import abcxauto.config.

    ``load_risk_settings()`` runs at import and would otherwise read the
    worktree / repo-root ``risk_settings.json``. Journal and the rotating
    app log are the same class of leak: first ``get_journal()`` / first
    ``setup_file_logging()`` would open the live files if the env is empty.
    """
    tmp = Path(tempfile.gettempdir()) / f"abcxauto-pytest-{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["ABCXAUTO_RISK_SETTINGS_PATH"] = str(tmp / "no-settings.json")
    os.environ["ABCXAUTO_JOURNAL_PATH"] = str(tmp / "journal.db")
    os.environ["ABCXAUTO_LOG_PATH"] = str(tmp / "app.log")


# Env knobs that overlay the same Config fields as risk_settings.json / .env.
# Without this list, `_load_env_config()` / `load_dotenv()` can make
# default tests see the operator live knobs after the settings file is
# redirected.
_OPERATOR_CONFIG_ENV = (
    "ABCXAUTO_SESSION_TOKEN_CAP",
    "ABCXAUTO_SESSION_LOOK_CAP",
    "ABCXAUTO_MODEL",
    "ABCXAUTO_MODEL_RTH",
    "ABCXAUTO_MODEL_RESEARCH",
    "ABCXAUTO_MODEL_PARAMS",
    "ABCXAUTO_MODEL_PARAMS_RTH",
    "ABCXAUTO_MODEL_PARAMS_RESEARCH",
    "ABCXAUTO_TEMPERATURE",
    "ABCXAUTO_MAX_TOKENS",
    "ABCXAUTO_RISK_POSTURE",
    "ABCXAUTO_RISK_GATES_ENABLED",
    "ABCXAUTO_SIZING_FLOORS",
    "ABCXAUTO_DAILY_LOSS_LIMIT_PCT",
    "ABCXAUTO_MAX_POSITION_PCT",
    "ABCXAUTO_MAX_OPEN_POSITIONS",
    "ABCXAUTO_AUTO_PANIC_ON_BREACH",
    "ABCXAUTO_DEFINED_RISK_ONLY",
    "ABCXAUTO_CASH_ONLY",
    "ABCXAUTO_PORTFOLIO_CAP_USD",
    "ABCXAUTO_MAX_PEAK_DRAWDOWN_PCT",
    "ABCXAUTO_MAX_OPTION_PREMIUM_PCT",
    "ABCXAUTO_MAX_RISK_PER_TRADE_PCT",
    "ABCXAUTO_MAX_SYMBOL_CONCENTRATION_PCT",
    "ABCXAUTO_MAX_ARENA_CONCENTRATION_PCT",
    "ABCXAUTO_SCAN_FETCH_CAP",
    "ABCXAUTO_TRADING_BUDGET_USD",
    "ABCXAUTO_TARGET_CAPITAL",
    "ABCXAUTO_MONITOR_ENABLED",
    "ABCXAUTO_MONITOR_POLL_S",
    "ABCXAUTO_MONITOR_REVIEW_S",
    "ABCXAUTO_MONITOR_EXTENDED_HOURS",
    "ABCXAUTO_DISCONNECT_HALT_S",
    "ABCXAUTO_LIVE_CONFIRM",
    "TRADING_MODE",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
)


@pytest.fixture(autouse=True)
def _clear_risk_overrides(tmp_path, monkeypatch):
    """Risk overrides must not leak; use a temp settings file per test.

    Also drop operator ``.env`` / shell knobs so ``_load_env_config`` sees
    code defaults. ``load_dotenv()`` is a no-op here: it would otherwise
    re-inject ``ABCXAUTO_SESSION_TOKEN_CAP`` from the live desk ``.env``
    when pytest is launched from the repo root.
    """
    from abcxauto.config import (
        clear_risk_settings,
        clear_runtime_overrides,
        get_config,
        load_risk_settings,
    )

    path = tmp_path / "risk_settings.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent_state.json"))
    monkeypatch.setattr("abcxauto.config.load_dotenv", lambda *a, **k: False)
    for key in _OPERATOR_CONFIG_ENV:
        monkeypatch.delenv(key, raising=False)
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

    async def grok_turn(g, *, connector, world, snap, wake="", resume=False, **_k):
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

    async def grok_turn(g, *, connector, world, snap, wake="", resume=False, **_k):
        raw = await fake_grok(g, wake, stage="act")
        try:
            payload = _json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            payload = {}
        if "strategy" not in payload and "action" not in payload:
            from abcxauto.brain import BrainTurn

            return BrainTurn(text=str(payload.get("thesis") or "no ticket"))
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
def _reset_cancel_guard():
    """10147 / look-budget cancel state is process-life; do not leak across tests."""
    from abcxauto.protect import reset_cancel_guard_for_tests

    reset_cancel_guard_for_tests()
    yield
    reset_cancel_guard_for_tests()


@pytest.fixture(autouse=True)
def _reset_abort_fuse():
    """Abort-fuse cancel overlay is process-life; do not leak across tests."""
    from abcxauto.abort_fuse import reset_abort_fuse_for_tests

    reset_abort_fuse_for_tests()
    yield
    reset_abort_fuse_for_tests()


@pytest.fixture(autouse=True)
def _pcs_kill_look_off_unless_marked(monkeypatch, request):
    """Kill-look contract is production-on; unit tests opt in via env or mark."""
    marked = request.node.get_closest_marker("pcs_kill_look") is not None
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1" if marked else "0")


@pytest.fixture(autouse=True)
def _isolate_halt_state(tmp_path, monkeypatch):
    """Halt latch is durable; do not write the live data/state file."""
    monkeypatch.setenv("ABCXAUTO_HALT_STATE_PATH", str(tmp_path / "halt_state.json"))
    from abcxauto.risk_gates import reset_risk_gate

    reset_risk_gate()
    yield
    reset_risk_gate()


@pytest.fixture(autouse=True)
def _isolate_desk_state(tmp_path, monkeypatch):
    """Pytest must not clobber the live last_turn / wake files."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "grok_wake.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_CAPS_PATH", str(tmp_path / "session_caps.json"))
    from abcxauto.session_caps import reset_session_caps

    reset_session_caps()
    monkeypatch.setenv("ABCXAUTO_DESK_BRIEF_PATH", str(tmp_path / "desk_brief.json"))
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    monkeypatch.setenv(
        "ABCXAUTO_RESEARCH_BUDGET_PATH", str(tmp_path / "research_budget.json")
    )
    monkeypatch.setenv("ABCXAUTO_WORKING_MEMORY_PATH", str(tmp_path / "working_memory.json"))
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe_allowlist.json"))
    monkeypatch.setenv("ABCXAUTO_LAST_TURN_PATH", str(tmp_path / "last_turn.json"))
    monkeypatch.setenv("ABCXAUTO_THINK_TAIL_PATH", str(tmp_path / "think_tail.txt"))
    monkeypatch.setenv("ABCXAUTO_THINK_PREV_PATH", str(tmp_path / "think_prev.txt"))
    monkeypatch.setenv("ABCXAUTO_THINK_SESSION_DIR", str(tmp_path / "think_session"))
    monkeypatch.setenv("ABCXAUTO_RUN_PATH", str(tmp_path / "run.json"))
    from abcxauto.research_budget import reset_research_budget

    reset_research_budget()
