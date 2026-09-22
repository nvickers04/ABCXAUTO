"""Model id + model_params are operator knobs, not a grok-4.6-only code path.

Flipping to grok-4.7 (when xAI publishes the id) is Settings / env /
risk_settings.json. Unknown future chat.create kwargs pass through; an
old SDK TypeError drops them instead of crashing the look. self_tune
cannot overwrite the brain.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.config import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_XHIGH,
    LAUNCH_MODEL_KEYS,
    RESERVED_CHAT_KEYS,
    agent_config_snapshot,
    coerce_model_params,
    get_config,
    launch_model_knobs,
    load_risk_settings,
    risk_settings_path,
    set_agent_knobs,
    update_agent_config,
)
from abcxauto.desk_mode import session_model, session_model_params
from abcxauto.llm import GrokClient, chat_create_kwargs, create_chat, normalize_chat_extras
from abcxauto.self_tune import OPERATOR_DISK_KEYS, apply_self_tune
from abcxauto.thin_rth_kill_look import rth_model_no_xhigh, rth_params_no_xhigh


def test_default_model_is_one_constant():
    assert DEFAULT_MODEL == "grok-4.6"
    assert DEFAULT_MODEL_XHIGH == "grok-4.6-xhigh"
    assert get_config().model == DEFAULT_MODEL
    assert get_config().model_rth == ""
    assert get_config().model_research == ""
    assert get_config().model_params == {}
    assert get_config().model_params_rth == {}
    assert get_config().model_params_research == {}
    assert session_model("regular", get_config()) == DEFAULT_MODEL
    assert session_model_params("regular", get_config()) == {}
    assert session_model_params("premarket", get_config()) == {}
    # xhigh stays a suffix the operator can set; launch does not bake 4.7.
    cfg = SimpleNamespace(
        model=DEFAULT_MODEL_XHIGH, model_rth="", model_research=""
    )
    assert session_model("premarket", cfg) == DEFAULT_MODEL_XHIGH
    knobs = launch_model_knobs(reload=True)
    assert knobs["model"] == DEFAULT_MODEL
    assert knobs["model"] != "grok-4.7"
    assert knobs["model_rth"] == ""
    assert knobs["model_research"] == ""
    assert knobs["model_params"] == {}
    assert knobs["model_params_rth"] == {}
    assert knobs["model_params_research"] == {}


def test_create_chat_downgrades_rejected_xhigh():
    from abcxauto.llm import create_chat

    seen: list[dict] = []

    class _Chat:
        def create(self, **kwargs):
            seen.append(dict(kwargs))
            if kwargs.get("reasoning_effort") == "xhigh":
                raise ValueError(
                    "Invalid reasoning effort: xhigh. Must be one of: ('low', 'high')"
                )
            return {"ok": True}

    out = create_chat(
        SimpleNamespace(chat=_Chat()),
        model="grok-4.7",
        reasoning_effort="xhigh",
    )
    assert out == {"ok": True}
    assert seen[-1]["reasoning_effort"] == "high"


def test_default_desk_create_kwargs_omit_effort():
    """Empty Settings maps send no reasoning_effort; SDK then defaults high."""
    cfg = get_config()
    g = SimpleNamespace(
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        model_params=dict(cfg.model_params or {}),
    )
    kw = chat_create_kwargs(g, messages=["hi"], tools=["book"])
    assert kw["model"] == DEFAULT_MODEL
    assert kw["temperature"] == 0.3
    assert kw["max_tokens"] == 8192
    assert kw["include"] == ["verbose_streaming"]
    assert kw["store_messages"] is True
    assert kw["use_encrypted_content"] is True
    assert "previous_response_id" not in kw
    assert "reasoning_effort" not in kw
    assert "effort" not in kw
    assert "thinking" not in kw


def test_settings_can_select_a_later_model_id():
    """hardcoded grok-4.6 is not the only path."""
    cfg = update_agent_config(
        model="grok-4.7",
        model_rth="grok-4.7",
        model_research="grok-4.7",
        persist=False,
    )
    assert cfg.model == "grok-4.7"
    assert session_model("regular", cfg) == "grok-4.7"
    assert session_model("premarket", cfg) == "grok-4.7"
    client = SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: SimpleNamespace()))
    g = GrokClient(client=client, session="regular")
    assert g.model == "grok-4.7"
    assert rth_model_no_xhigh("grok-4.7-xhigh", enabled=True) == "grok-4.7"


def test_env_model_and_params_load(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_MODEL", "grok-4.7")
    monkeypatch.setenv(
        "ABCXAUTO_MODEL_PARAMS",
        json.dumps({"reasoning_effort": "high", "thinking": True}),
    )
    get_config.cache_clear()
    cfg = get_config()
    assert cfg.model == "grok-4.7"
    assert cfg.model_params["reasoning_effort"] == "high"
    assert cfg.model_params["thinking"] is True


def test_invalid_env_params_are_ignored(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_MODEL_PARAMS", "not-json")
    get_config.cache_clear()
    assert get_config().model_params == {}


def test_model_params_persist_in_risk_settings():
    cfg = update_agent_config(
        model="grok-4.7",
        model_params={"reasoning_effort": "high", "effort": "xhigh"},
    )
    assert cfg.model_params["reasoning_effort"] == "high"
    assert cfg.model_params["effort"] == "xhigh"
    raw = json.loads(risk_settings_path().read_text(encoding="utf-8"))
    assert raw["model"] == "grok-4.7"
    assert raw["model_params"]["effort"] == "xhigh"

    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(risk_settings_path())
    reread = get_config()
    assert reread.model == "grok-4.7"
    assert reread.model_params == {"reasoning_effort": "high", "effort": "xhigh"}


def test_settings_form_accepts_json_string():
    res = set_agent_knobs(
        {"model_params": '{"reasoning_effort":"low","future_knob":2}'},
        persist=True,
    )
    assert res["rejected"] == {}
    assert res["applied"]["model_params"]["future_knob"] == 2
    assert agent_config_snapshot()["model_params"]["reasoning_effort"] == "low"


def test_empty_model_params_clears():
    update_agent_config(model_params={"reasoning_effort": "high"})
    assert update_agent_config(model_params="").model_params == {}
    assert update_agent_config(model_params="{}").model_params == {}


def test_reserved_chat_keys_are_dropped_from_params():
    got = coerce_model_params(
        {
            "model": "hijack",
            "messages": [],
            "tools": [],
            "include": ["nope"],
            "temperature": 1.2,
            "max_tokens": 99,
            "reasoning_effort": "high",
        }
    )
    assert got == {"reasoning_effort": "high"}
    for key in RESERVED_CHAT_KEYS:
        assert key not in got


def test_bad_model_params_are_rejected():
    with pytest.raises(ValueError):
        update_agent_config(model_params="[1,2]")
    with pytest.raises(ValueError):
        update_agent_config(model_params={"bad key": 1})
    res = set_agent_knobs({"model_params": "not-json"})
    assert "model_params" in res["rejected"]
    assert get_config().model_params == {}


def test_chat_create_passes_future_params():
    created: dict = {}

    class _Chat:
        @staticmethod
        def create(**k):
            if "effort" in k or "thinking" in k:
                raise TypeError("unexpected effort/thinking")
            created.update(k)
            return SimpleNamespace()

    g = SimpleNamespace(
        model="grok-4.7",
        temperature=0.3,
        max_tokens=2048,
        model_params={"reasoning_effort": "high", "thinking": True, "effort": "xhigh"},
    )
    kw = chat_create_kwargs(g, messages=["hi"], tools=["book"])
    assert kw["model"] == "grok-4.7"
    assert kw["reasoning_effort"] == "high"
    assert "thinking" not in kw
    assert "effort" not in kw
    assert kw["include"] == ["verbose_streaming"]
    assert kw["store_messages"] is True
    create_chat(SimpleNamespace(chat=_Chat()), **kw)
    assert created["reasoning_effort"] == "high"
    assert created["model"] == "grok-4.7"
    assert created["store_messages"] is True


def test_chat_create_kwargs_previous_response_id_omits_messages():
    g = SimpleNamespace(
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
        model_params={},
    )
    kw = chat_create_kwargs(
        g,
        messages=["should-not-send"],
        tools=["book"],
        previous_response_id="resp_xyz",
    )
    assert kw["previous_response_id"] == "resp_xyz"
    assert kw["store_messages"] is True
    assert "messages" not in kw
    assert kw["tools"] == ["book"]


def test_effort_alias_reaches_sdk_and_unknown_key_does_not_strip_it():
    created: dict = {}

    class _Chat:
        @staticmethod
        def create(**k):
            if "future_unknown" in k:
                raise TypeError("unexpected future_unknown")
            created.update(k)
            return SimpleNamespace(ok=True)

    assert normalize_chat_extras({"effort": "high", "thinking": True}) == {
        "reasoning_effort": "high"
    }
    assert normalize_chat_extras(
        {"reasoning_effort": "high", "effort": "xhigh"}
    ) == {"reasoning_effort": "high"}
    g = SimpleNamespace(
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
        model_params={"effort": "high", "future_unknown": 1},
    )
    kw = chat_create_kwargs(g, messages=["hi"])
    assert kw["reasoning_effort"] == "high"
    assert "effort" not in kw
    chat = create_chat(SimpleNamespace(chat=_Chat()), **kw)
    assert chat.ok is True
    assert created["reasoning_effort"] == "high"
    assert "future_unknown" not in created


def test_chat_create_drops_unknown_kwargs_instead_of_crashing():
    class _Strict:
        def create(
            self,
            model,
            *,
            messages=None,
            temperature=None,
            max_tokens=None,
            include=None,
            tools=None,
        ):
            assert model == "grok-4.7"
            return SimpleNamespace(ok=True)

    g = SimpleNamespace(
        model="grok-4.7",
        temperature=0.2,
        max_tokens=1024,
        model_params={"thinking": True, "effort": "high"},
    )
    kw = chat_create_kwargs(g, messages=["hi"])
    assert "thinking" not in kw
    assert kw["reasoning_effort"] == "high"
    chat = create_chat(SimpleNamespace(chat=_Strict()), **kw)
    assert chat.ok is True


def test_self_tune_cannot_overwrite_brain_or_params():
    before = get_config()
    update_agent_config(
        model="grok-4.6",
        model_rth="rth-brain",
        model_research="research-brain",
        model_params={"reasoning_effort": "high"},
        model_params_rth={"effort": "low"},
        model_params_research={"effort": "xhigh"},
        persist=True,
    )
    out = apply_self_tune(
        {
            "model": "hijack",
            "model_rth": "hijack-rth",
            "model_research": "hijack-research",
            "model_params": {"reasoning_effort": "low"},
            "model_params_rth": {"effort": "xhigh"},
            "model_params_research": {"effort": "low"},
            "defined_risk_only": False,
        },
        persist=True,
    )
    rejected = out.get("rejected") or {}
    for key in (
        "model",
        "model_rth",
        "model_research",
        "model_params",
        "model_params_rth",
        "model_params_research",
    ):
        assert key in OPERATOR_DISK_KEYS
        assert key in rejected
        assert "operator disk" in rejected[key]
    cfg = get_config()
    assert cfg.model == "grok-4.6"
    assert cfg.model_rth == "rth-brain"
    assert cfg.model_research == "research-brain"
    assert cfg.model_params == {"reasoning_effort": "high"}
    assert cfg.model_params_rth == {"effort": "low"}
    assert cfg.model_params_research == {"effort": "xhigh"}
    assert cfg.defined_risk_only is True
    assert before.defined_risk_only is True


def test_brain_fingerprint_includes_params(monkeypatch):
    from abcxauto.pro_engine import ProEngine

    class _Box:
        model = "grok-4.6"
        model_rth = ""
        model_research = ""
        model_params: dict = {}
        temperature = 0.3
        max_tokens = 8192

    box = _Box()
    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: box)
    first = ProEngine._brain_fingerprint()
    box.model_params = {"reasoning_effort": "high"}
    assert ProEngine._brain_fingerprint() != first
    box.model = "grok-4.7"
    assert ProEngine._brain_fingerprint()[0] == "grok-4.7"


def test_launch_helpers_read_disk_not_hardcoded_default(monkeypatch):
    """DESK / CloudAgent launch reloads risk_settings.json — not grok-4.6 only."""
    update_agent_config(
        model="grok-4.7",
        model_rth="grok-4.7",
        model_research="grok-4.7-xhigh",
        model_params={"effort": "xhigh", "thinking": True},
        model_params_rth={"effort": "low"},
        model_params_research={"effort": "xhigh", "thinking": True},
        persist=True,
    )
    from abcxauto import config as cfg_mod
    from abcxauto import think_stream as ts

    cfg_mod._runtime_overrides.clear()
    get_config.cache_clear()
    knobs = launch_model_knobs(reload=True)
    assert set(knobs) >= set(LAUNCH_MODEL_KEYS)
    assert knobs["model"] == "grok-4.7"
    assert knobs["model_rth"] == "grok-4.7"
    assert knobs["model_research"] == "grok-4.7-xhigh"
    assert knobs["model_params"] == {"effort": "xhigh", "thinking": True}
    assert knobs["model_params_rth"] == {"effort": "low"}
    assert knobs["model_params_research"]["effort"] == "xhigh"
    assert knobs["model"] != DEFAULT_MODEL

    client = SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: SimpleNamespace()))
    g = GrokClient(client=client, session="premarket")
    assert g.model == "grok-4.7-xhigh"
    assert g.model_params["effort"] == "xhigh"

    ts._run = {}
    run = ts.begin_run()
    assert run["model"] == "grok-4.7"
    assert run["model_rth"] == "grok-4.7"
    assert run["model_research"] == "grok-4.7-xhigh"
    assert run["model_params"]["thinking"] is True
    disk = json.loads(ts.RUN_PATH.read_text(encoding="utf-8"))
    assert disk["model"] == "grok-4.7"
    assert disk["model_params"]["effort"] == "xhigh"

    from abcxauto.cursor_env import START_PRO_SOURCE
    from abcxauto.pro_desktop import run_app
    from abcxauto.supervisor import prepare_desk_start

    assert "launch_model_knobs" in START_PRO_SOURCE
    assert START_PRO_SOURCE.index("launch_model_knobs") < START_PRO_SOURCE.index(
        "run_app"
    )

    monkeypatch.setattr("abcxauto.supervisor.reap_leftover_desk", lambda **_k: [])
    monkeypatch.setattr("abcxauto.supervisor.clear_stale_desk_lock", lambda: None)
    monkeypatch.setattr("abcxauto.supervisor.clear_operator_stop", lambda: None)
    cfg_mod._runtime_overrides.clear()
    get_config.cache_clear()
    cfg_mod._file_overrides = {}
    prepare_desk_start()
    assert get_config().model == "grok-4.7"
    assert get_config().model_research == "grok-4.7-xhigh"
    assert get_config().model_params["effort"] == "xhigh"

    probe = ts.RUN_PATH.parent / "launch-probe.txt"
    monkeypatch.setenv("ABCXAUTO_LAUNCH_PROBE", str(probe))
    cfg_mod._runtime_overrides.clear()
    get_config.cache_clear()
    cfg_mod._file_overrides = {}
    run_app()
    assert get_config().model == "grok-4.7"
    assert get_config().model_params["thinking"] is True
    assert get_config().model_params_rth == {"effort": "low"}
    assert get_config().model_params_research["effort"] == "xhigh"


def test_session_params_fall_back_to_shared():
    cfg = update_agent_config(
        model_params={"effort": "high", "thinking": True},
        model_params_rth={},
        model_params_research={},
        persist=False,
    )
    assert session_model_params("premarket", cfg) == {
        "effort": "high",
        "thinking": True,
    }
    cfg = update_agent_config(
        model_params={"effort": "high"},
        model_params_rth={"effort": "low"},
        model_params_research={"effort": "xhigh", "future_knob": 1},
        persist=False,
    )
    assert session_model_params("premarket", cfg) == {
        "effort": "xhigh",
        "future_knob": 1,
    }
    client = SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: SimpleNamespace()))
    g = GrokClient(client=client, session="premarket")
    assert g.model_params["effort"] == "xhigh"
    assert g.model_params["future_knob"] == 1


def test_rth_params_cannot_defeat_xhigh_strip_or_f10(monkeypatch):
    """RTH keeps effort/reasoning_effort xhigh; still strips model-id suffix.

    F10 dollar cap is separate and unchanged.
    """
    raw = {"effort": "xhigh", "reasoning_effort": "xhigh", "thinking": True}
    assert rth_params_no_xhigh(raw, enabled=True) == {
        "effort": "xhigh",
        "reasoning_effort": "xhigh",
        "thinking": True,
    }
    assert rth_params_no_xhigh(
        {"leak": "grok-4.7-xhigh", "effort": "xhigh"}, enabled=True
    ) == {"effort": "xhigh"}
    assert rth_params_no_xhigh("not-json", enabled=True) == {}
    assert rth_params_no_xhigh(None, enabled=True) == {}
    kept = rth_params_no_xhigh(raw, enabled=False)
    assert kept["effort"] == "xhigh"
    assert rth_model_no_xhigh("grok-4.7-xhigh", enabled=True) == "grok-4.7"
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_enabled", lambda: True
    )
    cfg = SimpleNamespace(
        model_params={"effort": "xhigh", "thinking": True},
        model_params_rth={"effort": "xhigh", "thinking": True},
        model_params_research={"effort": "xhigh"},
    )
    rth = session_model_params("regular", cfg)
    assert rth == {"effort": "xhigh", "thinking": True}
    assert rth.get("effort") == "xhigh"
    assert session_model_params("premarket", cfg) == {"effort": "xhigh"}


def test_new_chat_applies_rth_params_when_client_had_no_session(monkeypatch):
    from abcxauto.brain import _new_chat
    from abcxauto.llm import GrokClient

    created: dict = {}

    class _ChatAPI:
        def create(self, **k):
            created.update(k)
            return SimpleNamespace()

    update_agent_config(
        model_params={"effort": "xhigh", "thinking": True},
        persist=False,
    )
    monkeypatch.setattr("abcxauto.thin_rth_kill_look.kill_look_enabled", lambda: True)
    g = GrokClient(client=SimpleNamespace(chat=_ChatAPI()))
    assert g.model_params.get("effort") == "xhigh"
    _new_chat(g, session="regular")
    assert g.model_params.get("effort") == "xhigh"
    assert created.get("reasoning_effort") == "xhigh"
    assert "thinking" not in created
    client = SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: SimpleNamespace()))
    update_agent_config(
        model_params_rth={"effort": "xhigh", "thinking": True},
        persist=False,
    )
    g = GrokClient(client=client, session="regular")
    assert g.model_params.get("effort") == "xhigh"
    assert g.model_params.get("thinking") is True


def test_env_session_params_load(monkeypatch):
    monkeypatch.setenv(
        "ABCXAUTO_MODEL_PARAMS_RTH",
        json.dumps({"effort": "low"}),
    )
    monkeypatch.setenv(
        "ABCXAUTO_MODEL_PARAMS_RESEARCH",
        json.dumps({"effort": "xhigh"}),
    )
    get_config.cache_clear()
    cfg = get_config()
    assert cfg.model_params_rth == {"effort": "low"}
    assert cfg.model_params_research == {"effort": "xhigh"}
