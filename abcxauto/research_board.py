"""Research board: same columns for held + scan names. Pure facts, no send.

One pass shapes rows the caller already fetched. No network. Never borrow
another row's last or chain. Ruled-out filtering is the caller's job.
"""

from __future__ import annotations

from typing import Any


def _sym(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _finite(raw: Any) -> float | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val or val in (float("inf"), float("-inf")):
        return None
    return val


def _spread(bid: Any, ask: Any) -> float | None:
    b = _finite(bid)
    a = _finite(ask)
    if b is None or a is None:
        return None
    return a - b


def _headline_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("headline", "title", "text", "summary"):
            raw = item.get(key)
            if raw is not None and str(raw).strip():
                return str(raw)
    return str(item or "")


def _news_count(headlines: Any, symbol: str) -> tuple[int, bool]:
    """Return (news_n, mark_unavailable).

    Headlines that do not mention the symbol (case insensitive) are dropped.
    When nothing remains, news_n is 0 and unavailable is True.
    """
    sym = _sym(symbol)
    if not isinstance(headlines, list) or not headlines:
        return 0, True
    kept = 0
    for item in headlines:
        text = _headline_text(item)
        if sym and sym.lower() in text.lower():
            kept += 1
    return kept, kept == 0


def _side_quote(chain: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        side = chain.get(key)
        if isinstance(side, dict):
            return side
    return None


def _side_has_bid_ask(side: dict[str, Any] | None) -> bool:
    if not isinstance(side, dict):
        return False
    return _finite(side.get("bid")) is not None and _finite(side.get("ask")) is not None


def _option_mark(chain: Any) -> str:
    """debit_vertical only when chain dict has call and put with bid/ask. No invented strikes."""
    if not isinstance(chain, dict):
        return "unavailable"
    call = _side_quote(chain, "call", "Call", "C")
    put = _side_quote(chain, "put", "Put", "P")
    if _side_has_bid_ask(call) and _side_has_bid_ask(put):
        return "debit_vertical"
    return "unavailable"


def _sizeable(last: Any, stop_dist: Any, chain: Any) -> bool:
    """True when last is finite and stop_dist or a chain dict is present."""
    if _finite(last) is None:
        return False
    if stop_dist:
        return True
    return isinstance(chain, dict)


def _row_sort_key(row: dict[str, Any]) -> tuple:
    vs = _finite(row.get("vs_spy"))
    vs_sort = vs if vs is not None else float("-inf")
    return (0 if row.get("sizeable") else 1, -vs_sort, str(row.get("symbol") or ""))


def sort_board_rows(rows: list[dict]) -> list[dict]:
    """Sizeable first, then higher vs_spy, then symbol. Stable copy."""
    out = [r for r in (rows or []) if isinstance(r, dict)]
    out.sort(key=_row_sort_key)
    return out


def _shape_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    sym = _sym(raw.get("symbol"))
    if not sym or sym == "SPY":
        return None

    last = raw.get("last")
    if _finite(last) is not None:
        last = _finite(last)
    # else keep as-is (None / missing) — never borrow another row

    chain_in = raw.get("chain")
    if isinstance(chain_in, dict):
        chain: Any = chain_in
    else:
        chain = "unavailable"

    news_n, news_gone = _news_count(raw.get("headlines"), sym)
    web = raw.get("web")
    if web is None or web == "":
        web = "unavailable"
    odds = raw.get("odds")
    if odds is None or odds == "":
        odds = "unavailable"

    row: dict[str, Any] = {
        "symbol": sym,
        "last": last,
        "spread": _spread(raw.get("bid"), raw.get("ask")),
        "vs_spy": raw.get("vs_spy"),
        "earnings_in": raw.get("earnings_in"),
        "news_n": news_n,
        "web": web,
        "odds": odds,
        "chain": chain,
        "sized": raw.get("sized"),
        "option": _option_mark(chain),
        "sizeable": _sizeable(last, raw.get("stop_dist"), chain),
    }
    if news_gone:
        row["news"] = "unavailable"
    if raw.get("held") is True:
        row["held"] = True
    return row


def _cap_board(rows: list[dict]) -> list[dict]:
    """At most eight non-held names, plus held (flag held=True). Max nine when held present."""
    held = [dict(r) for r in rows if r.get("held") is True]
    others = [dict(r) for r in rows if r.get("held") is not True]
    for h in held:
        h["held"] = True
    others = others[:8]
    return sort_board_rows(held + others)


def build_board(rows_in: list[dict]) -> list[dict]:
    """Shape and sort board rows. SPY dropped. Cap eight plus held."""
    shaped: list[dict] = []
    for raw in rows_in or []:
        if not isinstance(raw, dict):
            continue
        row = _shape_row(raw)
        if row is None:
            continue
        shaped.append(row)
    shaped = sort_board_rows(shaped)
    return _cap_board(shaped)


def _chain_fact(chain: Any) -> str:
    if isinstance(chain, dict):
        return "ok"
    if chain is None or chain == "":
        return "unavailable"
    return str(chain)


def board_line(rows: list[dict] | None) -> str:
    """One fact string. No sell/rotate/should/must."""
    parts: list[str] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        sym = _sym(raw.get("symbol"))
        if not sym:
            continue
        last = raw.get("last")
        vs = raw.get("vs_spy")
        news = raw.get("news_n", 0)
        chain = _chain_fact(raw.get("chain"))
        parts.append(f"{sym} last={last} vs_spy={vs} news={news} chain={chain}")
    if not parts:
        return "board"
    return "board " + " | ".join(parts)


def on_board(rows: list[dict] | None, symbol: str) -> bool:
    """True when symbol is already a board row."""
    want = _sym(symbol)
    if not want:
        return False
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        if _sym(raw.get("symbol")) == want:
            return True
    return False
