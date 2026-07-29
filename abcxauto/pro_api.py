"""Local HTTP API for the web Pro / Focus UI → live IBKR (+ optional MDA bars).

Run with the desktop shell:
  python -m abcxauto --desktop

Or API only (UI separate):
  uvicorn abcxauto.pro_api:app --host 127.0.0.1 --port 8765

Endpoints (all local):
  GET  /api/health
  GET  /api/status
  POST /api/connect
  POST /api/disconnect
  GET  /api/book          positions + orders + account (IBKR)
  GET  /api/bars/{sym}    history (IBKR preferred, MDA fallback)
  GET  /api/activity      journal decisions + session fills
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from abcxauto.config import get_config
from abcxauto.connections import connection_status, get_connector

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST = REPO_ROOT / "web-pro" / "dist"


def _infer_order_role(
    order: dict[str, Any],
    pos_qty: float | None,
) -> str:
    ot = str(order.get("order_type") or "").upper()
    action = str(order.get("action") or "").upper()
    if "STP" in ot or ot == "TRAIL":
        return "stop"
    if ot in ("LMT", "LIMIT") and pos_qty is not None and pos_qty != 0:
        if pos_qty > 0 and action == "SELL":
            return "target"
        if pos_qty < 0 and action == "BUY":
            return "target"
    if pos_qty is None or pos_qty == 0:
        return "entry"
    return "exit"


def _order_price(order: dict[str, Any]) -> float:
    for key in ("lmt_price", "aux_price", "trail_percent"):
        v = order.get(key)
        if v is None:
            continue
        try:
            f = float(v)
            if f > 0 and f < 1e100:
                return f
        except (TypeError, ValueError):
            continue
    return 0.0


def map_positions(
    raw_positions: list[dict[str, Any]],
    raw_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map IBKR portfolio rows → UI Position shape."""
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for o in raw_orders:
        sym = str(o.get("symbol") or "").upper()
        if not sym:
            continue
        by_sym.setdefault(sym, []).append(o)

    out: list[dict[str, Any]] = []
    for p in raw_positions:
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        qty = float(p.get("quantity") or 0)
        if qty == 0:
            continue
        sec = str(p.get("sec_type") or "STK").upper()
        if sec not in ("STK", "OPT"):
            sec = "STK"
        price = float(p.get("market_price") or 0)
        avg = float(p.get("avg_cost") or 0)
        # OPT averageCost from IB is often per-share * multiplier; leave as broker reports
        upnl = float(p.get("unrealized_pnl") or 0)
        con = p.get("conId") or p.get("con_id") or ""
        stops = [
            o
            for o in by_sym.get(sym, [])
            if _infer_order_role(o, qty) == "stop"
        ]
        targets = [
            o
            for o in by_sym.get(sym, [])
            if _infer_order_role(o, qty) == "target"
        ]
        protected = sec != "STK" or bool(stops)
        parts: list[str] = []
        if stops:
            parts.append(f"SL {_order_price(stops[0]):.2f}")
        elif sec == "STK":
            parts.append("unprotected — stop missing")
        if targets:
            parts.append(f"TP {_order_price(targets[0]):.2f}")
        out.append(
            {
                "conId": str(con),
                "symbol": sym,
                "type": sec,
                "qty": qty,
                "avgCost": avg,
                "price": price,
                "uPnl": upnl,
                "details": " · ".join(parts) if parts else "open",
                "protected": protected,
            }
        )
    out.sort(key=lambda r: r["symbol"])
    return out


def map_orders(
    raw_orders: list[dict[str, Any]],
    raw_positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    qty_by: dict[str, float] = {}
    for p in raw_positions:
        sym = str(p.get("symbol") or "").upper()
        qty_by[sym] = float(p.get("quantity") or 0)

    out: list[dict[str, Any]] = []
    for o in raw_orders:
        sym = str(o.get("symbol") or "").upper()
        if not sym:
            continue
        action = str(o.get("action") or "BUY").upper()
        side = "BUY" if action.startswith("B") else "SELL"
        status = str(o.get("status") or "Submitted")
        if status not in ("Submitted", "PreSubmitted", "Filled", "Cancelled"):
            status = "Submitted"
        role = _infer_order_role(o, qty_by.get(sym))
        out.append(
            {
                "id": str(o.get("order_id") or o.get("conId") or len(out)),
                "symbol": sym,
                "side": side,
                "type": str(o.get("order_type") or "LMT"),
                "qty": float(o.get("quantity") or 0),
                "price": _order_price(o),
                "status": status,
                "role": role,
            }
        )
    return out


def map_account(raw: dict[str, Any]) -> dict[str, Any]:
    if not raw or raw.get("error"):
        return {
            "netLiq": 0.0,
            "dayPnl": 0.0,
            "cash": 0.0,
            "accountId": "",
            "error": (raw or {}).get("error"),
        }
    def f(*keys: str) -> float:
        for k in keys:
            if k in raw and raw[k] is not None:
                try:
                    return float(raw[k])
                except (TypeError, ValueError):
                    pass
        return 0.0

    return {
        "netLiq": f("netliquidation", "NetLiquidation", "net_liquidation"),
        "dayPnl": f("dailypnl", "DailyPnL", "daily_pnl"),
        "cash": f("totalcashvalue", "TotalCashValue", "cash"),
        "unrealized": f("unrealizedpnl", "UnrealizedPnL"),
        "accountId": str(raw.get("account_id") or ""),
    }


async def _ib_bars(symbol: str, range_key: str) -> list[dict[str, Any]]:
    """IBKR historical bars when TWS is connected."""
    conn = get_connector()
    if not getattr(conn, "connected", False):
        return []
    ib = getattr(conn, "ib", None)
    if ib is None:
        return []
    # duration / barSize for Focus ranges
    if range_key == "1D":
        duration, bar_size = "1 D", "5 mins"
    elif range_key == "5D":
        duration, bar_size = "5 D", "30 mins"
    else:
        duration, bar_size = "1 M", "1 day"
    try:
        from ib_insync import Stock

        contract = Stock(symbol, "SMART", "USD")
        async with conn.async_lock:
            await ib.qualifyContractsAsync(contract)
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        out: list[dict[str, Any]] = []
        for b in bars or []:
            d = getattr(b, "date", None)
            if hasattr(d, "isoformat"):
                ts = d.isoformat()
            else:
                ts = str(d)
            out.append(
                {
                    "t": ts,
                    "o": float(b.open),
                    "h": float(b.high),
                    "l": float(b.low),
                    "c": float(b.close),
                    "v": float(getattr(b, "volume", 0) or 0),
                }
            )
        return out
    except Exception:
        logger.exception("IBKR bars failed for %s", symbol)
        return []


async def _mda_bars(symbol: str, range_key: str) -> list[dict[str, Any]]:
    from abcxauto.marketdata.client import get_marketdata_client

    client = get_marketdata_client()
    if range_key == "1D":
        # MDA may only support daily — try fine then fall back
        for res, n in (("5", 78), ("15", 78), ("60", 78), ("D", 5)):
            rows = await client.get_stock_candles(
                symbol, resolution=res, countback=n
            )
            if rows:
                return rows
        return []
    if range_key == "5D":
        for res, n in (("60", 65), ("D", 10)):
            rows = await client.get_stock_candles(
                symbol, resolution=res, countback=n
            )
            if rows:
                return rows
        return []
    return await client.get_stock_candles(symbol, resolution="D", countback=42)


def create_app(*, serve_static: bool = True) -> FastAPI:
    app = FastAPI(title="ABCXAUTO Pro API", version="0.6.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"ok": "true", "service": "abcxauto-pro"}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        st = connection_status()
        cfg = get_config()
        return {
            **st,
            "ibkr_host": cfg.ibkr_host,
            "ibkr_port": cfg.ibkr_port,
            "data_mode": "live" if st.get("ibkr_connected") else "offline",
        }

    @app.post("/api/connect")
    async def connect_ibkr() -> dict[str, Any]:
        conn = get_connector()
        try:
            ok = bool(await conn.connect())
        except Exception as exc:
            logger.exception("connect failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        st = connection_status(conn)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Could not reach TWS/Gateway at "
                    f"{get_config().ibkr_host}:{get_config().ibkr_port}. "
                    "Start paper TWS and enable API."
                ),
            )
        return {"ok": True, **st}

    @app.post("/api/disconnect")
    async def disconnect_ibkr() -> dict[str, Any]:
        conn = get_connector()
        try:
            if hasattr(conn, "disconnect"):
                await conn.disconnect()
            elif hasattr(conn, "ib") and conn.ib is not None:
                conn.ib.disconnect()
                if hasattr(conn, "_connected"):
                    conn._connected = False
        except Exception as exc:
            logger.warning("disconnect: %s", exc)
        return {"ok": True, **connection_status(conn)}

    @app.get("/api/book")
    async def book() -> dict[str, Any]:
        conn = get_connector()
        live = bool(getattr(conn, "connected", False))
        if not live:
            return {
                "live": False,
                "positions": [],
                "orders": [],
                "account": map_account({}),
                "source": "offline",
            }
        try:
            raw_pos = await conn.get_positions()
            raw_ord = await conn.get_open_orders()
            raw_acct = await conn.get_account_summary()
        except Exception as exc:
            logger.exception("book snapshot failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "live": True,
            "source": "ibkr",
            "positions": map_positions(raw_pos or [], raw_ord or []),
            "orders": map_orders(raw_ord or [], raw_pos or []),
            "account": map_account(raw_acct or {}),
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    @app.get("/api/bars/{symbol}")
    async def bars(
        symbol: str,
        range: str = Query("5D", pattern="^(1D|5D|1M)$"),  # noqa: A002
    ) -> dict[str, Any]:
        sym = symbol.strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol required")
        source = "none"
        rows = await _ib_bars(sym, range)
        if rows:
            source = "ibkr"
        else:
            rows = await _mda_bars(sym, range)
            if rows:
                source = "mda"
        # Normalize to {t, c} for Focus
        bars_out: list[dict[str, Any]] = []
        for r in rows:
            t = r.get("t")
            if isinstance(t, (int, float)):
                # unix seconds
                ts = datetime.fromtimestamp(float(t), tz=timezone.utc).isoformat()
            else:
                ts = str(t)
            c = r.get("c")
            if c is None:
                continue
            bars_out.append(
                {
                    "t": ts,
                    "c": float(c),
                    "o": float(r["o"]) if r.get("o") is not None else None,
                    "h": float(r["h"]) if r.get("h") is not None else None,
                    "l": float(r["l"]) if r.get("l") is not None else None,
                }
            )
        return {
            "symbol": sym,
            "range": range,
            "source": source,
            "bars": bars_out,
        }

    @app.get("/api/activity")
    async def activity(limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        # Journal decisions
        try:
            from abcxauto.memory import get_journal

            j = get_journal()
            for d in j.recent_decisions(limit=limit) or []:
                items.append(
                    {
                        "id": f"dec-{d.get('id')}",
                        "kind": "act" if d.get("action") else "system",
                        "title": f"{(d.get('strategy') or d.get('action') or 'cycle').upper()}",
                        "body": str(d.get("rationale") or d.get("outcome") or ""),
                        "ts": d.get("ts")
                        or datetime.now(timezone.utc).isoformat(),
                        "meta": {
                            "cycle": d.get("cycle") or 0,
                            "action": str(d.get("action") or ""),
                        },
                    }
                )
        except Exception:
            logger.debug("journal activity unavailable", exc_info=True)

        # Live fills if connected
        conn = get_connector()
        if getattr(conn, "connected", False) and hasattr(conn, "get_fills"):
            try:
                for f in (await conn.get_fills()) or []:
                    sym = f.get("symbol") or ""
                    items.append(
                        {
                            "id": f"fill-{f.get('exec_id') or f.get('order_id') or len(items)}",
                            "kind": "fill",
                            "title": f"FILL · {sym}",
                            "body": (
                                f"{f.get('side') or ''} {f.get('quantity') or ''} "
                                f"@ {f.get('price') or ''}"
                            ).strip(),
                            "ts": f.get("time")
                            or datetime.now(timezone.utc).isoformat(),
                            "meta": {"symbol": sym},
                        }
                    )
            except Exception:
                logger.debug("fills unavailable", exc_info=True)

        items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
        return {"items": items[:limit], "live": bool(getattr(conn, "connected", False))}

    if serve_static and DIST.is_dir() and (DIST / "index.html").is_file():
        # SPA assets
        app.mount(
            "/assets",
            StaticFiles(directory=str(DIST / "assets")),
            name="assets",
        )

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(DIST / "index.html")

        # other static files (logo etc.)
        @app.get("/{path:path}")
        async def spa_or_static(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(status_code=404)
            candidate = DIST / path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(DIST / "index.html")

    return app


# Uvicorn entry: abcxauto.pro_api:app
app = create_app(serve_static=True)
