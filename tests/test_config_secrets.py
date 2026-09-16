"""Config secrets never print. A pytest traceback showed the xAI key once."""

from dataclasses import asdict, fields, replace

from abcxauto.config import Config

_SECRET_FIELDS = ("xai_api_key", "marketdata_token", "live_confirm")


def _cfg() -> Config:
    return Config(
        xai_api_key="xai-FAKE-KEY-0123456789",
        marketdata_token="mda-FAKE-TOKEN-abcdef",
        live_confirm="I UNDERSTAND LIVE TRADING",
    )


def test_repr_and_str_hide_secrets():
    cfg = _cfg()
    for text in (repr(cfg), str(cfg), f"{cfg}", f"{cfg!r}"):
        assert "xai-" not in text
        assert "FAKE" not in text
        assert "mda-" not in text
        assert "UNDERSTAND" not in text
        for name in _SECRET_FIELDS:
            assert f"{name}=" not in text
    # the non-secret knobs still print
    assert "model=" in repr(cfg)


def test_secret_fields_are_repr_false():
    by_name = {f.name: f for f in fields(Config)}
    for name in _SECRET_FIELDS:
        assert by_name[name].repr is False, name


def test_secrets_still_flow_through_asdict_and_replace():
    """repr=False hides the value from print, not from the code that uses it."""
    cfg = _cfg()
    assert cfg.xai_api_key == "xai-FAKE-KEY-0123456789"
    assert asdict(cfg)["xai_api_key"] == "xai-FAKE-KEY-0123456789"
    assert asdict(cfg)["marketdata_token"] == "mda-FAKE-TOKEN-abcdef"
    swapped = replace(cfg, xai_api_key="xai-OTHER")
    assert swapped.xai_api_key == "xai-OTHER"
    assert swapped.marketdata_token == cfg.marketdata_token
    assert "xai-OTHER" not in repr(swapped)
