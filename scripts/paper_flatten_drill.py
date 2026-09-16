"""Paper-only Flatten All drill. Tiny 1-share bracket, then the cockpit flatten path.

Noah's TWS, five minutes. This process never opens a live socket.

  python scripts/paper_flatten_drill.py

Requires: paper TWS API on 7497, empty book, desk paused (flatten_all closes
every lot). Uses client id 43 so it does not steal the desk's 42.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DRILL_PORT = 7497
DRILL_QTY = 1
DRILL_CLIENT_ID_DEFAULT = 43
LIVE_PORTS = frozenset({7496, 4001})
# Gateway paper is a different socket. This drill is TWS 7497 only.
OTHER_PAPER_PORTS = frozenset({4002})


class DrillRefused(Exception):
    """Operator-visible refuse. Not a test skip."""


def drill_client_id() -> int:
    raw = os.environ.get("ABCXAUTO_FLATTEN_DRILL_CLIENT_ID") or str(DRILL_CLIENT_ID_DEFAULT)
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise DrillRefused(f"bad client id {raw!r}") from e


def paper_7497_or_refuse(
    mode: str,
    port: Any,
    live_confirm: str = "",
) -> None:
    """Refuse anything that looks live, or any port other than TWS paper 7497."""
    normalized = str(mode or "").strip().lower() or "paper"
    try:
        port_i = int(port)
    except (TypeError, ValueError) as e:
        raise DrillRefused(f"IBKR_PORT={port!r} is not a port") from e

    if port_i in LIVE_PORTS or normalized == "live":
        raise DrillRefused(
            f"REFUSED live path: TRADING_MODE={normalized!r} IBKR_PORT={port_i}. "
            "This drill is paper TWS 7497 only."
        )
    if port_i in OTHER_PAPER_PORTS:
        raise DrillRefused(
            f"REFUSED Gateway paper port {port_i}. Run this drill against TWS 7497."
        )
    if normalized != "paper" or port_i != DRILL_PORT:
        raise DrillRefused(
            f"REFUSED TRADING_MODE={normalized!r} IBKR_PORT={port_i}. "
            "Need paper + 7497."
        )
    del live_confirm


def book_must_be_empty(positions: list, orders: list) -> None:
    """flatten_all closes the whole account. Refuse a dirty book."""
    lots = [p for p in (positions or []) if _qty(p)]
    working = list(orders or [])
    if lots or working:
        raise DrillRefused(
            f"REFUSED: book is not empty ({len(lots)} lots, {len(working)} orders). "
            "flatten_all is the cockpit nuclear path — it would close everything. "
            "Pause the desk and flatten or close those lots first."
        )


def _qty(pos: dict) -> float:
    try:
        return float(pos.get("quantity") if pos.get("quantity") is not None else pos.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _lot_id(row: dict) -> str:
    cid = row.get("conId") or row.get("con_id")
    try:
        if int(cid) > 0:
            return f"con:{int(cid)}"
    except (TypeError, ValueError):
        pass
    return f"stk:{str(row.get('symbol') or '').upper()}"


def _quote_px(quote: dict) -> float | None:
    for key in ("last", "mid", "close", "bid", "ask"):
        raw = (quote or {}).get(key)
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


def flatten_matches_book(result: dict, book: list) -> list[str]:
    """#216: success only when the independent reread is empty."""
    problems: list[str] = []
    live = [p for p in (book or []) if _qty(p)]
    success = bool((result or {}).get("success"))
    if success and live:
        problems.append(
            f"flatten_all success=True but reread still has {len(live)} lots"
        )
    if (not success) and (not live) and not (result or {}).get("book_unknown"):
        problems.append("flatten_all success=False but independent reread is empty")
    if success != (not live) and not (result or {}).get("book_unknown"):
        problems.append(
            f"success={success} does not match empty_book={not live}"
        )
    reported = {_lot_id(p) for p in (result or {}).get("remaining") or []}
    actual = {_lot_id(p) for p in live}
    if reported != actual:
        problems.append(f"remaining {sorted(reported)} != reread {sorted(actual)}")
    if live:
        failed = (result or {}).get("failed") or []
        failed_ids = {_lot_id(row) for row in failed if isinstance(row, dict)}
        missing = actual - failed_ids
        if missing:
            problems.append(f"leftover lots missing from failed: {sorted(missing)}")
        for row in (result or {}).get("failed") or []:
            if not isinstance(row, dict):
                continue
            sec = str(row.get("sec_type") or "STK").upper()
            if not sec.startswith("STK"):
                continue
            prot = str(row.get("protection") or "")
            if prot not in ("last_stop", "still_working", "none"):
                problems.append(f"{row.get('symbol')} leftover has no protection field")
    lots = [pr for pr in (result or {}).get("position_results") or [] if pr.get("method") != "noop"]
    if (result or {}).get("positions_total") and not lots:
        problems.append("positions_total set but no per-lot outcomes")
    return problems


class _Lines:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, verdict: str, detail: str) -> None:
        self.rows.append((name, verdict, detail))
        print(f"{name:<10} {verdict:<5} {detail}", flush=True)

    def failed(self) -> bool:
        return any(v == "FAIL" for _, v, _ in self.rows)


def _prepare_env() -> None:
    """Pin a non-desk client id for this process only. Never rewrite TRADING_MODE."""
    os.environ["IBKR_CLIENT_ID"] = str(drill_client_id())


async def _cleanup(conn: Any, lines: _Lines) -> None:
    try:
        await conn.flatten_all()
    except Exception as e:
        lines.add("CLEANUP", "FAIL", f"flatten_all raised: {e}")
        return
    try:
        for order in await conn.get_open_orders() or []:
            oid = order.get("order_id")
            if oid:
                await conn.cancel_order(oid)
    except Exception as e:
        lines.add("CLEANUP", "FAIL", f"cancel leftovers raised: {e}")
        return
    await asyncio.sleep(1)
    try:
        lots = [p for p in (await conn.get_positions() or []) if _qty(p)]
        orders = list(await conn.get_open_orders() or [])
    except Exception as e:
        lines.add("CLEANUP", "FAIL", f"reread raised: {e}")
        return
    if lots or orders:
        lines.add(
            "CLEANUP",
            "FAIL",
            f"still open: {len(lots)} lots, {len(orders)} orders — check TWS",
        )
        return
    lines.add("CLEANUP", "PASS", "book empty, no working orders")


async def _open_tiny_bracket(conn: Any, symbol: str) -> dict[str, Any]:
    quote = await conn.get_live_quote(symbol, fresh=True)
    if quote.get("error"):
        raise DrillRefused(f"no IBKR quote for {symbol}: {quote.get('error')}")
    last = _quote_px(quote)
    if last is None:
        raise DrillRefused(f"no last/mid for {symbol}")
    band = max(0.50, round(last * 0.003, 2))
    stop = round(last - band, 2)
    target = round(last + band, 2)
    placed = await conn.place_market_bracket(
        symbol, DRILL_QTY, "LONG", stop, target
    )
    if not placed.get("success"):
        raise DrillRefused(f"bracket failed: {placed}")
    await asyncio.sleep(1)
    return {"placed": placed, "last": last, "stop": stop, "target": target}


async def _confirm_stop(conn: Any, symbol: str) -> dict:
    from abcxauto.broker.connector import _covering_stop_order

    lots = [p for p in (await conn.get_positions() or []) if _qty(p)]
    mine = [p for p in lots if str(p.get("symbol") or "").upper() == symbol]
    if not mine:
        raise DrillRefused(f"{symbol} not in the book after the bracket")
    orders = list(await conn.get_open_orders() or [])
    covering = _covering_stop_order(mine[0], orders)
    if covering is None:
        raise DrillRefused(f"{symbol} is open but no working last-stop at IBKR")
    return {"lot": mine[0], "stop": covering}


async def _run(symbol: str) -> int:
    lines = _Lines()
    print("PAPER FLATTEN DRILL", flush=True)
    print(f"qty={DRILL_QTY} symbol={symbol} port={DRILL_PORT} (TWS paper only)", flush=True)
    print("", flush=True)

    _prepare_env()
    from abcxauto.config import get_config
    from abcxauto.broker.connection import LIVE_PORTS as CFG_LIVE
    from abcxauto.broker.connector import (
        _covering_stop_order,
        _index_stk_stops,
        get_ibkr_connector,
    )

    cfg = get_config()
    try:
        paper_7497_or_refuse(cfg.trading_mode, cfg.ibkr_port, cfg.live_confirm)
        if int(cfg.ibkr_port) in CFG_LIVE:
            raise DrillRefused("config port is a live socket")
    except DrillRefused as e:
        lines.add("GUARD", "FAIL", str(e))
        print("", flush=True)
        print("RESULT     FAIL", flush=True)
        return 2
    lines.add(
        "GUARD",
        "PASS",
        f"paper TWS {int(cfg.ibkr_port)} client_id={drill_client_id()} "
        f"(desk 42 left alone)",
    )

    conn = get_ibkr_connector()
    conn.client_id = drill_client_id()
    try:
        ok = await conn.connect(max_retries=1)
    except Exception as e:
        lines.add("CONNECT", "FAIL", f"connect refused or failed: {e}")
        print("", flush=True)
        print("RESULT     FAIL", flush=True)
        return 2
    if not ok:
        lines.add("CONNECT", "FAIL", "TWS not up on 7497 (or client id in use)")
        print("", flush=True)
        print("RESULT     FAIL", flush=True)
        return 2
    lines.add("CONNECT", "PASS", f"connected client_id={conn.client_id}")

    try:
        try:
            book_must_be_empty(
                await conn.get_positions(), await conn.get_open_orders()
            )
        except DrillRefused as e:
            lines.add("BOOK", "FAIL", str(e))
            return 2
        lines.add("BOOK", "PASS", "empty before start")

        try:
            opened = await _open_tiny_bracket(conn, symbol)
            confirmed = await _confirm_stop(conn, symbol)
        except DrillRefused as e:
            lines.add("BRACKET", "FAIL", str(e))
            await _cleanup(conn, lines)
            return 1
        lines.add(
            "BRACKET",
            "PASS",
            f"1 {symbol} long @ {opened['last']}; stop {opened['stop']} "
            f"resting oid={confirmed['stop'].get('order_id')}",
        )

        result = await conn.flatten_all()
        await asyncio.sleep(1)
        reread = [p for p in (await conn.get_positions() or []) if _qty(p)]
        problems = flatten_matches_book(result, reread)
        if problems:
            lines.add("FLATTEN", "FAIL", "; ".join(problems))
        else:
            lines.add(
                "FLATTEN",
                "PASS",
                f"success={result.get('success')} status={result.get('status')} "
                f"{result.get('positions_closed')}/{result.get('positions_total')} "
                f"closed; reread {len(reread)} lots",
            )

        # Ugly case we can do for real: cancel the stop, restore via the same helper.
        try:
            book_must_be_empty(
                await conn.get_positions(), await conn.get_open_orders()
            )
            opened = await _open_tiny_bracket(conn, symbol)
            confirmed = await _confirm_stop(conn, symbol)
            lot = confirmed["lot"]
            stop = confirmed["stop"]
            saved = _index_stk_stops([stop])
            cancelled = await conn.cancel_order(stop.get("order_id"))
            await asyncio.sleep(1)
            live_orders = list(await conn.get_open_orders() or [])
            if _covering_stop_order(lot, live_orders) is not None:
                lines.add(
                    "RESTORE",
                    "SKIP",
                    "cancel did not drop the stop (TWS kept it) — still_working path, not a restore",
                )
            elif not (cancelled.get("success") or cancelled.get("order_gone")):
                lines.add(
                    "RESTORE",
                    "SKIP",
                    f"TWS rejected the cancel: {cancelled.get('error') or cancelled}",
                )
            else:
                prot = await conn._last_stop_for_leftover_stk(
                    lot, saved_stops=saved, live_orders=live_orders
                )
                await asyncio.sleep(1)
                after = list(await conn.get_open_orders() or [])
                covering = _covering_stop_order(lot, after)
                if prot.get("protection") == "last_stop" and covering is not None:
                    lines.add(
                        "RESTORE",
                        "PASS",
                        f"cancelled oid={stop.get('order_id')}, last-stop "
                        f"oid={prot.get('protection_order_id')} @ {prot.get('stop_price')}",
                    )
                elif prot.get("protection") == "still_working":
                    lines.add("RESTORE", "PASS", "stop still_working after cancel attempt")
                else:
                    lines.add(
                        "RESTORE",
                        "FAIL",
                        f"leftover helper {prot}; covering={covering}",
                    )
        except DrillRefused as e:
            lines.add("RESTORE", "FAIL", str(e))
        except Exception as e:
            lines.add("RESTORE", "FAIL", f"restore case raised: {e}")

        lines.add(
            "NO-CLOSE",
            "SKIP",
            "cannot provoke a real close reject on paper 1-share GTC MKT "
            "without stubbing the broker — not tested",
        )
        lines.add(
            "NAKED",
            "SKIP",
            "leftover helper uses paper last/avg, so it places a last-stop; "
            "will not zero prices to fake protection=none — not tested",
        )
    finally:
        await _cleanup(conn, lines)
        try:
            await conn.disconnect()
        except Exception:
            pass

    print("", flush=True)
    if lines.failed():
        print("RESULT     FAIL", flush=True)
        return 1
    print("RESULT     PASS", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        default=os.environ.get("ABCXAUTO_FLATTEN_DRILL_SYMBOL") or "SPY",
        help="liquid STK (default SPY)",
    )
    args = parser.parse_args(argv)
    symbol = str(args.symbol or "SPY").strip().upper()
    if not symbol.isalpha() or len(symbol) > 6:
        print(f"REFUSED symbol {symbol!r}", flush=True)
        return 2
    return asyncio.run(_run(symbol))


if __name__ == "__main__":
    raise SystemExit(main())
