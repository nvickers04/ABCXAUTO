"""Readonly tools for agent_loop snap (account / book / hours / quote)."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Dict, List

from xai_sdk.chat import tool

from abcxauto.marketdata.market_hours import get_session_info

logger = logging.getLogger(__name__)

_SYMBOL = {"type": "string", "description": "Stock ticker, e.g. AAPL"}


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# Snap surface only — Pro JSON path does not use propose_order / MDA research tools.
TOOL_DEFINITIONS = [
    tool(
        name="quote",
        description="IBKR live last/bid/ask (TWS stream). Use this for send geometry — not MDA.",
        parameters=_schema(
            {
                "symbol": _SYMBOL,
                "symbols": {"type": "array", "items": {"type": "string"}},
            },
            [],
        ),
    ),
    tool(
        name="market_hours",
        description="US session: premarket/regular/postmarket/closed.",
        parameters=_schema({}, []),
    ),
    tool(
        name="positions",
        description="IBKR positions with P&L.",
        parameters=_schema({}, []),
    ),
    tool(
        name="account_summary",
        description="IBKR account: NLV, cash, buying power, margin.",
        parameters=_schema({}, []),
    ),
    tool(
        name="open_orders",
        description="Working IBKR orders.",
        parameters=_schema({}, []),
    ),
]


def _clip(data: Any, max_chars: int = 24_000) -> str:
    text = json.dumps(data, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + '... [truncated — narrow the request with filters]"}'
    return text


def _quote_symbols(args: Dict[str, Any]) -> List[str]:
    raw = args.get("symbols")
    if raw is None:
        raw = args.get("symbol")
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out: List[str] = []
    for item in items:
        sym = str(item or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= 8:
            break
    return out


async def _quote(args: Dict[str, Any], connector: Any) -> Any:
    """IBKR live quote. No MDA fallback — delayed prints are a different tool."""
    items = _quote_symbols(args)
    if not items:
        return {"error": "symbol required", "source": "ibkr"}
    fresh = bool(args.get("fresh"))
    batch = getattr(connector, "get_live_quotes", None)
    single = getattr(connector, "get_live_quote", None)
    if len(items) == 1 and callable(single):
        try:
            return await single(items[0], fresh=fresh)
        except TypeError:
            return await single(items[0])
    if callable(batch):
        try:
            return await batch(items, fresh=fresh)
        except TypeError:
            return await batch(items)
    if callable(single):
        rows = []
        for s in items:
            try:
                rows.append(await single(s, fresh=fresh))
            except TypeError:
                rows.append(await single(s))
        return {"source": "ibkr", "freshness": "live", "quotes": rows}
    return {"error": "IBKR live quote unavailable", "source": "ibkr"}


async def _market_hours(args: Dict[str, Any], connector: Any) -> Any:
    return get_session_info()


async def _positions(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_positions()


async def _account_summary(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_account_summary()


async def _open_orders(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_open_orders()


Handler = Callable[[Dict[str, Any], Any], Coroutine[Any, Any, Any]]

READONLY_HANDLERS: Dict[str, Handler] = {
    "quote": _quote,
    "market_hours": _market_hours,
    "positions": _positions,
    "account_summary": _account_summary,
    "open_orders": _open_orders,
}


async def run_readonly_tool(name: str, args: Dict[str, Any], connector: Any) -> str:
    handler = READONLY_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = await handler(args, connector)
        if result is None:
            return json.dumps({"error": f"{name}: no data returned"})
        return _clip(result)
    except Exception as e:
        logger.warning(f"Tool {name} failed: {e}")
        return json.dumps({"error": f"{name} failed: {e}"})
