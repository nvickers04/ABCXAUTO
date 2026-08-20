"""Paper lab playbook: Grok writes instructions; live only follows a promote."""

import json
from datetime import datetime, timedelta, timezone

from abcxauto.lab_playbook import (
    apply_from_judgment,
    clamp_update,
    empty_type_catalog,
    format_block,
    live_has_promoted,
    live_new_risk_allowed,
    load_lab,
    load_live,
    maybe_promote,
    notebook_text,
    playbook_age_hours,
    playbook_facts,
    playbook_is_stale,
    playbook_payload,
    playbook_type_keys,
    render_playbook_tree,
    revision_card,
    save_lab,
)


def test_clamp_drops_empty():
    assert clamp_update({}) is None
    assert clamp_update("x") is None


def test_clamp_keeps_type_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    u = clamp_update(
        {
            "mode": "exploit",
            "types": {},
            "ready_to_promote": True,
        }
    )
    assert u is not None
    assert u["mode"] == "exploit"
    assert "bracket" in u["types"]
    assert u["types"]["bracket"]["strategies"] == []
    assert "TYPE bracket" in u["instructions"]


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


def test_new_strategy_refines_under_existing_type(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(clamp_update({"types": {}, "mode": "explore"}))
    patch = clamp_update(
        {
            "types": {
                "vertical_spread": {
                    "strategies": [
                        {
                            "name": "debit_call",
                            "when_on": "defined-risk debit",
                            "tool_order": "quote, option_chain, send",
                            "ticket_shape": "vertical_spread long below short for calls",
                            "invalidation": "thesis gone",
                        }
                    ]
                }
            }
        }
    )
    assert patch is not None
    names = [s["name"] for s in patch["types"]["vertical_spread"]["strategies"]]
    assert names == ["debit_call"]
    assert "TYPE vertical_spread" in patch["instructions"]
    assert "debit_call" in patch["instructions"]
    i_type = patch["instructions"].index("TYPE vertical_spread")
    i_child = patch["instructions"].index("debit_call")
    assert i_type < i_child


def test_paper_may_take_new_risk_without_playbook(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    assert live_new_risk_allowed() is True


def test_live_blocks_new_risk_until_promote(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    assert live_new_risk_allowed() is False
    assert live_has_promoted() is False


def test_promote_requires_beating_and_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    save_lab(
        {
            "mode": "exploit",
            "instructions": "Do more of the bracket winners in index names.",
            "do_more": "index winners",
            "stop_doing": "lottery calls",
            "ready_to_promote": True,
        },
        scorecard={"beating_model": False, "edge_usd": -2},
    )
    assert maybe_promote(scorecard={"beating_model": False}) is None
    live = maybe_promote(scorecard={"beating_model": True, "edge_usd": 10})
    assert live is not None
    assert live["promoted"] is True
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


def test_save_lab_appends_scored_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    save_lab(
        {
            "mode": "explore",
            "instructions": "First card.",
            "do_more": "try size",
            "stop_doing": "lottery",
            "ready_to_promote": False,
            "lots_at_write": ["SPY 260918C500 x1"],
        },
        scorecard={"beating_model": False, "edge_usd": -100.0},
    )
    save_lab(
        {
            "mode": "explore",
            "instructions": "Second card sizes to the envelope.",
            "do_more": "size",
            "stop_doing": "1 lot",
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
    assert "ledger:" in block
    assert "notebook: playbook tool" in block
    assert "stale=" not in block
    payload = playbook_payload()
    assert "Second card" in payload["current"]["instructions"]
    assert payload["current"]["instructions_n"] == len("Second card sizes to the envelope.")
    assert "Second card" in playbook_payload(full=True)["current"]["instructions"]
    old = playbook_payload(1)["revision"]
    assert old["edge_usd"] == -100.0
    assert old["closed_edge"] == -80.0
    assert "instructions" not in old
    assert "First card" not in json.dumps(old)


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
    assert "Defined-risk debit" in payload["current"]["instructions"]
    assert "XLF 260828C58.5" in format_block()


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
        "types": {},
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
    assert "TYPE bracket" in (out.get("instructions") or "")
    assert "trading_mode" in (out.get("rejected") or {})
    assert "sizing_floors" in (out.get("rejected") or {})
    assert "trading_budget_usd" in (out.get("rejected") or {})
    lab = load_lab()
    assert "TYPE bracket" in lab["instructions"]
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


<<<<<<< HEAD
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
    """Not a screen-window text parser — prose is notebook, set_wake parks."""
=======
def test_new_risk_until_prose_is_not_the_book(tmp_path, monkeypatch):
    """Wake clocks / diary are not the notebook; set_wake parks."""
>>>>>>> 209a5d0 (Persist the lab playbook as a TYPE tree Grok can fill.)
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    prose = "No new risk until 10:30 ET. Park until open."
    out = apply_from_judgment(
        {"lab_playbook": {"instructions": prose, "mode": "explore"}}
    )
    assert out is not None
    assert out.get("status") == "rejected"
    assert "diary" in (out.get("rejected") or {})
    assert not load_lab().get("types")
    from abcxauto.wake_bus import load_alarm

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    before = load_alarm()
    apply_from_judgment({"lab_playbook": {"types": {}, "mode": "explore"}})
    after = load_alarm()
    assert after.wake_at == before.wake_at
    assert after.set_at == before.set_at
    block = format_block()
    assert "10:30" not in block
    assert "notebook: playbook tool" in block


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
    assert playbook_payload()["current"]["instructions"] == "Live notes."


def test_playbook_type_keys_skip_knobs_and_undefined_risk():
    keys = playbook_type_keys()
    assert "bracket" in keys
    assert "market_bracket" in keys
    assert "trailing_stop" in keys
    assert "modify_stop" in keys
    assert "close_option" in keys
    assert "market_order" in keys
    assert "limit_order" in keys
    assert "stop_order" in keys
    assert "buy_option" in keys
    assert "modify_target" in keys
    assert "cancel_order" in keys
    assert "calendar_spread" in keys
    assert "iron_condor" in keys
    assert "cash_secured_put" in keys
    assert "vertical_spread" in keys
    assert "protective_put" in keys
    assert "set_risk" not in keys
    assert "self_tune" not in keys
    assert "ratio_spread" not in keys
    assert "jade_lizard" not in keys
    catalog = empty_type_catalog()
    assert set(catalog) == set(keys)
    tree = render_playbook_tree(catalog)
    assert "NVDA" not in tree
    assert "SPY" not in tree
    assert "AAPL" not in tree


def test_empty_catalog_write_is_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    out = apply_from_judgment({"lab_playbook": {"types": {}, "mode": "explore"}})
    assert out is not None
    assert out.get("status") != "rejected"
    lab = load_lab()
    keys = set(playbook_type_keys())
    assert set(lab["types"]) == keys
    for name, row in lab["types"].items():
        assert row["strategies"] == []
        assert "defined_risk" in row
        assert "open_shape" in row
        assert "close_tp_sl" in row
    assert "ratio_spread" not in lab["types"]
    assert "jade_lizard" not in lab["types"]
    tree = notebook_text(lab)
    assert "TYPE bracket" in tree
    assert "TYPE vertical_spread" in tree
    assert "NVDA" not in tree
    payload = playbook_payload()
    assert "TYPE bracket" in payload["current"]["instructions"]
    assert payload["current"]["types"]["bracket"]["strategies"] == []


def test_strategy_under_unknown_type_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    out = apply_from_judgment(
        {
            "lab_playbook": {
                "types": {
                    "mean_reversion": {
                        "strategies": [
                            {
                                "name": "fade",
                                "when_on": "overextended",
                                "tool_order": "quote, send",
                                "ticket_shape": "invented",
                                "invalidation": "none",
                            }
                        ]
                    }
                },
                "mode": "explore",
            }
        }
    )
    assert out is not None
    assert out.get("status") == "rejected"
    assert "unknown_type" in (out.get("rejected") or {})
    assert not load_lab().get("types")

    skipped = apply_from_judgment(
        {
            "lab_playbook": {
                "types": {
                    "ratio_spread": {
                        "strategies": [{"name": "ratio", "when_on": "x"}]
                    }
                },
                "mode": "explore",
            }
        }
    )
    assert skipped.get("status") == "rejected"
    assert "unknown_type" in (skipped.get("rejected") or {})


def test_gates_half_pct_stripped_or_rejected(tmp_path, monkeypatch):
    """0.5% was never a send gate. Notebook cannot persist it as GATES/floor law."""
    from abcxauto.config import get_config

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    assert bool(getattr(get_config(), "sizing_floors", False)) is False

    catalog = empty_type_catalog()
    catalog["vertical_spread"]["strategies"] = [
        {
            "name": "debit_call",
            "when_on": "defined-risk debit",
            "tool_order": "quote, option_chain, send",
            "ticket_shape": "vertical_spread debit",
            "invalidation": "GATES: 0.5% / floor 0.5% NL",
        }
    ]
    out = apply_from_judgment(
        {"lab_playbook": {"types": catalog, "mode": "explore"}}
    )
    assert out is not None
    lab = load_lab()
    tree = lab["instructions"]
    assert "TYPE vertical_spread" in tree
    assert "debit_call" in tree
    assert "GATES: 0.5%" not in tree
    assert "floor 0.5% NL" not in json.dumps(lab.get("types") or {})
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


def test_rendered_tree_shows_type_then_children(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    types = empty_type_catalog()
    types["vertical_spread"]["strategies"] = [
        {
            "name": "debit_call",
            "when_on": "defined-risk debit when the chain is live",
            "tool_order": "quote, option_chain, send",
            "ticket_shape": "vertical_spread long_strike below short_strike for calls",
            "invalidation": "thesis gone or protection missing",
        }
    ]
    tree = render_playbook_tree(types)
    assert "TYPE bracket" in tree
    assert "TYPE vertical_spread" in tree
    i_type = tree.index("TYPE vertical_spread")
    i_child = tree.index("debit_call")
    assert i_type < i_child
    i_bracket = tree.index("TYPE bracket")
    # Child is under its type, not a sibling listed before the TYPE line.
    assert "  - debit_call" in tree

    apply_from_judgment({"lab_playbook": {"types": types, "mode": "explore"}})
    payload = playbook_payload()
    body = payload["current"]["instructions"]
    assert body.index("TYPE vertical_spread") < body.index("debit_call")
    assert i_bracket >= 0


def test_ticker_list_and_diary_rejected_as_whole_book(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    tickers = apply_from_judgment(
        {"lab_playbook": {"instructions": "AAPL MSFT NVDA QQQ IWM", "mode": "explore"}}
    )
    assert tickers.get("status") == "rejected"
    assert "ticker_list" in (tickers.get("rejected") or {})
    diary = apply_from_judgment(
        {"lab_playbook": {"instructions": "nap until the open then journal", "mode": "explore"}}
    )
    assert diary.get("status") == "rejected"
    assert "diary" in (diary.get("rejected") or {}) or "shape" in (diary.get("rejected") or {})


def test_structured_text_maps_onto_types(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    text = (
        "TYPE vertical_spread\n"
        "STRATEGY debit_call\n"
        "when_on: defined-risk debit\n"
        "tool_order: quote, option_chain, send\n"
        "ticket_shape: long_strike below short_strike for calls\n"
        "invalidation: thesis gone\n"
    )
    out = apply_from_judgment(
        {"lab_playbook": {"instructions": text, "mode": "explore"}}
    )
    assert out is not None
    assert out.get("status") != "rejected"
    lab = load_lab()
    names = [s["name"] for s in lab["types"]["vertical_spread"]["strategies"]]
    assert "debit_call" in names
    assert "TYPE bracket" in lab["instructions"]
    assert lab["instructions"].index("TYPE vertical_spread") < lab["instructions"].index(
        "debit_call"
    )


def test_write_keeps_pct_gate_only_when_floors_on_and_n_is_knob(tmp_path, monkeypatch):
    """Same GATES/floor lines are law only when floors are ON and N is the live knob."""
    from abcxauto.config import get_config, update_risk_config

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    update_risk_config(sizing_floors=True, max_risk_per_trade_pct=0.5, persist=False)
    cfg = get_config()
    assert cfg.sizing_floors is True
    assert abs(float(cfg.max_risk_per_trade_pct) - 0.5) < 1e-6

    catalog = empty_type_catalog()
    catalog["vertical_spread"]["strategies"] = [
        {
            "name": "debit_call",
            "when_on": "defined-risk debit",
            "tool_order": "quote, option_chain, send",
            "ticket_shape": "vertical_spread debit",
            "invalidation": "GATES: 0.5%\nfloor 0.5% NL",
        }
    ]
    out = apply_from_judgment(
        {"lab_playbook": {"types": catalog, "mode": "explore"}}
    )
    assert out is not None
    dumped = json.dumps(load_lab().get("types") or {})
    assert "GATES: 0.5%" in dumped
    assert "floor 0.5% NL" in dumped
    assert "invented_pct_gate" not in (out.get("rejected") or {})

    update_risk_config(max_risk_per_trade_pct=25.0, persist=False)
    stale = apply_from_judgment(
        {"lab_playbook": {"types": catalog, "mode": "explore"}}
    )
    assert stale is not None
    dumped = json.dumps(load_lab().get("types") or {})
    assert "debit_call" in dumped
    assert "GATES: 0.5%" not in dumped
    assert "floor 0.5% NL" not in dumped
    assert "invented_pct_gate" in (stale.get("rejected") or {})


def test_default_tool_recipe_stored_not_gated(tmp_path, monkeypatch):
    from abcxauto.brain import agent_tools
    from abcxauto.lab_playbook import grounding_error

    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    raw = {
        "types": {
            "vertical_spread": {
                "defined_risk": True,
                "open_shape": "debit or credit vertical BAG",
                "close_tp_sl": "same strategy + closing_position + limit",
                "default_tool_recipe": ["quote", "option_chain", "send"],
                "strategies": [],
            }
        },
        "mode": "explore",
    }
    assert grounding_error(raw, tool_trace=[]) == ""
    out = apply_from_judgment({"lab_playbook": raw})
    assert out is not None
    assert out.get("status") != "rejected"
    lab = load_lab()
    assert lab["types"]["vertical_spread"]["default_tool_recipe"] == [
        "quote",
        "option_chain",
        "send",
    ]
    tree = lab["instructions"]
    assert "recipe: quote, option_chain, send" in tree
    names = {
        str(getattr(getattr(t, "function", None), "name", None) or getattr(t, "name", "") or "")
        for t in agent_tools(session="regular")
    }
    assert {"quote", "scan", "option_chain", "send", "book"} <= names
