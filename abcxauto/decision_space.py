"""Agent-visible decision space: what orders are possible + paper exercise.

Situational awareness is incomplete without knowing *which decisions the
system can actually make*. This module catalogs IBKR-facing capabilities
and exercises paper place → (fill if marketable) → cancel-each-by-id.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abcxauto.broker.order_types import IBKROrderType
from abcxauto.config import get_config
from abcxauto.proposals import STRATEGIES

LOG = Path("decision_space.log")

# Explicit map: strategy → what the agent can decide (for awareness prompts/tools)
DECISION_CATALOG: list[dict[str, Any]] = [
    {
        "id": "market_order",
        "ibkr_type": "MKT",
        "kind": "stock_exit_or_entry",
        "fills_when": "immediately at market",
        "agent_use": "closing_position=true for exits; prefer market_bracket for new risk",
        "cancelable": "only while working (usually fills instantly)",
    },
    {
        "id": "limit_order",
        "ibkr_type": "LMT",
        "kind": "stock",
        "fills_when": "price reaches limit",
        "agent_use": "exit/reprice; entry only with defined risk policy",
        "cancelable": "yes while working",
    },
    {
        "id": "stop_order",
        "ibkr_type": "STP",
        "kind": "stock_protection",
        "fills_when": "stop triggered → market",
        "agent_use": "exits/protection; closing_position for bare stops",
        "cancelable": "yes while working",
    },
    {
        "id": "stop_limit",
        "ibkr_type": "STP LMT",
        "kind": "stock_protection",
        "fills_when": "stop triggers then limit",
        "agent_use": "protected exit with limit",
        "cancelable": "yes while working",
    },
    {
        "id": "trailing_stop",
        "ibkr_type": "TRAIL",
        "kind": "stock_protection",
        "fills_when": "trail triggers → market",
        "agent_use": "let winners run with dynamic stop",
        "cancelable": "yes while working",
    },
    {
        "id": "trailing_stop_limit",
        "ibkr_type": "TRAIL LIMIT",
        "kind": "stock_protection",
        "fills_when": "trail triggers then limit",
        "agent_use": "trail with limit protection",
        "cancelable": "yes while working",
    },
    {
        "id": "bracket",
        "ibkr_type": "LMT+OCA",
        "kind": "entry_defined_risk",
        "fills_when": "entry limit fills; then stop/target work",
        "agent_use": "preferred new long/short with stop+target",
        "cancelable": "cancel each child/parent by order_id",
    },
    {
        "id": "market_bracket",
        "ibkr_type": "MKT+OCA",
        "kind": "entry_defined_risk",
        "fills_when": "entry markets; then OCA works",
        "agent_use": "fast entry with stop+target",
        "cancelable": "cancel OCA legs by order_id",
    },
    {
        "id": "oca",
        "ibkr_type": "OCA group",
        "kind": "protection_pair",
        "fills_when": "stop or target triggers",
        "agent_use": "protect existing position",
        "cancelable": "cancel each leg by order_id",
    },
    {
        "id": "modify_order",
        "ibkr_type": "modify",
        "kind": "management",
        "fills_when": "n/a",
        "agent_use": "reprice/resize working order by order_id",
        "cancelable": "n/a",
    },
    {
        "id": "modify_stop",
        "ibkr_type": "modify",
        "kind": "management",
        "fills_when": "n/a",
        "agent_use": "move stop by order_id",
        "cancelable": "n/a",
    },
    {
        "id": "modify_target",
        "ibkr_type": "modify",
        "kind": "management",
        "fills_when": "n/a",
        "agent_use": "move target by order_id",
        "cancelable": "n/a",
    },
    {
        "id": "cancel_order",
        "ibkr_type": "cancel",
        "kind": "management",
        "fills_when": "n/a",
        "agent_use": "cancel ONE working order by order_id (explicit individual cancel)",
        "cancelable": "n/a",
    },
    {
        "id": "close_option",
        "ibkr_type": "OPT close",
        "kind": "option_exit",
        "fills_when": "limit/mid close",
        "agent_use": "ONLY way to close options — never stock order for OPT conId",
        "cancelable": "while working",
    },
    {
        "id": "vertical_spread",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo limit",
        "agent_use": "defined-risk vertical",
        "cancelable": "while working",
    },
    {
        "id": "iron_condor",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo limit",
        "agent_use": "range premium",
        "cancelable": "while working",
    },
    {
        "id": "iron_butterfly",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo limit",
        "agent_use": "pin premium",
        "cancelable": "while working",
    },
    {
        "id": "straddle",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "vol expansion",
        "cancelable": "while working",
    },
    {
        "id": "strangle",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "cheaper vol",
        "cancelable": "while working",
    },
    {
        "id": "butterfly",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "pin risk-defined",
        "cancelable": "while working",
    },
    {
        "id": "calendar_spread",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "term structure",
        "cancelable": "while working",
    },
    {
        "id": "diagonal_spread",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "directional calendar",
        "cancelable": "while working",
    },
    {
        "id": "covered_call",
        "ibkr_type": "stock+OPT",
        "kind": "multi_leg",
        "fills_when": "stock+short call",
        "agent_use": "income on stock",
        "cancelable": "legs by order_id",
    },
    {
        "id": "protective_put",
        "ibkr_type": "stock+OPT",
        "kind": "multi_leg",
        "fills_when": "stock+long put",
        "agent_use": "hedge stock",
        "cancelable": "legs by order_id",
    },
    {
        "id": "collar",
        "ibkr_type": "stock+OPT",
        "kind": "multi_leg",
        "fills_when": "stock+put+call",
        "agent_use": "hedged stock",
        "cancelable": "legs by order_id",
    },
    {
        "id": "ratio_spread",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "ratio risk",
        "cancelable": "while working",
    },
    {
        "id": "jade_lizard",
        "ibkr_type": "BAG/combo",
        "kind": "multi_leg",
        "fills_when": "combo",
        "agent_use": "premium + skew",
        "cancelable": "while working",
    },
]


def list_decision_space() -> dict[str, Any]:
    """Full situational map of decisions the agent can make."""
    cfg = get_config()
    ibkr_enum = [{"name": t.name, "api_value": t.value} for t in IBKROrderType]
    registered = sorted(STRATEGIES.keys())
    return {
        "paper_only": cfg.is_paper,
        "ibkr_port": cfg.ibkr_port,
        "principle": (
            "Awareness = prices + positions(conId) + which order decisions are possible. "
            "Always cancel working orders by exact order_id. Never close OPT by stock symbol."
        ),
        "ibkr_order_type_enum": ibkr_enum,
        "agent_strategies_registered": registered,
        "decisions": DECISION_CATALOG,
        "individual_cancel": {
            "tool": "cancel_order_id",
            "params": ["order_id"],
            "rule": "Cancel ONE working order at a time by order_id from open_orders",
        },
        "paper_exercise": {
            "tool": "paper_exercise_order_types",
            "description": (
                "PAPER ONLY: place order types so marketable ones fill; then cancel "
                "each remaining working order_id individually; flatten residual stock if needed"
            ),
        },
    }


async def _quote_px(connector: Any, symbol: str) -> float:
    try:
        if hasattr(connector, "get_quote"):
            q = await connector.get_quote(symbol)
            if isinstance(q, dict):
                for k in ("last", "ask", "bid", "close"):
                    if q.get(k):
                        return float(q[k])
    except Exception:
        pass
    try:
        from abcxauto.marketdata.client import get_marketdata_client

        q = await get_marketdata_client().get_quote(symbol)
        if isinstance(q, dict):
            for k in ("last", "ask", "bid", "close"):
                if q.get(k):
                    return float(q[k])
    except Exception:
        pass
    return 0.0


def _oids_from(result: Any) -> list[int]:
    if not isinstance(result, dict):
        return []
    out: list[int] = []
    for k in ("order_id", "orderId", "parent_order_id", "stop_order_id", "target_order_id"):
        v = result.get(k)
        if v is not None:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                pass
    for k in ("order_ids", "child_order_ids"):
        for v in result.get(k) or []:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                pass
    return out


async def cancel_order_id(connector: Any, order_id: int) -> dict[str, Any]:
    """Cancel exactly one order by id (agent-explicit individual cancel)."""
    if not get_config().is_paper:
        return {"error": "cancel_order_id paper-only guard: non-paper port", "order_id": order_id}
    try:
        res = await connector.cancel_order(int(order_id))
        return {
            "ok": True,
            "order_id": int(order_id),
            "result": res if isinstance(res, dict) else {"raw": str(res)},
            "mode": "individual_cancel",
        }
    except Exception as e:
        return {"ok": False, "order_id": int(order_id), "error": str(e)}


async def paper_exercise_order_types(
    connector: Any,
    *,
    symbol: str = "SPY",
    quantity: int = 1,
    types: list[str] | None = None,
) -> dict[str, Any]:
    """PAPER: place types (fill marketable), then cancel each working order_id one-by-one.

    Flow:
      1. Place sequence of stock order types
      2. Marketable orders may fill (MKT / aggressive LMT)
      3. Enumerate open orders and cancel EACH by order_id individually
      4. Flatten residual stock qty if any remains
    """
    cfg = get_config()
    if not cfg.is_paper:
        return {"error": "paper_exercise_order_types refused: not paper port", "port": cfg.ibkr_port}
    if not getattr(connector, "connected", False):
        await connector.connect()
    if not getattr(connector, "connected", False):
        return {"error": "IBKR not connected"}

    px = await _quote_px(connector, symbol)
    if px <= 0:
        px = 100.0
    qty = max(1, int(quantity))
    # Aggressive limit to encourage fill on paper
    buy_lmt = round(px * 1.02, 2)
    sell_lmt = round(px * 0.98, 2)
    stop_sell = round(px * 0.90, 2)  # working protection after long
    stop_lim = round(px * 0.89, 2)

    plan = types or [
        "market_order",
        "limit_order",
        "stop_order",
        "stop_limit",
        "trailing_stop",
        "bracket",
        "oca",
        "market_bracket",
    ]

    placed: list[dict] = []
    all_oids: list[int] = []

    async def _place(name: str, coro) -> dict:
        row: dict[str, Any] = {"strategy": name, "t": datetime.now(timezone.utc).isoformat()}
        try:
            res = await coro
            row["result"] = res if isinstance(res, dict) else {"raw": str(res)[:300]}
            oids = _oids_from(res)
            row["order_ids"] = oids
            row["placed_ok"] = bool(
                isinstance(res, dict)
                and (res.get("success") or oids or res.get("filled"))
            )
            all_oids.extend(oids)
        except Exception as e:
            row["placed_ok"] = False
            row["error"] = str(e)
        placed.append(row)
        await asyncio.sleep(0.35)
        return row

    # 1) MARKET buy — should fill
    if "market_order" in plan and hasattr(connector, "place_market_order"):
        await _place(
            "market_order_buy",
            connector.place_market_order(symbol, "BUY", qty, wait_for_fill=True, timeout=20.0),
        )

    # 2) LIMIT buy aggressive — should fill
    if "limit_order" in plan and hasattr(connector, "place_limit_order"):
        await _place(
            "limit_order_buy_aggressive",
            connector.place_limit_order(symbol, "BUY", qty, buy_lmt, tif="DAY"),
        )

    # Position may exist now — place protection that STAYS working, then cancel each
    if "stop_order" in plan and hasattr(connector, "place_stop_order"):
        await _place(
            "stop_order_sell_protect",
            connector.place_stop_order(symbol, "SELL", qty, stop_sell),
        )
    if "stop_limit" in plan and hasattr(connector, "place_stop_limit"):
        await _place(
            "stop_limit_sell_protect",
            connector.place_stop_limit(symbol, "SELL", qty, stop_sell, stop_lim),
        )
    if "trailing_stop" in plan and hasattr(connector, "place_trailing_stop"):
        await _place(
            "trailing_stop_protect",
            connector.place_trailing_stop(symbol, qty, "LONG", trail_percent=5.0),
        )
    if "trailing_stop_limit" in plan and hasattr(connector, "place_trailing_stop_limit"):
        await _place(
            "trailing_stop_limit_protect",
            connector.place_trailing_stop_limit(
                symbol, qty, "LONG", trail_percent=5.0, limit_offset=0.5
            ),
        )
    if "oca" in plan and hasattr(connector, "place_oca"):
        await _place(
            "oca_protect",
            connector.place_oca(symbol, qty, "LONG", stop_sell, buy_lmt * 1.05),
        )
    if "bracket" in plan and hasattr(connector, "place_bracket_order"):
        # Aggressive entry so it can fill, then cancel residual children
        await _place(
            "bracket_aggressive",
            connector.place_bracket_order(
                symbol,
                qty,
                "LONG",
                entry_price=buy_lmt,
                stop_price=stop_sell,
                target_price=round(px * 1.10, 2),
            ),
        )
    if "market_bracket" in plan and hasattr(connector, "place_market_bracket"):
        await _place(
            "market_bracket",
            connector.place_market_bracket(
                symbol, qty, "LONG", stop_price=stop_sell, target_price=round(px * 1.10, 2)
            ),
        )

    # Snapshot open orders → cancel EACH individually by order_id
    open_orders = []
    try:
        open_orders = await connector.get_open_orders() or []
    except Exception as e:
        open_orders = [{"error": str(e)}]

    cancel_log: list[dict] = []
    seen: set[int] = set()
    for o in open_orders:
        if not isinstance(o, dict):
            continue
        oid = o.get("order_id") or o.get("orderId")
        if oid is None:
            continue
        try:
            oid_i = int(oid)
        except (TypeError, ValueError):
            continue
        if oid_i in seen:
            continue
        seen.add(oid_i)
        cancel_log.append(await cancel_order_id(connector, oid_i))
        await asyncio.sleep(0.25)

    # Also cancel any tracked oids not in open list
    for oid in all_oids:
        if oid not in seen:
            cancel_log.append(await cancel_order_id(connector, oid))
            seen.add(oid)
            await asyncio.sleep(0.2)

    # Flatten residual stock position (fills leave inventory)
    flatten = None
    try:
        positions = await connector.get_positions() or []
        for p in positions:
            if str(p.get("symbol", "")).upper() != symbol.upper():
                continue
            sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
            if not sec.startswith("STK"):
                continue
            q = int(float(p.get("quantity") or 0))
            if q == 0:
                continue
            action = "SELL" if q > 0 else "BUY"
            flatten = await connector.place_market_order(
                symbol, action, abs(q), wait_for_fill=True, timeout=20.0
            )
            break
    except Exception as e:
        flatten = {"error": str(e)}

    report = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "symbol": symbol,
        "px": px,
        "decision_space_ref": "list_decision_space",
        "placed": placed,
        "open_orders_before_cancel": open_orders,
        "individual_cancels": cancel_log,
        "cancels_ok": sum(1 for c in cancel_log if c.get("ok")),
        "cancels_fail": sum(1 for c in cancel_log if not c.get("ok")),
        "flatten": flatten,
        "summary": (
            f"paper exercise {symbol}: placed={len(placed)} "
            f"individual_cancels={len(cancel_log)} "
            f"(ok={sum(1 for c in cancel_log if c.get('ok'))})"
        ),
    }
    try:
        LOG.open("a", encoding="utf-8").write(json.dumps(report, default=str) + "\n")
    except OSError:
        pass
    return report
