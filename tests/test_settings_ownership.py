"""Who may write which settings knob.

Two decisions live here. The IBKR host and client id move only while the link
is down (two books = two processes, two client ids — a live socket keeps the
host and id it dialled). ``scan_fetch_cap`` has exactly one writer,
``self_tune``.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from abcxauto.config import (
    AGENT_CONFIG_KEYS,
    AGENT_DISCONNECTED_ONLY_KEYS,
    PERSISTED_SETTINGS_KEYS,
    broker_link_connected,
    clamp_agent_knobs,
    get_config,
    risk_settings_path,
    set_agent_knobs,
    update_agent_config,
)

# Imported up front: the fake connector below shadows the real module, and
# pro_desktop pulls the broker package in at import time.
from abcxauto.pro_desktop import ProTerminal

CONNECTOR = "abcxauto.broker.connector"


def _fake_connector(*, connected: bool) -> types.ModuleType:
    mod = types.ModuleType(CONNECTOR)
    mod.IBKRConnector = type(
        "IBKRConnector",
        (),
        {"_instance": types.SimpleNamespace(connected=connected)},
    )
    return mod


class _Page:
    """Enough Flet page for ProTerminal.__init__ — no window, no event loop."""

    title = ""
    bgcolor = ""
    padding = 0
    theme_mode = None

    def __init__(self) -> None:
        self.window = type(
            "W", (), {"width": 1280, "height": 820, "min_width": 960, "min_height": 640}
        )()
        self.snack_bar = None
        self.overlay = []
        self.controls = []

    def add(self, *_) -> None:
        pass

    def update(self) -> None:
        pass

    def run_task(self, _) -> None:
        pass


@pytest.fixture
def link_up(monkeypatch):
    monkeypatch.setitem(sys.modules, CONNECTOR, _fake_connector(connected=True))


@pytest.fixture
def link_down(monkeypatch):
    monkeypatch.setitem(sys.modules, CONNECTOR, _fake_connector(connected=False))


# ------------------------------------------------------------------ link probe


def test_an_unimported_connector_is_not_a_link(monkeypatch):
    monkeypatch.delitem(sys.modules, CONNECTOR, raising=False)
    assert broker_link_connected() is False


def test_a_built_but_idle_connector_is_not_a_link(link_down):
    assert broker_link_connected() is False


def test_a_live_socket_is_a_link(link_up):
    assert broker_link_connected() is True


def test_a_broken_connector_reads_as_no_link(monkeypatch):
    class _Boom:
        @property
        def connected(self) -> bool:
            raise RuntimeError("socket gone")

    mod = types.ModuleType(CONNECTOR)
    mod.IBKRConnector = type("IBKRConnector", (), {"_instance": _Boom()})
    monkeypatch.setitem(sys.modules, CONNECTOR, mod)
    assert broker_link_connected() is False


# ------------------------------------------------------------ link knob lock


def test_host_and_client_id_are_the_disconnected_only_knobs():
    assert AGENT_DISCONNECTED_ONLY_KEYS == {"ibkr_host", "ibkr_client_id"}
    assert AGENT_DISCONNECTED_ONLY_KEYS <= AGENT_CONFIG_KEYS


def test_client_id_moves_while_the_link_is_down(link_down):
    assert update_agent_config(ibkr_client_id=77).ibkr_client_id == 77
    assert json.loads(risk_settings_path().read_text(encoding="utf-8"))["ibkr_client_id"] == 77


def test_client_id_is_refused_while_connected(link_up):
    before = get_config().ibkr_client_id
    with pytest.raises(ValueError) as err:
        update_agent_config(ibkr_client_id=before + 1)
    assert "disconnect" in str(err.value).lower()
    assert get_config().ibkr_client_id == before


def test_the_form_reports_the_refusal_instead_of_swallowing_it(link_up):
    before = get_config().ibkr_client_id
    res = set_agent_knobs({"ibkr_client_id": before + 1, "monitor_poll_s": 60})
    assert "ibkr_client_id" not in res["applied"]
    assert "disconnect" in res["rejected"]["ibkr_client_id"].lower()
    # Scoped: the rest of the form still applies.
    assert res["applied"]["monitor_poll_s"] == 60
    assert get_config().ibkr_client_id == before


def test_a_refused_client_id_never_reaches_the_settings_file(link_up):
    set_agent_knobs({"ibkr_client_id": get_config().ibkr_client_id + 1})
    path = risk_settings_path()
    if path.is_file():
        assert "ibkr_client_id" not in json.loads(path.read_text(encoding="utf-8"))


def test_host_moves_while_the_link_is_down(link_down):
    assert update_agent_config(ibkr_host="10.0.0.5").ibkr_host == "10.0.0.5"
    assert json.loads(risk_settings_path().read_text(encoding="utf-8"))["ibkr_host"] == "10.0.0.5"


def test_host_is_refused_while_connected(link_up):
    before = get_config().ibkr_host
    with pytest.raises(ValueError) as err:
        update_agent_config(ibkr_host="10.0.0.5")
    assert "disconnect" in str(err.value).lower()
    assert get_config().ibkr_host == before


def test_the_form_reports_a_refused_host_instead_of_swallowing_it(link_up):
    before = get_config().ibkr_host
    res = set_agent_knobs({"ibkr_host": "10.0.0.5", "monitor_poll_s": 60})
    assert "ibkr_host" not in res["applied"]
    assert "disconnect" in res["rejected"]["ibkr_host"].lower()
    # Scoped: the rest of the form still applies.
    assert res["applied"]["monitor_poll_s"] == 60
    assert get_config().ibkr_host == before


def test_a_refused_host_never_reaches_the_settings_file(link_up):
    set_agent_knobs({"ibkr_host": "10.0.0.5"})
    path = risk_settings_path()
    if path.is_file():
        assert "ibkr_host" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_connected_desk_still_takes_the_other_knobs(link_up):
    applied, _notes, rejected = clamp_agent_knobs(
        {"model": "grok-4.6", "max_tokens": 4096, "temperature": 0.4}
    )
    assert rejected == {}
    assert applied["max_tokens"] == 4096


@pytest.mark.parametrize("key", sorted(AGENT_DISCONNECTED_ONLY_KEYS))
def test_settings_field_is_disabled_while_connected(monkeypatch, key):
    pro = ProTerminal(_Page())
    monkeypatch.setitem(sys.modules, CONNECTOR, _fake_connector(connected=True))
    pro._sync_settings_page(force=True)
    assert pro.fields[key].disabled is True
    monkeypatch.setitem(sys.modules, CONNECTOR, _fake_connector(connected=False))
    pro._sync_settings_page(force=True)
    assert pro.fields[key].disabled is False


# ------------------------------------------------------- scan_fetch_cap owner


def test_scan_cap_is_not_operator_writable():
    assert "scan_fetch_cap" not in AGENT_CONFIG_KEYS
    assert "scan_fetch_cap" not in PERSISTED_SETTINGS_KEYS
    res = set_agent_knobs({"scan_fetch_cap": 3})
    assert res["applied"] == {}
    assert res["rejected"]["scan_fetch_cap"] == "not an agent setting"


def test_scan_cap_is_not_on_the_settings_form():
    from abcxauto.pro_desktop import AGENT_FIELD_KEYS, PACING_FIELDS

    assert "scan_fetch_cap" not in AGENT_FIELD_KEYS
    assert all(key != "scan_fetch_cap" for key, _label, _hint in PACING_FIELDS)


def test_self_tune_still_owns_the_scan_cap():
    from abcxauto.self_tune import SCAN_FETCH_CAP_RANGE, apply_self_tune

    res = apply_self_tune({"scan_fetch_cap": 3}, persist=True)
    assert res["applied"]["scan_fetch_cap"] == 3
    assert get_config().scan_fetch_cap == 3

    hi = SCAN_FETCH_CAP_RANGE[1]
    res = apply_self_tune({"scan_fetch_cap": hi + 50}, persist=False)
    assert res["applied"]["scan_fetch_cap"] == hi


def test_opportunity_scan_still_reads_the_cap(monkeypatch):
    monkeypatch.delenv("ABCXAUTO_SCAN_FETCH_CAP", raising=False)
    from abcxauto.opportunity_scan import normalize_tickers, scan_fetch_cap
    from abcxauto.self_tune import apply_self_tune

    apply_self_tune({"scan_fetch_cap": 2}, persist=True)
    assert scan_fetch_cap() == 2
    assert normalize_tickers(["AAPL", "MSFT", "NVDA"]) == ["AAPL", "MSFT"]


def test_the_scan_cap_env_form_still_wins_for_the_scanner(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_SCAN_FETCH_CAP", "3")
    from abcxauto.opportunity_scan import scan_fetch_cap

    assert scan_fetch_cap() == 3
