"""Dual-mode desk: RTH thin sender vs premarket/AH research (no send)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from abcxauto.config import (
    clear_runtime_overrides,
    get_config,
    update_agent_config,
)
from abcxauto.desk_mode import (
    REASON_RESEARCH_NO_SEND,
    build_expectancy,
    desk_mode,
    desk_session,
    fetch_public_page,
    is_research_session,
    is_rth_session,
    load_research_brief,
    note_research_tool,
    research_brief_stale,
    research_keep_looking,
    research_send_block,
    rth_research_color,
    session_model,
    write_research_brief,
)
from abcxauto.llm import GrokClient, SYSTEM_PROMPT
from abcxauto.self_tune import apply_self_tune
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK
from tests.test_send import _connector, _placeable_ticket, _safe_execute_must_not_run


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def test_system_prompt_untouched():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_labeled_sessions_do_not_invent_a_second_clock():
    assert desk_session("regular") == "regular"
    assert desk_session("premarket") == "premarket"
    assert desk_session("postmarket") == "postmarket"
    assert desk_session("closed") == "closed"
    assert is_rth_session("regular") is True
    assert is_research_session("regular") is False
    for sess in ("premarket", "postmarket", "closed"):
        assert is_research_session(sess) is True
        assert is_rth_session(sess) is False
        assert desk_mode(sess) == "research"
    assert desk_mode("regular") == "rth"


def test_research_keep_looking_is_premarket_not_rth_or_park():
    """Idle-after-brief is the defect. Overnight park still parks. RTH still waits."""
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert research_keep_looking("premarket") is True
    assert research_keep_looking("regular") is False
    assert research_keep_looking("closed") is False
    assert research_keep_looking("postmarket") is False
    assert research_keep_looking("") is False
    assert research_keep_looking("unknown") is False


def _ibit_xlf_positions():
    return [
        {"symbol": "IBIT", "quantity": 10, "sec_type": "STK", "con_id": 11},
        {"symbol": "XLF", "quantity": 20, "sec_type": "STK", "con_id": 22},
    ]


def test_spoken_close_without_send_needs_open_lot_and_no_send():
    from abcxauto.desk_mode import (
        inventory_wake_fact,
        look_spoken_close_without_send,
        spoken_close_without_send,
    )

    pos = _ibit_xlf_positions()
    assert spoken_close_without_send(
        "CLOSE IBIT. EXIT XLF.", positions=pos, sends=0
    )
    assert spoken_close_without_send(
        "close IBIT / exit XLF", positions=pos, sends=0
    )
    assert spoken_close_without_send(
        "Closing both lots. No ticket yet.", positions=pos, sends=0
    )
    assert spoken_close_without_send(
        "CLOSE/EXIT the book.", positions=pos, sends=0
    )
    assert not spoken_close_without_send(
        "CLOSE IBIT. EXIT XLF.", positions=pos, sends=1
    )
    assert not spoken_close_without_send(
        "CLOSE IBIT.", positions=pos, sends=0, tool_trace=["send"]
    )
    assert not spoken_close_without_send("CLOSE IBIT.", positions=[], sends=0)
    assert not spoken_close_without_send(
        "Standing down. Watching IWM. No ticket.", positions=pos, sends=0
    )
    assert not spoken_close_without_send(
        "Watching IBIT until the close. No ticket.", positions=pos, sends=0
    )
    assert look_spoken_close_without_send(
        {
            "rationale": "CLOSE IBIT and EXIT XLF.",
            "sends": 0,
            "positions": pos,
        }
    )
    fact = inventory_wake_fact(pos)
    assert fact.startswith("open_lots=")
    assert "IBIT" in fact
    assert "XLF" in fact
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_session_model_falls_back_to_current_model():
    cfg = get_config()
    assert cfg.model_rth == ""
    assert cfg.model_research == ""
    assert session_model("regular", cfg) == cfg.model
    assert session_model("premarket", cfg) == cfg.model
    assert session_model("postmarket", cfg) == cfg.model
    cfg = update_agent_config(
        model="grok-4.6",
        model_rth="grok-4.6-fast",
        model_research="grok-4.6",
        persist=False,
    )
    assert session_model("regular", cfg) == "grok-4.6-fast"
    assert session_model("premarket", cfg) == "grok-4.6"
    assert session_model("closed", cfg) == "grok-4.6"


def test_grok_client_uses_session_model():
    cfg = update_agent_config(
        model="grok-4.6",
        model_rth="rth-brain",
        model_research="research-brain",
        persist=False,
    )
    client = SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: SimpleNamespace()))
    rth = GrokClient(client=client, session="regular")
    research = GrokClient(client=client, session="premarket")
    bare = GrokClient(client=client)
    assert rth.model == "rth-brain"
    assert research.model == "research-brain"
    assert bare.model == cfg.model


@pytest.mark.asyncio
async def test_rth_send_reaches_executor(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append((action, connector))
        return {"status": "ok", "note": "dispatched"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket = {**_placeable_ticket(), "_desk_session": "regular"}
    result = await send_action(ticket, _connector())
    assert result["status"] == "ok"
    assert len(dispatched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sess", ["premarket", "postmarket", "closed"])
async def test_research_send_is_blocked(monkeypatch, sess):
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    ticket = {**_placeable_ticket(), "_desk_session": sess}
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == REASON_RESEARCH_NO_SEND
    assert "research" in str(result.get("note") or "").lower()


@pytest.mark.asyncio
async def test_7496_still_fail_closed_in_research_session(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7496")
    clear_runtime_overrides()
    get_config.cache_clear()
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    ticket = {**_placeable_ticket(), "_desk_session": "premarket"}
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == "live_port_paper"
    assert get_config().trading_mode == "paper"
    assert get_config().ibkr_port == 7496


def test_research_brief_writes_expectancy_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    snap = {
        "news_items": [
            {
                "symbol": "NVDA",
                "headline": "NVDA beats estimates and raises guidance",
                "publisher": "MDA",
            },
            {
                "symbol": "XYZ",
                "headline": "XYZ announces acquisition of ABC",
                "publisher": "MDA",
            },
        ],
        "scan_hits": {
            "rows": [
                {"symbol": "AMD", "open_gap_pct": 4.2},
                {"symbol": "FLAT", "open_gap_pct": 0.1},
            ]
        },
    }
    note_research_tool(snap, "news", {"items": snap["news_items"]})
    first = write_research_brief(session="premarket", snap=snap, now=datetime.now(timezone.utc))
    path = tmp_path / "research_brief.json"
    assert path.is_file()
    assert first["session"] == "premarket"
    assert first["mode"] == "research"
    assert first["tickets"] == []
    assert "NVDA" in first["symbols"]
    assert first["facts"]
    assert first["expectancy"]
    assert len(first["expectancy"]) <= 10
    kinds = {row["catalyst"] for row in first["expectancy"]}
    assert "earnings" in kinds or "m_and_a" in kinds or "gap_risk" in kinds
    for row in first["expectancy"]:
        assert "source" in row
        assert "why" in row
        assert "uncertainty" in row
        assert "invalidate" in row
        assert "strategy" not in row
        assert "quantity" not in row
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    snap["news_items"] = [
        {
            "symbol": "TSLA",
            "headline": "TSLA misses estimates after hours",
            "publisher": "MDA",
        }
    ]
    second = write_research_brief(session="premarket", snap=snap, now=later)
    disk = load_research_brief()
    assert disk["as_of"] == second["as_of"]
    assert disk["as_of"] != first["as_of"]
    assert "TSLA" in disk["symbols"]


def test_expectancy_prefers_catalysts_over_tiny_gaps():
    rows = build_expectancy(
        snap={
            "news_items": [
                {
                    "symbol": "AAPL",
                    "headline": "AAPL reports earnings and cuts outlook",
                }
            ],
            "scan_hits": {"rows": [{"symbol": "NOISE", "open_gap_pct": 0.2}]},
        }
    )
    assert rows
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["catalyst"] == "earnings"
    assert rows[0]["source"].startswith("news")
    assert all(r["symbol"] != "NOISE" for r in rows)


def test_rth_color_missing_stale_and_present(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    missing = rth_research_color(full=True)
    assert "missing" in missing
    assert "never a live trigger" in missing or "not a live trigger" in missing
    write_research_brief(
        session="premarket",
        snap={
            "news_items": [
                {
                    "symbol": "NVDA",
                    "headline": "NVDA beats estimates after hours",
                    "publisher": "MDA",
                }
            ]
        },
        now=datetime.now(timezone.utc),
    )
    full = rth_research_color(full=True)
    assert "prior_session_research" in full
    assert "not a live trigger" in full or "never a live trigger" in full
    assert "NVDA" in full
    short = rth_research_color(full=False)
    assert "on_disk" in short
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    write_research_brief(
        session="premarket",
        snap={"news_items": [{"symbol": "OLD", "headline": "OLD announces merger"}]},
        now=old,
    )
    brief = load_research_brief()
    assert research_brief_stale(brief) is True
    stale = rth_research_color(full=True)
    assert "stale" in stale


def test_rth_wake_loads_brief_and_runs_when_missing():
    from abcxauto.world_state import format_wake

    missing = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "session=regular" in missing
    assert "prior_session_research=missing" in missing
    assert "desk_mode=rth" in missing
    assert "send=allowed" in missing

    from abcxauto.desk_mode import write_research_brief

    write_research_brief(
        session="premarket",
        snap={
            "news_items": [
                {
                    "symbol": "AMD",
                    "headline": "AMD raises guidance after hours",
                    "publisher": "MDA",
                }
            ]
        },
    )
    present = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"research_brief_full": True},
    )
    assert "prior_session_research(color, not a live trigger)" in present
    assert "AMD" in present
    research = format_wake(
        cycle=1,
        session="premarket",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={},
    )
    assert "desk_mode=research" in research
    assert "send=blocked" in research
    assert REASON_RESEARCH_NO_SEND in research


def test_self_tune_cannot_set_session_models():
    before = get_config()
    out = apply_self_tune(
        {
            "model_rth": "hijack-rth",
            "model_research": "hijack-research",
            "model": "hijack-model",
            "defined_risk_only": False,
            "cash_only": False,
        },
        persist=True,
    )
    rejected = out.get("rejected") or {}
    assert "model_rth" in rejected
    assert "model_research" in rejected
    cfg = get_config()
    assert cfg.model_rth == before.model_rth
    assert cfg.model_research == before.model_research
    assert cfg.model == before.model
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True


def test_research_send_block_payload():
    row = research_send_block(session="premarket")
    assert row["status"] == "blocked"
    assert row["reason_code"] == REASON_RESEARCH_NO_SEND
    assert row["desk_mode"] == "research"


@pytest.mark.asyncio
async def test_web_fetch_is_thin_title_and_text(monkeypatch):
    html = (
        "<html><head><title>Acme raises guidance</title></head>"
        "<body><script>ignore()</script><p>After-hours earnings beat.</p></body></html>"
    )

    class _Resp:
        content = html.encode("utf-8")
        encoding = "utf-8"
        status_code = 200
        url = "https://example.com/pr"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url.startswith("https://")
            return _Resp()

    import httpx as httpx_mod

    monkeypatch.setattr(httpx_mod, "AsyncClient", _Client)
    page = await fetch_public_page("https://example.com/pr")
    assert page.get("title") == "Acme raises guidance"
    assert "earnings beat" in (page.get("text") or "")
    assert page.get("source") == "web"
    refused = await fetch_public_page("file:///etc/passwd")
    assert refused.get("error")


@pytest.mark.asyncio
async def test_web_tool_is_research_only(monkeypatch):
    import json

    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    def _world(session: str) -> WorldState:
        return WorldState(
            cycle=1,
            session_status=session,
            flat=True,
            needs_protection=False,
            unprotected=[],
            net_liquidation=37000.0,
            daily_pnl=0.0,
            positions=[],
            open_orders=[],
            opportunities=[],
            news_items=[],
            risk_posture="balanced",
            effective_posture="balanced",
            gates={},
            envelope={},
            regime={},
            portfolio_risk={},
            working_thesis="",
            recent_decisions=[],
            trade_plan=None,
        )

    rth = await _run_tool(
        "web",
        {"url": "https://example.com"},
        connector=None,
        world=_world("regular"),
        snap={},
        turn=BrainTurn(),
    )
    data = json.loads(rth)
    assert "research-only" in str(data.get("error") or "")

    async def _ok(url):
        return {
            "url": url,
            "title": "IR",
            "text": "announces merger",
            "source": "web",
            "use": "research_expectancy_not_send",
        }

    monkeypatch.setattr("abcxauto.desk_mode.fetch_public_page", _ok)
    raw = await _run_tool(
        "web",
        {"url": "https://example.com/ir"},
        connector=None,
        world=_world("premarket"),
        snap={},
        turn=BrainTurn(),
    )
    payload = json.loads(raw)
    assert payload.get("title") == "IR"
    assert payload.get("source") == "web"


@pytest.mark.asyncio
async def test_send_tool_hard_blocks_in_research():
    import json

    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    world = WorldState(
        cycle=1,
        session_status="after-hours" if False else "postmarket",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    turn = BrainTurn()
    raw = await _run_tool(
        "send",
        {"strategy": "market_bracket", "symbol": "SPY"},
        connector=MagicMock(),
        world=world,
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data.get("reason_code") == REASON_RESEARCH_NO_SEND
    assert data.get("status") == "blocked"
