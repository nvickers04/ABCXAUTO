"""Dual-mode desk: RTH thin sender; paper stay-up may send outside RTH."""

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
    WEB_USE,
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
    rth_flat_keep_looking,
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


def test_research_keep_looking_does_not_reenter_without_a_poke():
    """Mill is dead; after words-only wait for book event / poke."""
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert research_keep_looking("premarket") is False
    assert research_keep_looking("regular") is False
    assert research_keep_looking("closed") is False
    assert research_keep_looking("postmarket") is False
    assert research_keep_looking("") is False
    assert research_keep_looking("unknown") is False


def _flat_rth_snap():
    return {"positions": [], "open_orders": [], "fills": []}


def test_rth_flat_keep_looking_never_mills_a_words_only_flat(monkeypatch):
    """Words-only flat RTH is not a pulse mill. Soften=FAIL nameless/7496."""
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: True),
    )
    flat = _flat_rth_snap()
    assert rth_flat_keep_looking("regular", flat) is False
    assert rth_flat_keep_looking("regular", None) is False
    assert rth_flat_keep_looking("regular", {}) is False
    assert rth_flat_keep_looking("regular", {"positions": []}) is False
    assert rth_flat_keep_looking("", flat) is False
    assert rth_flat_keep_looking("unknown", flat) is False
    assert rth_flat_keep_looking("premarket", flat) is False
    assert rth_flat_keep_looking("closed", flat) is False
    assert rth_flat_keep_looking(
        "regular",
        {"positions": [{"symbol": "IBIT", "quantity": 10}], "open_orders": []},
    ) is False
    assert rth_flat_keep_looking(
        "regular",
        {"positions": [], "open_orders": [], "open_lots": ["IBIT STK long 10"]},
    ) is False
    assert rth_flat_keep_looking(
        "regular",
        {
            "positions": [],
            "open_orders": [{"symbol": "SPY", "order_id": 7, "type": "LMT"}],
        },
    ) is False
    assert rth_flat_keep_looking(
        "regular",
        {
            "positions": [],
            "open_orders": [],
            "fills": [
                {
                    "side": "BOT",
                    "conId": "1",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            ],
        },
    ) is False
    assert (
        rth_flat_keep_looking(
            "regular",
            flat,
            desk_fact="fact: working_order_missing QQQ 260918C500 long 1.",
        )
        is False
    )
    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: False),
    )
    assert rth_flat_keep_looking("regular", flat) is False


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


def test_spoken_ticket_without_send_works_flat_and_does_not_mill_color():
    from abcxauto.desk_mode import (
        TICKET_WAKE_FACT,
        look_spoken_close_without_send,
        look_spoken_ticket_without_send,
        look_unpaid_ticket,
        spoken_close_without_send,
        spoken_ticket_without_send,
        ticket_wake_fact,
    )

    say = "INTC market_bracket LONG 10 stop 35 target 42."
    assert spoken_ticket_without_send(say, sends=0)
    assert look_spoken_ticket_without_send(
        {"rationale": say, "sends": 0, "positions": []}
    )
    assert look_unpaid_ticket({"rationale": say, "sends": 0, "positions": []})
    # Flat book: CLOSE/EXIT path stays blind; ticket path sees the named send.
    assert not spoken_close_without_send(say, positions=[], sends=0)
    assert spoken_ticket_without_send("NVDA STK bracket", sends=0)
    assert spoken_ticket_without_send("SPY put spread debit.", sends=0)
    assert spoken_ticket_without_send("SPY put", sends=0)
    assert spoken_ticket_without_send("BUY NVDA 10", sends=0)
    pos = _ibit_xlf_positions()
    assert spoken_ticket_without_send(
        "INTC market_bracket LONG.", sends=0
    )
    # Lots on the book do not hide a new named ticket.
    assert look_unpaid_ticket(
        {"rationale": "INTC market_bracket LONG.", "sends": 0, "positions": pos}
    )
    assert not spoken_ticket_without_send(say, sends=1)
    assert not spoken_ticket_without_send(say, sends=0, tool_trace=["send"])
    assert not spoken_ticket_without_send(
        "Standing down. Watching IWM. No ticket.", sends=0
    )
    assert not spoken_ticket_without_send(
        "Watching IBIT until the close. No ticket.", sends=0
    )
    assert not spoken_ticket_without_send("relative to SPY on the tape.", sends=0)
    # CLOSE/EXIT with lots stays the close path, not this matcher.
    assert spoken_close_without_send("CLOSE IBIT. EXIT XLF.", positions=pos, sends=0)
    assert not spoken_ticket_without_send("CLOSE IBIT. EXIT XLF.", sends=0)
    assert look_spoken_close_without_send(
        {"rationale": "CLOSE IBIT. EXIT XLF.", "sends": 0, "positions": pos}
    )
    assert look_unpaid_ticket(
        {"rationale": "CLOSE IBIT. EXIT XLF.", "sends": 0, "positions": pos}
    )
    assert ticket_wake_fact() == TICKET_WAKE_FACT
    assert "SEND-THE-TICKET" not in ticket_wake_fact()
    assert "send did not run" in ticket_wake_fact().lower()
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_spoken_ticket_without_send_hold_lot_is_not_unpaid():
    """Describing an open lot is not an unpaid ticket; a new named ticket still is."""
    from abcxauto.desk_mode import (
        look_spoken_ticket_without_send,
        look_unpaid_ticket,
        spoken_ticket_without_send,
    )

    hold = "NVDA long 11 still protected. no second name."
    nvda = [{"symbol": "NVDA"}]
    assert not spoken_ticket_without_send(
        hold, positions=nvda, sends=0, tool_trace=["book", "quote"]
    )
    payload = {
        "rationale": hold,
        "sends": 0,
        "positions": nvda,
        "tool_trace": ["book", "quote"],
    }
    assert not look_spoken_ticket_without_send(payload)
    assert not look_unpaid_ticket(payload)

    ticket = "INTC market_bracket LONG 10 stop 35 target 42"
    assert spoken_ticket_without_send(ticket, sends=0)
    assert spoken_ticket_without_send(ticket, positions=nvda, sends=0)
    assert spoken_ticket_without_send(
        ticket, positions=_ibit_xlf_positions(), sends=0
    )
    assert look_spoken_ticket_without_send(
        {"rationale": ticket, "sends": 0, "positions": nvda}
    )
    assert look_unpaid_ticket({"rationale": ticket, "sends": 0, "positions": []})
    assert look_unpaid_ticket(
        {"rationale": ticket, "sends": 0, "positions": _ibit_xlf_positions()}
    )
    assert spoken_ticket_without_send("buy AMZN", positions=nvda, sends=0)
    assert spoken_ticket_without_send("AMZN long", positions=nvda, sends=0)
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_spoken_ticket_without_send_hold_bracket_stop_is_not_unpaid():
    """Live 2026-09-22: Hold AVGO + bracket stop/target prose must not arm wake.

    ``_ticket_structure_pattern`` matches the word ``bracket`` in protection
    speech. Hold / pass / no-trade stands the open down; buy NVDA or NVDA
    bracket with zero send still arms. A hold essay that only compares other
    names is not unpaid either.
    """
    from abcxauto.desk_mode import (
        _ticket_structure_pattern,
        look_spoken_ticket_without_send,
        look_unpaid_ticket,
        spoken_ticket_without_send,
    )

    say = (
        "Hold AVGO. 89 shares already protected — bracket stop 356.35 / "
        "target 360.87. NVDA AMD MSFT SHOP are not better; I will not cut "
        "or rotate into those names. No ticket."
    )
    avgo = [{"symbol": "AVGO", "quantity": 89}]
    # Diagnose: structure would have fired on ``bracket`` alone.
    assert _ticket_structure_pattern().search(say).group(0).lower() == "bracket"
    assert not spoken_ticket_without_send(
        say, positions=avgo, sends=0, tool_trace=["book", "quote", "news"]
    )
    assert not look_spoken_ticket_without_send(
        {
            "rationale": say,
            "sends": 0,
            "positions": avgo,
            "tool_trace": ["book", "quote", "news"],
        }
    )
    # Hold essay naming AVGO + peers + stop/target/bracket — not an unpaid ticket.
    essay = (
        "Compared NVDA, GOOGL, AMD, ORCL, MSFT, and SHOP against AVGO. "
        "Stop 356.35 / target 360.87 bracket already working on the lot. "
        "Conclusion: Hold."
    )
    assert _ticket_structure_pattern().search(essay).group(0).lower() == "bracket"
    assert not spoken_ticket_without_send(
        essay, positions=avgo, sends=0, tool_trace=["book", "quote"]
    )
    assert not look_unpaid_ticket(
        {
            "rationale": essay,
            "sends": 0,
            "positions": avgo,
            "tool_trace": ["book", "quote"],
        }
    )
    assert not spoken_ticket_without_send(
        "AVGO vs NVDA GOOGL AMD. Pass on rotating. Stop/target/bracket stay.",
        positions=avgo,
        sends=0,
    )
    assert not spoken_ticket_without_send(
        "No-trade. AVGO stop 356.35 target 369.85 bracket. Watching NVDA MSFT.",
        positions=avgo,
        sends=0,
    )
    # Real spoken buy / structure still unpaid when no send.
    assert spoken_ticket_without_send("buy NVDA", positions=avgo, sends=0)
    assert spoken_ticket_without_send("NVDA bracket", positions=avgo, sends=0)
    assert spoken_ticket_without_send(
        "Hold AVGO. buy NVDA 10.", positions=avgo, sends=0
    )
    assert spoken_ticket_without_send(
        "INTC market_bracket LONG 10 stop 35 target 42.", sends=0
    )
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


# Live desk oracle (last ~200KB): stream silent=14, synthesize=13, send=0.
# Tip: [think] Let me synthesize a trading plan/the picture → prose → often
# names a ticket → [stream silent] with zero send. Soft-spin: keep alive,
# tokens burn, no action.
_SOFT_SPIN_MILL_SAY = (
    "Let me synthesize a trading plan/the picture. "
    "More prose. Stream going quiet."
)
_SOFT_SPIN_TICKET_SAY = (
    "Let me synthesize a trading plan/the picture. "
    "INTC market_bracket LONG 10 stop 35 target 42."
)


def test_synthesize_mill_zero_tools_zero_send_is_not_a_finished_look():
    """Soft-spin mill language with no tool_trace and sends==0 is a mill.

    A look that called tools or send is not. Named-ticket mill language
    stays unpaid (#164). Spoken-no-tool that is not mill is not.
    """
    from abcxauto.desk_mode import (
        MILL_WAKE_FACT,
        SYNTHESIZE_MILL_TRIES,
        look_synthesize_mill,
        look_unpaid_ticket,
        mill_wake_fact,
        spoken_synthesize_mill,
    )

    assert SYNTHESIZE_MILL_TRIES == 2
    assert spoken_synthesize_mill(_SOFT_SPIN_MILL_SAY, sends=0)
    assert spoken_synthesize_mill("Let me gather the tape.", sends=0)
    assert look_synthesize_mill(
        {"rationale": _SOFT_SPIN_MILL_SAY, "sends": 0, "tool_trace": []}
    )
    assert spoken_synthesize_mill("Let me decide whether to send.", sends=0)
    assert spoken_synthesize_mill("Time to decide on a ticket.", sends=0)
    assert not spoken_synthesize_mill(
        _SOFT_SPIN_MILL_SAY, sends=0, tool_trace=["book"]
    )
    assert not spoken_synthesize_mill(_SOFT_SPIN_MILL_SAY, sends=1)
    assert not spoken_synthesize_mill(
        _SOFT_SPIN_MILL_SAY, sends=0, tool_trace=["quote"]
    )
    assert not spoken_synthesize_mill(
        "Standing down. Watching IWM. No ticket.", sends=0
    )
    assert not spoken_synthesize_mill(
        "I decided to wait. Watching IWM.", sends=0
    )
    # Mill + named ticket: mill matcher still sees mill language; unpaid
    # owns the engine path.
    assert spoken_synthesize_mill(_SOFT_SPIN_TICKET_SAY, sends=0)
    assert look_unpaid_ticket(
        {"rationale": _SOFT_SPIN_TICKET_SAY, "sends": 0, "positions": []}
    )
    assert mill_wake_fact() == MILL_WAKE_FACT
    assert "TOOL-OR-SEND" in mill_wake_fact()
    assert "synthesize" in mill_wake_fact().lower()
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
    from abcxauto.send_preview import bind_place_token

    ticket = {**_placeable_ticket(), "_desk_session": "regular"}
    bind_place_token(ticket)
    result = await send_action(ticket, _connector())
    assert result["status"] == "ok"
    assert len(dispatched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sess", ["premarket", "postmarket", "closed"])
async def test_research_session_send_reaches_executor(monkeypatch, sess):
    """Paper stay-up: session label alone is not research_no_send."""
    dispatched = []

    async def _record(action, connector):
        dispatched.append((action, connector))
        return {"status": "ok", "note": "dispatched"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action
    from abcxauto.send_preview import bind_place_token

    ticket = {**_placeable_ticket(), "_desk_session": sess}
    bind_place_token(ticket)
    result = await send_action(ticket, _connector())
    assert result["status"] == "ok"
    assert result.get("reason_code") != REASON_RESEARCH_NO_SEND
    assert len(dispatched) == 1


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


def _news_snap(*rows: dict[str, str]) -> dict:
    items = [
        {
            "symbol": row["symbol"],
            "headline": row["headline"],
            "publisher": row.get("publisher", "MDA"),
        }
        for row in rows
    ]
    snap: dict = {"news_items": items}
    note_research_tool(snap, "news", {"items": items})
    return snap


def test_research_brief_writes_gathered_color_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    snap = _news_snap(
        {
            "symbol": "NVDA",
            "headline": "NVDA beats estimates and raises guidance",
        },
        {
            "symbol": "XYZ",
            "headline": "XYZ announces acquisition of ABC",
        },
    )
    snap["scan_hits"] = {
        "rows": [
            {"symbol": "AMD", "open_gap_pct": 4.2},
            {"symbol": "FLAT", "open_gap_pct": 0.1},
        ]
    }
    first = write_research_brief(session="premarket", snap=snap, now=datetime.now(timezone.utc))
    path = tmp_path / "research_brief.json"
    assert path.is_file()
    assert first["session"] == "premarket"
    assert first["mode"] == "research"
    assert "tickets" not in first
    assert "expectancy" not in first
    assert "NVDA" in first["symbols"]
    assert "AMD" in first["symbols"]
    assert first["facts"]
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    snap["news_items"] = [
        {
            "symbol": "TSLA",
            "headline": "TSLA misses estimates after hours",
            "publisher": "MDA",
        }
    ]
    note_research_tool(snap, "news", {"items": snap["news_items"]})
    second = write_research_brief(session="premarket", snap=snap, now=later)
    disk = load_research_brief()
    assert disk["as_of"] == second["as_of"]
    assert disk["as_of"] != first["as_of"]
    assert "TSLA" in disk["symbols"]
    assert "expectancy" not in disk
    assert "tickets" not in disk


def test_rth_color_missing_stale_and_present(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))
    missing = rth_research_color(full=True)
    assert "missing" in missing
    assert "never a live trigger" in missing or "not a live trigger" in missing
    write_research_brief(
        session="premarket",
        snap=_news_snap(
            {
                "symbol": "NVDA",
                "headline": "NVDA beats estimates after hours",
            }
        ),
        now=datetime.now(timezone.utc),
    )
    full = rth_research_color(full=True)
    assert "prior_session_research" in full
    assert "not a live trigger" in full or "never a live trigger" in full
    assert "NVDA" in full
    assert "facts=" in full
    assert "expectancy=" not in full
    short = rth_research_color(full=False)
    assert "on_disk" in short
    assert "facts=" in short
    assert "symbols=" in short
    assert "expectancy=" not in short
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    write_research_brief(
        session="premarket",
        snap=_news_snap({"symbol": "OLD", "headline": "OLD announces merger"}),
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
        snap=_news_snap(
            {
                "symbol": "AMD",
                "headline": "AMD raises guidance after hours",
            }
        ),
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
    assert "send=allowed(existing gates)" in research
    assert "session=premarket" in research
    assert "send=blocked" not in research
    assert REASON_RESEARCH_NO_SEND not in research


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
        "<html><head>"
        "<title>Tab title</title>"
        '<meta property="og:title" content="Acme raises guidance">'
        '<meta property="article:published_time" content="2026-09-17T16:00:00Z">'
        "</head><body>"
        "<nav>Home Markets Watchlist</nav>"
        "<script>ignore()</script>"
        "<p>After-hours earnings beat.</p>"
        "<footer>Copyright Acme</footer>"
        "</body></html>"
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
    assert page.get("host") == "example.com"
    assert page.get("as_of")
    assert page.get("published") == "2026-09-17T16:00:00Z"
    assert "earnings beat" in (page.get("text") or "")
    assert "Watchlist" not in (page.get("text") or "")
    assert "Copyright" not in (page.get("text") or "")
    assert page.get("source") == "web"
    assert page.get("use") == WEB_USE
    refused = await fetch_public_page("file:///etc/passwd")
    assert refused.get("error")
    assert refused.get("use") == WEB_USE
    assert refused.get("as_of")


@pytest.mark.asyncio
async def test_search_public_quotes_web_and_x(monkeypatch):
    from types import SimpleNamespace

    from abcxauto.desk_mode import search_public

    seen: dict = {}

    # xai-sdk 1.19 WebCitation / XCitation are url-only (no title/username fields).
    class _X:
        url = "https://x.com/example/status/1"

    class _Web:
        url = "https://example.com/avgo"

    class _CiteX:
        x_citation = _X()
        web_citation = None

        def HasField(self, name: str) -> bool:
            return name == "x_citation"

    class _CiteWeb:
        x_citation = None
        web_citation = _Web()

        def HasField(self, name: str) -> bool:
            return name == "web_citation"

    async def _sample(query, tools):
        seen["query"] = query
        seen["tools"] = list(tools)
        return SimpleNamespace(
            content="Guide raised.[[1]](https://x.com/example/status/1)",
            inline_citations=[_CiteX(), _CiteWeb()],
            citations=[],
        )

    monkeypatch.setattr("abcxauto.desk_mode._xai_search_sample", _sample)
    page = await search_public("AVGO guidance", where="both", handles="@example, other")
    assert page.get("use") == WEB_USE
    assert page.get("where") == "both"
    assert page.get("query") == "AVGO guidance"
    assert "Guide raised" in (page.get("text") or "")
    results = page.get("results") or []
    assert results[0]["source"] == "x"
    assert results[0]["handle"] == "example"
    assert results[1]["source"] == "web"
    assert seen["query"] == "AVGO guidance"
    assert len(seen["tools"]) == 2
    assert seen["tools"][0].WhichOneof("tool") == "web_search"
    assert seen["tools"][0].web_search.user_location.country == "US"
    assert seen["tools"][1].WhichOneof("tool") == "x_search"
    assert list(seen["tools"][1].x_search.allowed_x_handles) == ["example", "other"]

    async def _x_only(query, tools):
        seen["x_n"] = len(tools)
        return SimpleNamespace(content="post", inline_citations=[], citations=["https://x.com/a/status/2"])

    monkeypatch.setattr("abcxauto.desk_mode._xai_search_sample", _x_only)
    xpage = await search_public("AVGO", where="x")
    assert xpage.get("where") == "x"
    assert seen["x_n"] == 1
    assert (xpage.get("results") or [])[0]["source"] == "x"
    assert (xpage.get("results") or [])[0]["handle"] == "a"


def test_cite_rows_matches_xai_sdk_inline_citation_shape():
    """Installed xai-sdk InlineCitation oneof is url-only; HasField selects the arm."""
    from types import SimpleNamespace

    from xai_sdk.proto import chat_pb2

    from abcxauto.desk_mode import _cite_rows

    x = chat_pb2.InlineCitation(
        id="1",
        start_index=0,
        end_index=10,
        x_citation=chat_pb2.XCitation(url="https://x.com/example/status/1"),
    )
    w = chat_pb2.InlineCitation(
        id="2",
        start_index=11,
        end_index=20,
        web_citation=chat_pb2.WebCitation(url="https://example.com/avgo"),
    )
    rows = _cite_rows(
        SimpleNamespace(
            content="x[[1]](https://x.com/example/status/1) w[[2]](https://example.com/avgo)",
            inline_citations=[x, w],
            citations=["https://www.x.com/other/status/9"],
        )
    )
    assert [r["source"] for r in rows] == ["x", "web", "x"]
    assert rows[0]["handle"] == "example"
    assert rows[2]["handle"] == "other"
    assert rows[1]["url"] == "https://example.com/avgo"


@pytest.mark.asyncio
async def test_search_public_unavailable_is_short(monkeypatch):
    from abcxauto.desk_mode import search_public

    async def _boom(query, tools):
        raise RuntimeError(
            "Live search is deprecated. Please switch to the Agent Tools API "
            "status = StatusCode.UNIMPLEMENTED"
        )

    monkeypatch.setattr("abcxauto.desk_mode._xai_search_sample", _boom)
    page = await search_public("AVGO Broadcom stock news", where="web")
    assert page.get("error") == "web search unavailable"
    assert "UNIMPLEMENTED" not in str(page.get("error") or "")
    assert "traceback" not in str(page).lower()
    assert page.get("use") == WEB_USE
    assert page.get("send_geometry") is not True


def _world(session: str):
    from abcxauto.world_state import WorldState

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


def _tool_names(session: str) -> set[str]:
    from abcxauto.brain import agent_tools

    names: set[str] = set()
    for t in agent_tools(session=session):
        fn = getattr(t, "function", None)
        names.add(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    return names


def test_agent_tools_web_on_rth_and_research():
    rth = _tool_names("regular")
    assert "web" in rth
    assert "send" in rth
    assert "news" in rth
    assert "scan" in rth
    for sess in ("premarket", "postmarket", "closed"):
        research = _tool_names(sess)
        assert "web" in research, sess
        assert "send" in research, sess
        assert "news" in research, sess
        assert "scan" in research, sess


def test_write_research_brief_writes_rth(tmp_path, monkeypatch):
    path = tmp_path / "research_brief.json"
    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(path))
    snap = {
        "news_items": [
            {
                "symbol": "TSLA",
                "headline": "TSLA announces merger after hours",
                "publisher": "MDA",
            }
        ]
    }
    note_research_tool(snap, "news", {"items": snap["news_items"]})
    out = write_research_brief(session="regular", snap=snap, now=datetime.now(timezone.utc))
    assert out.get("session") == "regular"
    assert out.get("mode") == "research"
    assert path.is_file()
    wrote = write_research_brief(session="premarket", snap=snap, now=datetime.now(timezone.utc))
    assert wrote.get("session") == "premarket"
    assert path.is_file()


@pytest.mark.asyncio
async def test_web_tool_fetches_in_rth_and_research(monkeypatch, tmp_path):
    import json

    from abcxauto.brain import BrainTurn, _invoke_named_tool, _run_tool

    monkeypatch.setenv("ABCXAUTO_RESEARCH_BRIEF_PATH", str(tmp_path / "research_brief.json"))

    async def _ok(url):
        return {
            "url": url,
            "title": "IR",
            "text": "announces merger",
            "source": "web",
            "use": WEB_USE,
        }

    monkeypatch.setattr("abcxauto.desk_mode.fetch_public_page", _ok)

    rth_snap: dict = {}
    rth = await _run_tool(
        "web",
        {"url": "https://example.com/ir"},
        connector=None,
        world=_world("regular"),
        snap=rth_snap,
        turn=BrainTurn(),
    )
    data = json.loads(rth)
    assert "research-only" not in str(data.get("error") or "")
    assert data.get("title") == "IR"
    assert data.get("source") == "web"
    assert data.get("use") == WEB_USE
    assert rth_snap.get("research_web", {}).get("title") == "IR"

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
    assert payload.get("use") == WEB_USE

    rth_path = tmp_path / "research_brief.json"
    if rth_path.is_file():
        rth_path.unlink()
    await _invoke_named_tool(
        "web",
        {"url": "https://example.com/ir"},
        5.0,
        connector=None,
        world=_world("regular"),
        snap={},
        turn=BrainTurn(),
    )
    assert rth_path.is_file()
    disk = load_research_brief()
    assert disk.get("session") == "regular"

    await _invoke_named_tool(
        "web",
        {"url": "https://example.com/ir"},
        5.0,
        connector=None,
        world=_world("premarket"),
        snap={},
        turn=BrainTurn(),
    )
    assert rth_path.is_file()
    assert load_research_brief().get("session") == "premarket"


@pytest.mark.asyncio
async def test_send_tool_not_session_banned_in_research(monkeypatch):
    """Session label alone must not return research_no_send from the send tool."""
    import json

    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def _fake_execute(act, connector, world, snap):
        return {
            "status": "ok",
            "note": "execute_ticket reached",
            "strategy": str(act.get("strategy") or ""),
        }

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", _fake_execute)
    world = WorldState(
        cycle=1,
        session_status="postmarket",
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
        {
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "direction": "LONG",
                "quantity": 1,
                "stop_price": 400.0,
                "target_price": 420.0,
                "card": "stay-up",
            },
        },
        connector=MagicMock(),
        world=world,
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data.get("reason_code") != REASON_RESEARCH_NO_SEND
    assert data.get("status") == "ok"
    assert turn.sends
