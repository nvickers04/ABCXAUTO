"""End-to-end dev verify: drives AgentSession with a fake connector.

1. Sends a trade request to Grok.
2. Verifies the proposal auto-executes against the fake gateway.

Run: python scripts/dev_verify.py
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console

from abcxauto.agent import AgentSession

console = Console()


class FakeGateway:
    connected = True
    account_id = "DU1234567"
    calls: list[tuple] = []

    async def get_positions(self):
        return []

    async def get_open_orders(self):
        return []

    async def get_account_summary(self):
        return {"netliquidation": 100000}

    async def place_oca(self, **kwargs):
        self.calls.append(("place_oca", kwargs))
        return {"status": "submitted", "order_ids": [101, 102]}


async def main() -> None:
    connector = FakeGateway()
    session = AgentSession(connector, console=console)

    await session.inject(
        "[test] Propose an oca protective pair for AAPL: 10 shares LONG, "
        "stop at 140, target at 170. Use propose_order now.",
        source="test",
    )

    if not connector.calls:
        console.print("[red]VERIFY FAILED: gateway was never called[/red]")
        sys.exit(1)

    console.print(f"\n[bold green]VERIFY OK[/bold green] — gateway calls: {connector.calls}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())