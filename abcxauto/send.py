"""Thin order-dispatch façade for the agentic shell.

Agent loops should import dispatch from here only. Validation still runs
inside ``abcxauto.executor.safe_execute`` via ``proposals.validate_proposal`` —
this module does not re-validate.

Optional ``size_pct_nl`` is a clerk annotation next to quantity (hoisted by
``tool_args``). Qty stays on the wire; never invent shares from %.
"""

from __future__ import annotations

from typing import Any, Dict

from abcxauto.executor import safe_execute
from abcxauto.tool_args import SEND_SIZE_PCT_NL

__all__ = ["send_action", "safe_execute", "SEND_SIZE_PCT_NL"]


async def send_action(action: dict, connector: Any) -> Dict[str, Any]:
    """Dispatch ``action`` through the single executor path.

    Hold / noop (and other non-actionable strategies) short-circuit inside
    ``safe_execute`` with ``{status: held|blocked}`` and never hit the broker.
    Actionable strategies are validated then dispatched via the connector.
    """
    return await safe_execute(action, connector)
