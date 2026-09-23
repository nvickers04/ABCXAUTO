"""Prompt-hash continuity: refresh system once, then id-only creates."""

from __future__ import annotations

import json
from types import SimpleNamespace

from abcxauto import chat_cursor
from abcxauto.llm import chat_create_kwargs


def _g() -> SimpleNamespace:
    return SimpleNamespace(
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
        model_params={},
    )


def test_old_hash_plus_id_passes_previous_and_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cursor, "_STATE_DIR", tmp_path)
    path = chat_cursor.cursor_path()
    path.write_text(
        json.dumps(
            {
                "previous_response_id": "resp_old_thread",
                "prompt_hash": "deadbeef" * 8,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert chat_cursor.load_previous_response_id() == "resp_old_thread"
    assert chat_cursor.prompt_needs_refresh() is True

    msgs = ["system-current"]
    kw = chat_create_kwargs(
        _g(),
        messages=msgs,
        tools=["book"],
        previous_response_id=chat_cursor.load_previous_response_id(),
        refresh_system=True,
    )
    assert kw["previous_response_id"] == "resp_old_thread"
    assert kw["messages"] == msgs
    assert kw["store_messages"] is True

    chat_cursor.save_previous_response_id("resp_after_refresh")
    assert chat_cursor.load_previous_response_id() == "resp_after_refresh"
    assert chat_cursor.load_prompt_hash() == chat_cursor.system_prompt_hash()
    assert chat_cursor.prompt_needs_refresh() is False

    next_kw = chat_create_kwargs(
        _g(),
        messages=["should-not-send"],
        tools=["book"],
        previous_response_id=chat_cursor.load_previous_response_id(),
    )
    assert next_kw["previous_response_id"] == "resp_after_refresh"
    assert "messages" not in next_kw


def test_matching_hash_passes_id_omits_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cursor, "_STATE_DIR", tmp_path)
    chat_cursor.save_previous_response_id("resp_same_prompt")
    assert chat_cursor.load_prompt_hash() == chat_cursor.system_prompt_hash()
    assert chat_cursor.prompt_needs_refresh() is False

    kw = chat_create_kwargs(
        _g(),
        messages=["should-not-send"],
        tools=["book"],
        previous_response_id=chat_cursor.load_previous_response_id(),
    )
    assert kw["previous_response_id"] == "resp_same_prompt"
    assert "messages" not in kw


def test_missing_hash_with_id_needs_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cursor, "_STATE_DIR", tmp_path)
    path = chat_cursor.cursor_path()
    path.write_text(
        json.dumps({"previous_response_id": "resp_legacy"}) + "\n",
        encoding="utf-8",
    )
    assert chat_cursor.load_prompt_hash() == ""
    assert chat_cursor.prompt_needs_refresh() is True

    kw = chat_create_kwargs(
        _g(),
        messages=["system-now"],
        previous_response_id="resp_legacy",
        refresh_system=True,
    )
    assert kw["previous_response_id"] == "resp_legacy"
    assert kw["messages"] == ["system-now"]


def test_empty_save_is_noop_never_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_cursor, "_STATE_DIR", tmp_path)
    chat_cursor.save_previous_response_id("resp_keep")
    before = chat_cursor.cursor_path().read_text(encoding="utf-8")
    chat_cursor.save_previous_response_id("")
    chat_cursor.save_previous_response_id(None)
    after = chat_cursor.cursor_path().read_text(encoding="utf-8")
    assert after == before
    assert chat_cursor.load_previous_response_id() == "resp_keep"


def test_new_chat_stale_hash_then_matching(tmp_path, monkeypatch):
    """Old hash + id → previous_response_id and system messages; next save → id only."""
    from abcxauto.brain import _new_chat, _persist_response_id

    monkeypatch.setattr(chat_cursor, "_STATE_DIR", tmp_path)
    chat_cursor.cursor_path().write_text(
        json.dumps(
            {
                "previous_response_id": "resp_stale",
                "prompt_hash": "0" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    captured: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            captured.clear()
            captured.update(k)
            return SimpleNamespace(id="resp_refreshed")

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=None,
        _wake_n=0,
        model_params={},
    )
    _new_chat(g, session="regular")
    assert captured.get("previous_response_id") == "resp_stale"
    assert "messages" in captured
    assert captured.get("store_messages") is True

    _persist_response_id(SimpleNamespace(id="resp_refreshed"))
    assert chat_cursor.load_prompt_hash() == chat_cursor.system_prompt_hash()

    g.chat = None
    _new_chat(g, session="regular")
    assert captured.get("previous_response_id") == "resp_refreshed"
    assert "messages" not in captured
