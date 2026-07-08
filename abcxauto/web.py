"""ABCXAUTO monitoring dashboard — FastAPI + WebSocket over AgentSession.

Run: python -m abcxauto.web
Then open http://127.0.0.1:8000

Starts the autonomous agent alongside a read-only dashboard:
- live agent activity stream (Grok replies, tool calls, executions);
- live positions / P&L / open orders / protection status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from abcxauto.agent import AgentSession, proposal_to_dict
from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.marketdata.market_hours import get_session_info
from abcxauto.monitor import PortfolioMonitor

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    session: AgentSession = None
    connector: Any = None
    monitor: PortfolioMonitor = None
    sockets: Set[WebSocket] = set()
    loop: asyncio.AbstractEventLoop = None
    busy: bool = False
    scan_task: asyncio.Task = None


state = AppState()


def _market_active(cfg) -> bool:
    try:
        session = get_session_info().get("session")
    except Exception:
        return True
    if session == "regular":
        return True
    if cfg.monitor_extended_hours and session in ("premarket", "postmarket"):
        return True
    return False


async def _scan_loop(session: AgentSession, cfg) -> None:
    while True:
        await asyncio.sleep(max(60, cfg.scan_interval_s))
        if not cfg.scan_enabled:
            continue
        if not _market_active(cfg):
            continue
        await session.inject(
            "[scan] Scheduled opportunity scan. Review the account and market. "
            "Only propose new trades with a clear edge and defined risk; "
            "otherwise reply briefly that no action is warranted.",
            source="scan",
        )


def _broadcast(event: Dict[str, Any]) -> None:
    if not state.sockets or state.loop is None:
        return
    payload = json.dumps(event, default=str)

    async def _send() -> None:
        dead = []
        for ws in list(state.sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            state.sockets.discard(ws)

    state.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_send()))


def _on_agent_event(event: Dict[str, Any]) -> None:
    if event.get("type") in ("assistant_delta", "inject"):
        state.busy = True
        _broadcast({"type": "busy", "busy": True})
    if event.get("type") == "assistant_done":
        state.busy = False
        _broadcast({"type": "busy", "busy": False})
    _broadcast(event)


async def _start_agent(cfg) -> None:
    await state.session.inject(
        f"[mandate] {cfg.trading_mandate}\n"
        "Begin by auditing the account: positions, protection_status, open orders, "
        "and account summary. Act on anything urgent, then summarize your plan.",
        source="mandate",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    state.loop = asyncio.get_running_loop()
    state.connector = get_ibkr_connector()
    connected = await state.connector.connect()
    if not connected:
        logger.warning("IBKR not connected — research tools work, account/execution will fail")

    state.session = AgentSession(state.connector)
    state.session.add_listener(_on_agent_event)

    state.monitor = PortfolioMonitor(state.session, state.connector)
    if cfg.monitor_enabled and connected:
        state.monitor.start()

    if cfg.scan_enabled:
        state.scan_task = asyncio.create_task(_scan_loop(state.session, cfg))

    asyncio.create_task(_start_agent(cfg))

    logger.info(f"ABCXAUTO dashboard ready on http://{cfg.web_host}:{cfg.web_port}")
    yield

    if state.scan_task:
        state.scan_task.cancel()
        try:
            await state.scan_task
        except asyncio.CancelledError:
            pass
    state.monitor.stop()
    if state.connector.connected:
        await state.connector.disconnect()


app = FastAPI(title="ABCXAUTO", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state():
    cfg = get_config()
    last = state.session.last_execution if state.session else None
    return {
        "connected": bool(state.connector and state.connector.connected),
        "account_id": getattr(state.connector, "account_id", None),
        "mode": "paper" if cfg.is_paper else "LIVE",
        "model": cfg.model,
        "busy": state.busy,
        "monitor_running": bool(state.monitor and state.monitor.running),
        "scan_enabled": cfg.scan_enabled,
        "last_execution": last,
        "snapshot": state.monitor.latest if state.monitor else {},
    }


@app.post("/api/refresh")
async def post_refresh():
    if not state.monitor:
        return JSONResponse({"error": "monitor not ready"}, status_code=503)
    snapshot = await state.monitor.take_snapshot()
    return {"status": "ok", "snapshot": snapshot or state.monitor.latest}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.sockets.discard(ws)


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("abcxauto").setLevel(logging.INFO)

    cfg = get_config()
    config = uvicorn.Config(app, host=cfg.web_host, port=cfg.web_port, log_level="info")
    server = uvicorn.Server(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()