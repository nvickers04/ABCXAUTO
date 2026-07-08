"""Autonomous agent loop: streaming Grok replies, tool dispatch, auto-execution.

UI-agnostic core: AgentSession exposes async methods; the autonomous runner in
__main__.py and the monitoring dashboard in web.py both observe the same
session. Observers subscribe via ``add_listener`` (agent deltas, executions,
monitor snapshots).

Every validated ``propose_order`` call executes immediately — there is no
human confirmation gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from xai_sdk.chat import tool_result, user

from abcxauto.executor import execute_proposal
from abcxauto.llm import GrokClient
from abcxauto.proposals import (
    OrderProposal,
    ProposalValidationError,
    render_ticket,
    validate_proposal,
)
from abcxauto.tools import TOOL_DEFINITIONS, run_readonly_tool

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 20


def proposal_to_dict(proposal: OrderProposal) -> Dict[str, Any]:
    """JSON-friendly view of a proposal for UIs."""
    return {
        "id": proposal.id,
        "strategy": proposal.strategy,
        "params": proposal.params.model_dump(),
        "rationale": proposal.rationale,
        "max_loss": proposal.max_loss,
        "max_gain": proposal.max_gain,
        "gateway_method": proposal.gateway_method,
        "is_management": proposal.is_management,
    }


class AgentSession:
    """One autonomous Grok session over the ABCXAUTO tool set."""

    def __init__(self, connector: Any, console: Optional[Console] = None) -> None:
        self.connector = connector
        self.console = console or Console()
        self.grok = GrokClient()
        self.chat = self.grok.create_chat(TOOL_DEFINITIONS)
        self.last_execution: Optional[Dict[str, Any]] = None
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._turn_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Events (dashboard / other observers)
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def emit(self, event: Dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as e:
                logger.debug(f"Event listener error: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def inject(self, text: str, *, source: str = "system") -> None:
        """Inject a message and let Grok respond (mandate, monitor, scans)."""
        async with self._turn_lock:
            self.emit({"type": "inject", "source": source, "text": text})
            self.chat.append(user(text))
            await self._run_turn()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_turn(self) -> None:
        """Stream responses, dispatching tool calls until Grok stops calling tools."""
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._stream_once()
            self.chat.append(response)

            tool_calls = list(response.tool_calls or [])
            if not tool_calls:
                self.emit({"type": "assistant_done"})
                return

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError as e:
                    result = json.dumps({"error": f"Malformed tool arguments: {e}"})
                else:
                    if name == "propose_order":
                        result = await self._handle_propose_order(args)
                    else:
                        self.console.print(f"[dim]tool: {name}({tc.function.arguments})[/dim]")
                        self.emit({"type": "tool_call", "name": name, "args": args})
                        result = await run_readonly_tool(name, args, self.connector)
                self.chat.append(tool_result(result, tool_call_id=tc.id))
        self.console.print("[red]Tool-round limit reached; stopping this turn.[/red]")
        self.emit({"type": "assistant_done"})

    async def _stream_once(self):
        """Stream one model response, printing text deltas live."""
        response = None
        printed = False
        async for resp, chunk in self.chat.stream():
            response = resp
            if chunk.content:
                self.console.print(chunk.content, end="", soft_wrap=True)
                self.emit({"type": "assistant_delta", "text": chunk.content})
                printed = True
        if printed:
            self.console.print()
        return response

    async def _handle_propose_order(self, args: dict) -> str:
        """Validate a proposal and execute it immediately."""
        try:
            proposal = validate_proposal(
                strategy=args.get("strategy", ""),
                params=args.get("params") or {},
                rationale=args.get("rationale", ""),
                max_loss=args.get("max_loss"),
                max_gain=args.get("max_gain"),
            )
        except ProposalValidationError as e:
            self.console.print(f"[red]Proposal rejected: {e}[/red]")
            self.emit({"type": "proposal_rejected", "error": str(e)})
            return json.dumps({"error": str(e)})

        self.console.print()
        self.console.print(render_ticket(proposal))
        self.console.print(
            f"[bold cyan]Proposal #{proposal.id} ({proposal.strategy}) — auto-executing.[/bold cyan]"
        )
        self.emit({"type": "proposal", "proposal": proposal_to_dict(proposal)})

        try:
            result = await execute_proposal(proposal, self.connector)
        except Exception as e:
            logger.exception("Execution failed")
            result = {"error": str(e)}

        self.console.print(f"[bold]Broker result:[/bold] {result}")
        execution = {
            "type": "execution",
            "proposal": proposal_to_dict(proposal),
            "result": result,
            "confirmed_by": "auto",
        }
        self.last_execution = execution
        self.emit(execution)

        return json.dumps(
            {
                "status": "executed",
                "proposal_id": proposal.id,
                "note": "Order auto-executed under autonomous policy.",
                "broker_result": result,
            },
            default=str,
        )