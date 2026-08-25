"""Shared news feed formatting for agent + UI."""

from abcxauto.news_feed import _universe, format_news_for_prompt


def test_format_news_for_prompt_empty():
    text = format_news_for_prompt([])
    assert "NEWS" in text
    assert "no headlines" in text
    assert "MDA" not in text


def test_format_news_for_prompt_items():
    text = format_news_for_prompt([
        {"symbol": "SPY", "headline": "Markets rally into the close"},
        {"symbol": "AAPL", "headline": "Apple supplier update"},
    ])
    assert "[SPY] Markets rally" in text
    assert "[AAPL] Apple supplier" in text


def test_universe_is_book_then_index_not_sandbox(tmp_path, monkeypatch):
    from abcxauto.universe import reset_universe_cache, save_allowlist

    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe.json"))
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": ["NVDA"],
            "exclude_symbols": [],
            "legal_symbols": ["SNXX", "AAOX", "NVDA"],
        }
    )
    reset_universe_cache()
    syms = _universe([{"symbol": "CRM"}, {"symbol": "crm"}])
    assert syms[0] == "CRM"
    assert "SPY" in syms
    assert "SNXX" not in syms
    assert "AAOX" not in syms
    assert len(syms) <= 14
    flat = _universe([])
    assert flat[0] == "SPY"
    assert "SNXX" not in flat
