"""ABCXAUTO autonomous agent — `python -m abcxauto`."""

from __future__ import annotations

import asyncio
import logging
import sys

from rich.console import Console
from rich.panel import Panel

from abcxauto.agent import AgentSession
from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.marketdata.market_hours import get_session_info
from abcxauto.monitor import PortfolioMonitor

console = Console()


def _utf8_console() -> None:
    """Prevent UnicodeEncodeError on legacy Windows consoles (cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("abcxauto").setLevel(logging.INFO)
    logging.getLogger("ib_insync").setLevel(logging.WARNING)


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
    """Periodic opportunity scans during market hours."""
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


async def main() -> None:
    _utf8_console()
    _setup_logging()
    cfg = get_config()

    if not cfg.xai_api_key:
        console.print(
            "[red]XAI_API_KEY is not set.[/red] "
            "Copy .env.template to .env and fill in your keys."
        )
        return
    if not cfg.marketdata_token:
        console.print(
            "[yellow]MARKETDATA_TOKEN is not set — market data tools will return errors.[/yellow]"
        )

    console.print(
        Panel.fit(
            "[bold]ABCXAUTO[/bold] — autonomous Grok agent for IBKR trading\n"
            f"model={cfg.model}  IBKR={cfg.ibkr_host}:{cfg.ibkr_port} "
            f"({'paper' if cfg.is_paper else '[bold red]LIVE[/bold red]'})\n"
            f"monitor review every {cfg.monitor_review_s}s  "
            f"scan every {cfg.scan_interval_s}s\n"
            "Ctrl+C to stop",
            border_style="cyan",
        )
    )

    if not cfg.is_paper:
        console.print("[bold red]WARNING: live port configured. Proceed with care.[/bold red]")

    connector = get_ibkr_connector()
    connected = await connector.connect()
    if connected:
        console.print(f"[green]IBKR connected[/green] (account {connector.account_id})")
    else:
        console.print(
            "[red]IBKR not connected[/red] — start TWS/Gateway with API enabled. "
            "Research tools still work; account tools and execution will fail."
        )

    session = AgentSession(connector, console=console)
    monitor = PortfolioMonitor(session, connector)
    if cfg.monitor_enabled and connected:
        monitor.start()
        console.print(
            f"[dim]Monitor running: P&L snapshot every {cfg.monitor_poll_s}s, "
            f"Grok review every {cfg.monitor_review_s}s.[/dim]"
        )

    scan_task = asyncio.create_task(_scan_loop(session, cfg)) if cfg.scan_enabled else None

    try:
        await session.inject(
            f"[mandate] {cfg.trading_mandate}\n"
            "Begin by auditing the account: positions, protection_status, open orders, "
            "and account summary. Act on anything urgent, then summarize your plan.",
            source="mandate",
        )
        while True:
            await asyncio.sleep(3600)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        if scan_task:
            scan_task.cancel()
            try:
                await scan_task
            except asyncio.CancelledError:
                pass
        monitor.stop()
        if connector.connected:
            await connector.disconnect()
        console.print("[dim]Agent stopped.[/dim]")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())