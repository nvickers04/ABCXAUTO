"""Paper lab playbook: Grok writes instructions; live only follows a promote."""

import json
from datetime import datetime, timedelta, timezone

from abcxauto.lab_playbook import (
    apply_from_judgment,
    clamp_update,
    live_has_promoted,
    live_new_risk_allowed,
    load_lab,
    load_live,
    maybe_promote,
    playbook_age_hours,
    playbook_facts,
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
            "do_more": "winners",
            "stop_doing": "chop",
            "ready_to_promote": True,
        }
    )
    assert u is not None
    assert u["mode"] == "exploit"
    assert "defined-risk" in u["instructions"]


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
