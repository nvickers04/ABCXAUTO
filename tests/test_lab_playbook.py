"""Paper lab playbook: Grok writes instructions. The socket is the live switch."""

import json
from datetime import datetime, timedelta, timezone

from abcxauto.lab_playbook import (
    apply_from_judgment,
    clamp_update,
    format_block,
    live_has_promoted,
    live_new_risk_allowed,
    load_lab,
    load_live,
    maybe_promote,
    playbook_age_hours,
    playbook_facts,
    playbook_is_stale,
    playbook_payload,
    revision_card,
    save_lab,
)


def test_clamp_drops_empty():
    assert clamp_update({}) is None
    assert clamp_update("x") is None


def test_clamp_keeps_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    u = clamp_update(
        {
            "mode": "exploit",
            "instructions": "Buy strength in legal names with defined-risk brackets.",
            "ready_to_promote": True,
        }
    )
    assert u is not None
    assert u["mode"] == "exploit"
    assert "defined-risk" in u["instructions"]


def test_clamp_patch_keeps_omitted_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "Screen defined-risk debit in legal names.",
            "ready_to_promote": False,
        }
    )
    patch = clamp_update({"ready_to_promote": True})
    assert patch is not None
    assert patch["instructions"] == "Screen defined-risk debit in legal names."
    assert patch["ready_to_promote"] is True
    assert patch["mode"] == "explore"


def test_new_instructions_replace_the_notebook(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "Old slogans.",
            "ready_to_promote": False,
        }
    )
    patch = clamp_update({"instructions": "Defined-risk debit. Size vs the envelope."})
    assert patch is not None
    assert patch["instructions"] == "Defined-risk debit. Size vs the envelope."


def test_paper_may_take_new_risk_without_playbook(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    assert live_new_risk_allowed() is True


def test_live_new_risk_does_not_wait_on_promote(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    assert live_new_risk_allowed() is True
    assert live_has_promoted() is False


def test_promote_needs_a_graduated_card_not_a_beating_book(monkeypatch, tmp_path):
    """A lucky book no longer unlocks live. An individual card graduates."""
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    save_lab(
        {
            "mode": "exploit",
            "instructions": "Do more of the bracket winners in index names.",
            "do_more": "index winners",
            "stop_doing": "lottery calls",
            "ready_to_promote": True,
            "cards": [
                {
                    "name": "index bracket",
                    "ticket": "market_bracket",
                    "thesis": "index brackets pay",
                    "retire_if": {"sample": 2, "condition": "two losers"},
                }
            ],
        },
        scorecard={"beating_model": False, "edge_usd": -2},
    )
    # Book beating, no graduated card: still no promote.
    monkeypatch.setattr("abcxauto.lab_playbook.card_facts", lambda *_a, **_k: [])
    assert maybe_promote(scorecard={"beating_model": True, "edge_usd": 10}) is None

    monkeypatch.setattr(
        "abcxauto.lab_playbook.card_facts",
        lambda *_a, **_k: [
            {"card": "index bracket", "graduated": True, "resolved": 2, "resolved_pnl": 44.0}
        ],
    )
    live = maybe_promote(scorecard={"beating_model": False, "edge_usd": -2})
    assert live is not None
    assert live["promoted"] is True
    assert live["graduated"] == ["index bracket"]
    assert "fills" in (live.get("note") or "").lower() or "copy" in (live.get("note") or "").lower()
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    assert live_new_risk_allowed() is True
    assert load_live().get("promoted_revision") == load_lab().get("revision")


def test_live_ignores_judgment_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    out = apply_from_judgment(
        {"lab_playbook": {"instructions": "YOLO options", "mode": "explore"}}
    )
    assert out is None
    assert not load_lab()


def test_playbook_facts_compare_write_to_now(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    written = datetime.now(timezone.utc) - timedelta(hours=5)
    save_lab(
        {
            "mode": "explore",
            "instructions": "1 lot debit in legal names.",
            "do_more": "",
            "stop_doing": "",
            "ready_to_promote": False,
        },
        scorecard={"beating_model": False, "edge_usd": -549.0},
    )
    lab = load_lab()
    lab["written_at"] = written.isoformat()
    (tmp_path / "lab.json").write_text(json.dumps(lab), encoding="utf-8")
    facts = playbook_facts({"beating_model": False, "edge_usd": -640.0})
    assert facts["revision"] == 1
    assert facts["ready_to_promote"] is False
    assert facts["at_write_edge"] == -549.0
    assert facts["now_edge"] == -640.0
    assert facts["age_h"] is not None
    assert 4.5 <= float(facts["age_h"]) <= 5.5
    age = playbook_age_hours(lab, now=written + timedelta(hours=2))
    assert age == 2.0
    assert playbook_is_stale(lab, now=written + timedelta(hours=2)) is True
    assert playbook_is_stale(lab, now=written + timedelta(minutes=10)) is False
    assert "stale" not in facts


def _flush_card(when_on: str = ">=6% earnings-miss gap") -> dict:
    return {
        "name": "mega-cap earnings-flush bounce",
        "thesis": "gap retrace after an earnings miss",
        "when_on": when_on,
        "scan": "most_active + top_losers",
        "shape": "LONG STK market_bracket",
        "invalidation": "stop through opening low",
        "status": "testing",
        "retire_if": {"sample": 8, "condition": "hit rate below 40%"},
    }


def test_playbook_age_follows_the_live_card_not_a_diary_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        {
            "mode": "explore",
            "instructions": "Explore the flush card.",
            "types": {"market_bracket": {"cards": [_flush_card()]}},
            "ready_to_promote": False,
        }
    )
    lab = load_lab()
    card_clock = datetime(2026, 8, 20, 16, 4, tzinfo=timezone.utc)
    lab["written_at"] = datetime(2026, 8, 25, 15, 14, tzinfo=timezone.utc).isoformat()
    lab["types"]["market_bracket"]["cards"][0]["written_at"] = card_clock.isoformat()
    (tmp_path / "lab.json").write_text(json.dumps(lab), encoding="utf-8")
    now = datetime(2026, 8, 25, 15, 24, tzinfo=timezone.utc)
    age = playbook_age_hours(lab, now=now)
    assert age is not None
    assert age > 24.0
    assert playbook_is_stale(lab, now=now) is True


def test_playbook_age_follows_the_newest_testing_card(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    sibling = dict(_flush_card())
    sibling["name"] = "large-cap 3pct gap hold"
    sibling["when_on"] = ">=3% open gap"
    save_lab(
        {
            "mode": "explore",
            "instructions": "Two cards.",
            "types": {"market_bracket": {"cards": [_flush_card(), sibling]}},
            "ready_to_promote": False,
        }
    )
    lab = load_lab()
    lab["types"]["market_bracket"]["cards"][0]["written_at"] = (
        datetime(2026, 8, 20, 16, 4, tzinfo=timezone.utc).isoformat()
    )
    lab["types"]["market_bracket"]["cards"][1]["written_at"] = (
        datetime(2026, 8, 25, 16, 9, tzinfo=timezone.utc).isoformat()
    )
    (tmp_path / "lab.json").write_text(json.dumps(lab), encoding="utf-8")
    now = datetime(2026, 8, 25, 16, 32, tzinfo=timezone.utc)
    age = playbook_age_hours(lab, now=now)
    assert age is not None
    assert age < 1.0
    assert playbook_is_stale(lab, now=now) is False


def test_save_lab_appends_scored_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "First card.",
            "types": {"market_bracket": {"cards": [_flush_card()]}},
            "ready_to_promote": False,
            "lots_at_write": ["SPY 260918C500 x1"],
        },
        scorecard={"beating_model": False, "edge_usd": -100.0},
    )
    diary = save_lab(
        {
            "mode": "explore",
            "instructions": "Same book, 10:50 rescan — no trigger.",
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            **_flush_card(),
                            "note": "10:50 rescan none >=6%",
                            "evidence": {"scan": "deepest ALB -3.8%"},
                        }
                    ]
                }
            },
            "ready_to_promote": False,
        },
        scorecard={"beating_model": False, "edge_usd": -90.0},
    )
    assert diary.get("revision_held") is True
    assert load_lab()["revision"] == 1
    assert [row["revision"] for row in load_lab()["ledger"]] == [1]
    save_lab(
        {
            "mode": "explore",
            "instructions": "Widen the gap floor.",
            "types": {"market_bracket": {"cards": [_flush_card(">=4% earnings-miss gap")]}},
            "ready_to_promote": False,
        },
        scorecard={"beating_model": False, "edge_usd": -80.0},
    )
    lab = load_lab()
    assert lab["revision"] == 2
    assert [row["revision"] for row in lab["ledger"]] == [1, 2]
    assert lab["ledger"][0]["edge_usd"] == -100.0
    assert lab["ledger"][0]["closed_edge"] == -80.0
    assert lab["ledger"][1]["edge_usd"] == -80.0
    assert lab["ledger"][1].get("closed_edge") is None
    assert "instructions" not in lab["ledger"][0]
    assert "instructions" not in revision_card(1)
    assert revision_card(1)["lots_at_write"] == ["SPY 260918C500 x1"]
    facts = playbook_facts({"edge_usd": -90.0, "beating_model": False})
    assert [row["revision"] for row in facts["ledger"]] == [1, 2]
    block = format_block()
    assert "instructions:" not in block
    assert "playbook rev=" not in block
    assert "write_lab_playbook to set" not in block
    assert "notebook" in block.lower()
    assert "playbook tool" in block
    assert "stale=" not in block
    payload = playbook_payload()
    assert ">=4% earnings-miss gap" in payload["tree"]
    assert int(payload["current"]["instructions_n"] or 0) > 0
    assert ">=4% earnings-miss gap" in playbook_payload(full=True)["tree"]
    old = playbook_payload(1)["revision"]
    assert old["edge_usd"] == -100.0
    assert old["closed_edge"] == -80.0
    assert "instructions" not in old
    assert "First card" not in json.dumps(old)


def test_save_lab_holds_revision_when_only_the_look_diary_changes(
    tmp_path, monkeypatch
):
    from abcxauto.lab_playbook import book_fingerprint

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    first = save_lab(
        {
            "mode": "explore",
            "instructions": "Explore the flush card.",
            "types": {"market_bracket": {"cards": [_flush_card()]}},
            "ready_to_promote": False,
        },
        scorecard={"beating_model": False, "edge_usd": -12.0},
    )
    assert first["revision"] == 1
    assert first.get("revision_held") is not True
    out = apply_from_judgment(
        {
            "lab_playbook": {
                "mode": "explore",
                "instructions": "10:50 rescan. Still no >=6% flush.",
                "types": {
                    "market_bracket": {
                        "note": "flat; rescan losers",
                        "cards": [
                            {
                                **_flush_card(),
                                "note": "10:50 none >=6%",
                                "evidence": {
                                    "scan": "ALB -3.8%",
                                    "news": "only DKS own miss",
                                },
                                "next_look_s": 300,
                            }
                        ],
                    }
                },
            }
        }
    )
    assert out is not None
    assert out.get("revision_held") is True
    assert out.get("revision") == 1
    lab = load_lab()
    assert lab["revision"] == 1
    assert lab.get("written_at") == first.get("written_at")
    assert [row["revision"] for row in lab["ledger"]] == [1]
    assert lab.get("instructions") == first.get("instructions")
    assert "10:50 rescan" not in (lab.get("instructions") or "")
    assert book_fingerprint(first) == book_fingerprint(lab)
    assert "revision_held" not in lab


def test_playbook_payload_score_is_now_not_the_write_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        {
            "mode": "explore",
            "instructions": "Defined-risk debit in legal names.",
            "do_more": "verticals",
            "stop_doing": "0-7 DTE lottery",
            "ready_to_promote": False,
            "lots_at_write": ["XLF 260828C58.5 x1 -41%"],
        },
        scorecard={"beating_model": False, "edge_usd": -550.0},
    )
    monkeypatch.setattr(
        "abcxauto.scorecard.compute_scorecard",
        lambda **_k: {
            "beating_model": False,
            "edge_usd": -1000.0,
            "net_liquidation": 35000.0,
        },
    )
    payload = playbook_payload()
    assert payload["score"]["at_write_edge"] == -550.0
    assert payload["score"]["now_edge"] == -1000.0
    assert payload["facts"]["now_edge"] == -1000.0
    assert payload["facts"]["at_write_edge"] == -550.0
    assert payload["score"]["lots_at_write"] == ["XLF 260828C58.5 x1 -41%"]
    assert "Defined-risk debit" in payload["tree"]
    assert "XLF 260828C58.5" in payload["score"]["lots_at_write"][0]
    assert "playbook rev=" not in format_block()


def test_playbook_score_includes_clerk_halt_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: type("C", (), {"daily_loss_limit_pct": 25.0, "is_paper": True})(),
    )
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": False, "halt_reason": "", "halt_kind": ""})(),
    )
    save_lab(
        {
            "mode": "explore",
            "instructions": "Notes.",
            "ready_to_promote": False,
        },
        scorecard={"beating_model": False, "edge_usd": -1.0},
    )
    monkeypatch.setattr(
        "abcxauto.scorecard.compute_scorecard",
        lambda **_k: {
            "net_liquidation": 40_000.0,
            "ibkr_daily_pnl": -100.0,
            "edge_usd": -1.0,
        },
    )
    payload = playbook_payload()
    assert payload["score"]["clerk_halted"] is False
    assert payload["score"]["daily_loss_limit_pct"] == 25.0
    assert payload["score"]["halt_trips_at_usd"] == -10000.0
    assert payload["score"]["ibkr_day_vs_halt"] == 9900.0


def test_new_notebook_does_not_need_basis_or_evidence(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import grounding_error

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    raw = {
        "instructions": "Prefer debit verticals on index ETFs.",
        "mode": "explore",
    }
    assert grounding_error(raw, tool_trace=[]) == ""
    assert grounding_error("x", tool_trace=[]) != ""


def test_patch_does_not_require_new_research(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import grounding_error

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "Screen defined-risk debit in legal names.",
            "do_more": "size",
            "stop_doing": "lottery",
            "ready_to_promote": False,
            "basis": ["debit_vertical"],
            "evidence": "prior card",
        }
    )
    assert grounding_error({"do_more": "size to envelope"}, tool_trace=[]) == ""


def test_save_lab_drops_dead_ceremony_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "Defined-risk debit.",
            "do_more": "harvest QQQ",
            "stop_doing": "lottery",
            "basis": ["debit_vertical"],
            "evidence": "prior card",
            "research_tools": ["scan"],
            "ready_to_promote": False,
        }
    )
    lab = load_lab()
    assert "do_more" not in lab
    assert "stop_doing" not in lab
    assert "basis" not in lab
    assert "evidence" not in lab
    assert "research_tools" not in lab
    assert lab["instructions"] == "Defined-risk debit."


def test_clear_lab_drops_standing_essay(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "1 lot forever.",
            "do_more": "clone XLE",
            "stop_doing": "",
            "ready_to_promote": False,
        }
    )
    from abcxauto.lab_playbook import clear_lab

    state = clear_lab(reason="operator start notebook")
    assert state["revision"] == 0
    assert state["instructions"] == ""
    assert load_lab()["instructions"] == ""
    assert "none" in format_block()


def test_write_rejects_floors_live_sleeve_keeps_notes(tmp_path, monkeypatch):
    """Architecture: notebook cannot loosen floors, switch live, or set a sleeve."""
    from abcxauto.config import get_config
    from abcxauto.lab_playbook import gate_rejects

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    before_mode = get_config().trading_mode
    before_budget = get_config().trading_budget_usd
    before_floors = bool(getattr(get_config(), "sizing_floors", False))

    raw = {
        "instructions": "Prefer debit verticals on index ETFs.",
        "mode": "explore",
        "trading_mode": "live",
        "sizing_floors": False,
        "trading_budget_usd": 50_000,
    }
    rejected = gate_rejects(raw)
    assert "trading_mode" in rejected
    assert "sizing_floors" in rejected
    assert "trading_budget_usd" in rejected

    out = apply_from_judgment({"lab_playbook": raw})
    assert out is not None
    assert "Prefer debit verticals" in (out.get("instructions") or "")
    assert "trading_mode" in (out.get("rejected") or {})
    assert "sizing_floors" in (out.get("rejected") or {})
    assert "trading_budget_usd" in (out.get("rejected") or {})
    lab = load_lab()
    assert lab["instructions"] == "Prefer debit verticals on index ETFs."
    assert "trading_mode" not in lab
    assert "sizing_floors" not in lab
    assert "trading_budget_usd" not in lab
    assert get_config().trading_mode == before_mode
    assert get_config().trading_budget_usd == before_budget
    assert bool(getattr(get_config(), "sizing_floors", False)) == before_floors


def test_write_gate_only_payload_rejected_without_saving(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    out = apply_from_judgment(
        {"lab_playbook": {"trading_mode": "live", "trading_budget_usd": 1}}
    )
    assert out is not None
    assert out.get("status") == "rejected"
    assert "trading_mode" in (out.get("rejected") or {})
    assert not load_lab().get("instructions")


def test_write_strips_half_pct_gate_when_floors_off(tmp_path, monkeypatch):
    """0.5% was never a send gate. Notebook cannot persist it as GATES/floor law."""
    from abcxauto.config import get_config

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    assert bool(getattr(get_config(), "sizing_floors", False)) is False

    prose = (
        "Prefer debit verticals on index ETFs.\n"
        "GATES: 0.5% / floor 0.5% NL\n"
        "A 0.5% bounce is notes, not a gate.\n"
        "floor 0.5% NL\n"
        "Size vs the envelope."
    )
    out = apply_from_judgment(
        {"lab_playbook": {"instructions": prose, "mode": "explore"}}
    )
    assert out is not None
    inst = load_lab()["instructions"]
    assert "Prefer debit verticals" in inst
    assert "Size vs the envelope." in inst
    assert "0.5% bounce" in inst
    assert "GATES: 0.5%" not in inst
    assert "floor 0.5% NL" not in inst
    assert "invented_pct_gate" in (out.get("rejected") or {})

    only_fake = apply_from_judgment(
        {
            "lab_playbook": {
                "instructions": "GATES: 0.5%\nfloor 0.5% NL",
                "mode": "explore",
            }
        }
    )
    assert only_fake is not None
    assert only_fake.get("status") == "rejected"
    assert "invented_pct_gate" in (only_fake.get("rejected") or {})
    assert "GATES: 0.5%" not in (load_lab().get("instructions") or "")
    assert "floor 0.5% NL" not in (load_lab().get("instructions") or "")


def test_write_keeps_pct_gate_only_when_floors_on_and_n_is_knob(tmp_path, monkeypatch):
    """Same GATES/floor lines are law only when floors are ON and N is the live knob."""
    from abcxauto.config import get_config, update_risk_config

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    update_risk_config(sizing_floors=True, max_risk_per_trade_pct=0.5, persist=False)
    cfg = get_config()
    assert cfg.sizing_floors is True
    assert abs(float(cfg.max_risk_per_trade_pct) - 0.5) < 1e-6

    prose = "Prefer debit verticals.\nGATES: 0.5%\nfloor 0.5% NL"
    out = apply_from_judgment(
        {"lab_playbook": {"instructions": prose, "mode": "explore"}}
    )
    assert out is not None
    inst = load_lab()["instructions"]
    assert "GATES: 0.5%" in inst
    assert "floor 0.5% NL" in inst
    assert "invented_pct_gate" not in (out.get("rejected") or {})

    update_risk_config(max_risk_per_trade_pct=25.0, persist=False)
    stale = apply_from_judgment(
        {"lab_playbook": {"instructions": prose, "mode": "explore"}}
    )
    assert stale is not None
    inst = load_lab()["instructions"]
    assert "Prefer debit verticals." in inst
    assert "GATES: 0.5%" not in inst
    assert "floor 0.5% NL" not in inst
    assert "invented_pct_gate" in (stale.get("rejected") or {})


def test_new_risk_until_prose_stays_notes_not_a_clock(tmp_path, monkeypatch):
    """Not a screen-window text parser — prose is notebook, clerk parks."""
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    prose = "No new risk until 10:30 ET. Park until open."
    out = apply_from_judgment(
        {"lab_playbook": {"instructions": prose, "mode": "explore"}}
    )
    assert out is not None
    assert load_lab()["instructions"] == prose
    assert "rejected" not in (out or {})
    # Wake line still only shows score glance — not the essay as an order.
    block = format_block()
    assert "10:30" not in block
    assert "playbook rev=" not in block
    assert "notebook" in block.lower()
    assert "playbook tool" in block
    # Playbook write must not arm a sit-clock.
    from abcxauto.park_clock import load_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    before = load_alarm()
    apply_from_judgment(
        {"lab_playbook": {"instructions": prose + " Again.", "mode": "explore"}}
    )
    after = load_alarm()
    assert after.wake_at == before.wake_at
    assert after.set_at == before.set_at


def test_playbook_revision_strips_old_essay_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    (tmp_path / "lab.json").write_text(
        json.dumps(
            {
                "mode": "explore",
                "instructions": "Live notes.",
                "revision": 2,
                "written_at": datetime.now(timezone.utc).isoformat(),
                "ledger": [
                    {
                        "revision": 1,
                        "edge_usd": -100.0,
                        "closed_edge": -80.0,
                        "instructions": "Hold forbidden without resting exit.",
                        "do_more": "cover short then sell long",
                    },
                    {
                        "revision": 2,
                        "edge_usd": -80.0,
                        "instructions": "Live notes.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    old = playbook_payload(1)["revision"]
    assert old["closed_edge"] == -80.0
    assert "instructions" not in old
    assert "do_more" not in old
    assert "Hold forbidden" not in json.dumps(old)
    assert playbook_payload()["tree"] == "Live notes."


def test_card_next_look_s_is_not_a_clerk_clock(tmp_path, monkeypatch):
    from abcxauto.lab_playbook import playbook_next_look_s

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    apply_from_judgment(
        {
            "lab_playbook": {
                "types": {
                    "market_bracket": {
                        "cards": [
                            {
                                "name": "flush bounce",
                                "thesis": "gap retrace",
                                "retire_if": {
                                    "sample": 8,
                                    "condition": "hit rate below 40%",
                                },
                                "next_look_s": 5,
                            }
                        ]
                    }
                }
            }
        }
    )
    card = load_lab()["types"]["market_bracket"]["cards"][0]
    assert "next_look_s" not in card
    assert playbook_next_look_s() is None
