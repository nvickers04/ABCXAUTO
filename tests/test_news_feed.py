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


def test_universe_prefers_book_then_market():
    syms = _universe([{"symbol": "CRM"}, {"symbol": "crm"}])
    assert syms[0] == "CRM"
    assert "NVDA" in syms
    assert len(syms) <= 14
