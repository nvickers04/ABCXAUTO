"""Grok's tool set: read-only market data + account tools, plus propose_order.

Every schema is defined once here and passed to xai_sdk natively. Handlers are
async and return JSON-serializable dicts. `propose_order` is intercepted by the
agent loop (validated and auto-executed); its handler here only exists for
schema registration.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Dict, List

from xai_sdk.chat import tool

from abcxauto.marketdata.client import get_marketdata_client
from abcxauto.marketdata.market_hours import get_session_info
from abcxauto.proposals import STRATEGIES

logger = logging.getLogger(__name__)

_SYMBOL = {"type": "string", "description": "Stock ticker, e.g. AAPL"}


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# ---------------------------------------------------------------------------
# Tool schemas (passed to xai_sdk chat.create)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    tool(
        name="quote",
        description="Real-time quote for a stock: price, bid/ask, volume, change %.",
        parameters=_schema({"symbol": _SYMBOL}, ["symbol"]),
    ),
    tool(
        name="candles",
        description="Historical OHLCV candles. Resolution: 'D' daily, 'W' weekly, or minutes like '5', '15', '60'.",
        parameters=_schema(
            {
                "symbol": _SYMBOL,
                "resolution": {"type": "string", "description": "Bar size (default 'D')"},
                "days_back": {"type": "integer", "description": "Calendar days of history (default 30)"},
            },
            ["symbol"],
        ),
    ),
    tool(
        name="atr",
        description="Average True Range (volatility) for a stock, daily bars.",
        parameters=_schema(
            {
                "symbol": _SYMBOL,
                "period": {"type": "integer", "description": "ATR period (default 14)"},
            },
            ["symbol"],
        ),
    ),
    tool(
        name="option_chain",
        description=(
            "Option chain with greeks and IV. Filter by expiration (YYYYMMDD), side "
            "('call'/'put'), DTE range, or target delta. Use this to find real strikes "
            "and expirations before proposing option structures."
        ),
        parameters=_schema(
            {
                "symbol": _SYMBOL,
                "expiration": {"type": "string", "description": "YYYYMMDD or YYYY-MM-DD (optional)"},
                "side": {"type": "string", "enum": ["call", "put"], "description": "Filter one side (optional)"},
                "min_dte": {"type": "integer", "description": "Min days to expiration (optional)"},
                "max_dte": {"type": "integer", "description": "Max days to expiration (optional)"},
                "delta": {"type": "number", "description": "Target absolute delta, e.g. 0.30 (optional)"},
                "strike_limit": {"type": "integer", "description": "Strikes near the money to include (optional)"},
            },
            ["symbol"],
        ),
    ),
    tool(
        name="option_expirations",
        description="List available option expiration dates (YYYY-MM-DD) for a symbol.",
        parameters=_schema({"symbol": _SYMBOL}, ["symbol"]),
    ),
    tool(
        name="option_quote",
        description=(
            "Quote + greeks (delta/gamma/theta/vega, IV, open interest) for a single "
            "option contract by OCC symbol, e.g. AAPL250117C00150000."
        ),
        parameters=_schema(
            {"option_symbol": {"type": "string", "description": "OCC option symbol"}},
            ["option_symbol"],
        ),
    ),
    tool(
        name="iv_info",
        description="Implied volatility summary for a symbol: ATM IV and IV by expiration.",
        parameters=_schema(
            {
                "symbol": _SYMBOL,
                "min_dte": {"type": "integer", "description": "Min DTE (optional)"},
                "max_dte": {"type": "integer", "description": "Max DTE (optional)"},
            },
            ["symbol"],
        ),
    ),
    tool(
        name="news",
        description="Recent news headlines for a symbol.",
        parameters=_schema(
            {
                "symbol": _SYMBOL,
                "countback": {"type": "integer", "description": "Number of items (default 10)"},
            },
            ["symbol"],
        ),
    ),
    tool(
        name="earnings",
        description="Earnings dates and results (past and upcoming) for a symbol.",
        parameters=_schema({"symbol": _SYMBOL}, ["symbol"]),
    ),
    tool(
        name="market_hours",
        description="Current US market session (premarket/regular/postmarket/closed) and next transition.",
        parameters=_schema({}, []),
    ),
    tool(
        name="positions",
        description="The operator's current IBKR positions with P&L.",
        parameters=_schema({}, []),
    ),
    tool(
        name="account_summary",
        description="IBKR account summary: net liquidation, cash, buying power, margin.",
        parameters=_schema({}, []),
    ),
    tool(
        name="open_orders",
        description="The operator's open (working) IBKR orders.",
        parameters=_schema({}, []),
    ),
    tool(
        name="protection_status",
        description=(
            "Protection audit: every position with its working stop-loss and take-profit "
            "orders, flagging any UNPROTECTED position. Use this to monitor and manage "
            "stops/targets."
        ),
        parameters=_schema({}, []),
    ),
    tool(
        name="executions",
        description="Recent execution fills (actual prices, commissions) — use to assess realized P&L.",
        parameters=_schema({}, []),
    ),
    tool(
        name="propose_order",
        description=(
            "Propose an order structure. ALL proposals auto-execute immediately under "
            "autonomous policy — entries, exits, and order-management actions "
            "(modify_order, modify_stop, modify_target, cancel_order, oca, "
            "trailing_stop, trailing_stop_limit). The tool result says whether "
            "the action executed or was rejected.\n"
            "POLICY: every new position MUST have a stop loss and take profit. Open stock "
            "positions with 'bracket' (limit entry) or 'market_bracket' (market entry). "
            "Bare limit/market/stop orders are only valid to close/reduce an existing "
            "position and require closing_position=true in params. Never leave a position "
            "unprotected: if you cancel or edit protection, ensure a stop remains or is "
            "immediately replaced.\n"
            "Strategies and their params:\n"
            "- bracket: symbol, quantity, direction(LONG/SHORT), entry_price, stop_price, target_price, time_bucket(intraday/short_swing/swing)\n"
            "- market_bracket: symbol, quantity, direction, stop_price, target_price (market entry, then OCA stop+target)\n"
            "- oca: symbol, quantity, direction, stop_price, target_price (protective pair for existing position)\n"
            "- modify_order: order_id + any of limit_price, stop_price, quantity (edit any working order in place, e.g. reprice an unfilled entry)\n"
            "- modify_stop: order_id, new_stop_price (move an existing stop order)\n"
            "- modify_target: order_id, new_limit_price (move an existing take-profit order)\n"
            "- cancel_order: order_id (auto-executes; replace protection immediately if you cancel a stop)\n"
            "- close_option: symbol, expiration(YYYYMMDD), strike, right(C/P), quantity (omit = full position), "
            "limit_price (omit = mid-based limit). THE ONLY WAY to close an option position — "
            "stock orders cannot close options.\n"
            "- limit_order: symbol, action(BUY/SELL), quantity, limit_price, tif(DAY/GTC), closing_position=true (STOCK EXITS ONLY)\n"
            "- market_order: symbol, action, quantity, closing_position=true (EXITS ONLY)\n"
            "- stop_order: symbol, action, quantity, stop_price, closing_position=true (EXITS ONLY)\n"
            "- stop_limit: symbol, action, quantity, stop_price, limit_price, tif, closing_position=true (EXITS ONLY)\n"
            "- trailing_stop: symbol, quantity, direction, trail_amount OR trail_percent\n"
            "- trailing_stop_limit: same as trailing_stop + limit_offset\n"
            "- vertical_spread: symbol, expiration(YYYYMMDD), long_strike, short_strike, right(C/P), quantity, limit_price\n"
            "- iron_condor: symbol, expiration, put_long_strike, put_short_strike, call_short_strike, call_long_strike, quantity, limit_price\n"
            "- iron_butterfly: symbol, expiration, center_strike, wing_width, quantity, limit_price\n"
            "- straddle: symbol, expiration, strike, quantity, action(BUY/SELL), limit_price\n"
            "- strangle: symbol, expiration, put_strike, call_strike, quantity, action, limit_price\n"
            "- butterfly: symbol, expiration, lower_strike, middle_strike, upper_strike, right, quantity, limit_price\n"
            "- calendar_spread: symbol, strike, near_expiration, far_expiration, right, quantity, limit_price\n"
            "- diagonal_spread: symbol, near_strike, far_strike, near_expiration, far_expiration, right, quantity, limit_price\n"
            "- covered_call / protective_put: symbol, expiration, strike, shares (multiple of 100)\n"
            "- collar: symbol, expiration, put_strike, call_strike, shares\n"
            "- ratio_spread: symbol, expiration, long_strike, short_strike, right, ratio([long,short]), quantity, limit_price\n"
            "- jade_lizard: symbol, expiration, put_strike, call_short_strike, call_long_strike, quantity, limit_price"
        ),
        parameters=_schema(
            {
                "strategy": {"type": "string", "enum": sorted(STRATEGIES.keys())},
                "params": {
                    "type": "object",
                    "description": "Strategy parameters (see strategy list in tool description)",
                },
                "rationale": {
                    "type": "string",
                    "description": "Plain-English reasoning: thesis, risk, breakevens, why this structure",
                },
                "max_loss": {"type": "string", "description": "Estimated max loss, e.g. '$240 per contract'"},
                "max_gain": {"type": "string", "description": "Estimated max gain, e.g. '$160 per contract'"},
            },
            ["strategy", "params", "rationale"],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Handlers (read-only). The chat loop passes the IBKR connector in.
# ---------------------------------------------------------------------------

def _clip(data: Any, max_chars: int = 24_000) -> str:
    """Serialize a tool result, clipping oversized payloads (e.g. huge chains)."""
    text = json.dumps(data, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + '... [truncated — narrow the request with filters]"}'
    return text


async def _quote(args: Dict[str, Any], connector: Any) -> Any:
    return await get_marketdata_client().get_quote(args["symbol"])


async def _candles(args: Dict[str, Any], connector: Any) -> Any:
    return await get_marketdata_client().get_candles(
        args["symbol"],
        resolution=args.get("resolution", "D"),
        days_back=int(args.get("days_back", 30)),
    )


async def _atr(args: Dict[str, Any], connector: Any) -> Any:
    value = await get_marketdata_client().calculate_atr(
        args["symbol"], period=int(args.get("period", 14))
    )
    return {"symbol": args["symbol"], "atr": value}


def _mda_date(value: Any) -> Any:
    """Normalize YYYYMMDD (IBKR style) to YYYY-MM-DD (MarketData.app style)."""
    if isinstance(value, str) and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


async def _option_chain(args: Dict[str, Any], connector: Any) -> Any:
    dte_range = None
    if args.get("min_dte") is not None or args.get("max_dte") is not None:
        dte_range = (args.get("min_dte", 0), args.get("max_dte", 3650))
    return await get_marketdata_client().get_option_chain(
        args["symbol"],
        expiration=_mda_date(args.get("expiration")),
        side=args.get("side"),
        dte_range=dte_range,
        delta=args.get("delta"),
        strike_limit=args.get("strike_limit"),
    )


async def _option_expirations(args: Dict[str, Any], connector: Any) -> Any:
    return {"symbol": args["symbol"], "expirations": await get_marketdata_client().get_option_expirations(args["symbol"])}


async def _option_quote(args: Dict[str, Any], connector: Any) -> Any:
    return await get_marketdata_client().get_option_quote(args["option_symbol"])


async def _iv_info(args: Dict[str, Any], connector: Any) -> Any:
    return await get_marketdata_client().get_iv_rank(
        args["symbol"], dte_min=args.get("min_dte"), dte_max=args.get("max_dte")
    )


async def _news(args: Dict[str, Any], connector: Any) -> Any:
    return await get_marketdata_client().get_news(
        args["symbol"], countback=int(args.get("countback", 10))
    )


async def _earnings(args: Dict[str, Any], connector: Any) -> Any:
    return await get_marketdata_client().get_earnings(args["symbol"])


async def _market_hours(args: Dict[str, Any], connector: Any) -> Any:
    return get_session_info()


async def _positions(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_positions()


async def _account_summary(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_account_summary()


async def _open_orders(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_open_orders()


async def _protection_status(args: Dict[str, Any], connector: Any) -> Any:
    from abcxauto.monitor import build_protection_report

    positions = await connector.get_positions()
    orders = await connector.get_open_orders()
    return build_protection_report(positions, orders)


async def _executions(args: Dict[str, Any], connector: Any) -> Any:
    return await connector.get_recent_executions()


Handler = Callable[[Dict[str, Any], Any], Coroutine[Any, Any, Any]]

READONLY_HANDLERS: Dict[str, Handler] = {
    "quote": _quote,
    "candles": _candles,
    "atr": _atr,
    "option_chain": _option_chain,
    "option_expirations": _option_expirations,
    "option_quote": _option_quote,
    "iv_info": _iv_info,
    "news": _news,
    "earnings": _earnings,
    "market_hours": _market_hours,
    "positions": _positions,
    "account_summary": _account_summary,
    "open_orders": _open_orders,
    "protection_status": _protection_status,
    "executions": _executions,
}


async def run_readonly_tool(name: str, args: Dict[str, Any], connector: Any) -> str:
    """Execute a read-only tool and return a JSON string for tool_result."""
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
