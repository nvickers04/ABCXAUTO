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
from abcxauto.llm import GrokClient, chat_create_kwargs, create_chat
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
    assert kw["thinking"] is True
    assert kw["effort"] == "xhigh"
    assert kw["include"] == ["verbose_streaming"]
    create_chat(SimpleNamespace(chat=_Chat()), **kw)
    assert created["thinking"] is True
    assert created["model"] == "grok-4.7"


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
        model_params={"thinking": True, "effort": "xhigh"},
    )
    kw = chat_create_kwargs(g, messages=["hi"])
    assert "thinking" in kw
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
    raw = {"effort": "xhigh", "reasoning_effort": "xhigh", "thinking": True}
    assert rth_params_no_xhigh(raw, enabled=True) == {"thinking": True}
    assert rth_params_no_xhigh("not-json", enabled=True) == {}
    assert rth_params_no_xhigh(None, enabled=True) == {}
    kept = rth_params_no_xhigh(raw, enabled=False)
    assert kept["effort"] == "xhigh"
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_enabled", lambda: True
    )
    cfg = SimpleNamespace(
        model_params={"effort": "xhigh", "thinking": True},
        model_params_rth={"effort": "xhigh", "thinking": True},
        model_params_research={"effort": "xhigh"},
    )
    rth = session_model_params("regular", cfg)
    assert rth == {"thinking": True}
    assert rth.get("effort") != "xhigh"
    assert session_model_params("premarket", cfg) == {"effort": "xhigh"}
    client = SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: SimpleNamespace()))
    update_agent_config(
        model_params_rth={"effort": "xhigh", "thinking": True},
        persist=False,
    )
    g = GrokClient(client=client, session="regular")
    assert g.model_params.get("effort") != "xhigh"
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
