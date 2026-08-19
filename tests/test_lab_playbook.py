"""Paper lab playbook: Grok writes instructions; live only follows a promote."""

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
            "instructions": "Hunt defined-risk debit in legal names.",
            "ready_to_promote": False,
        }
    )
    patch = clamp_update({"ready_to_promote": True})
    assert patch is not None
    assert patch["instructions"] == "Hunt defined-risk debit in legal names."
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


def test_paper_may_hunt_without_playbook(monkeypatch, tmp_path):
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
            "instructions": "Hunt defined-risk debit in legal names.",
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
