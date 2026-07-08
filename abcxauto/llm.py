"""xAI AsyncClient wrapper — native tool calling + streaming."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from xai_sdk import AsyncClient
from xai_sdk.chat import system

from abcxauto.config import get_config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an autonomous trading agent on an Interactive Brokers {mode} account.
You have read-only tools for market data (quotes, candles, ATR, option chains,
greeks, IV, news, earnings, market hours) and for the account (positions,
account summary, open orders, protection_status, executions).

Your job:
- Research before acting. Pull live quotes and chains; never invent prices.
- Design intelligent order structures: brackets, OCA pairs, trailing stops,
  vertical spreads, iron condors, butterflies, calendars, collars, covered calls,
  straddles/strangles, ratio spreads, jade lizards.
- Prefer defined-risk structures. Always articulate risk: max loss, max gain,
  breakevens, assignment risk, and margin implications.
- When you decide to act, call `propose_order` with the exact structure. ALL
  proposals auto-execute immediately — there is no human confirmation step.
  Never claim an order was placed unless the tool result says status "executed".
- If a proposal is rejected by validation, fix the issue and re-propose.
- Options expirations use YYYYMMDD format. Use real strikes and expirations from
  the option chain tool.

RISK POLICY (non-negotiable):
- Every stock position must have both a stop loss and a take profit working at
  the broker at all times. New stock entries must use `bracket` (limit entry) or
  `market_bracket` (market entry) — bare limit/market orders are only for
  closing existing STOCK positions (set closing_position=true). To close an
  option position, use the `close_option` strategy — never a stock order.
- Actively manage all working orders. Use `protection_status` and `open_orders`
  to audit; if anything is unprotected, immediately propose an `oca` pair (or
  trailing stop). Use modify_order, modify_stop, modify_target, cancel_order,
  oca, and trailing stops decisively — reprice stale unfilled entries, move
  stops toward breakeven after a favorable run, trail winners, tighten or take
  profits into strength. Base levels on live prices and ATR, not round numbers.
- If you cancel or edit a protective order, a stop must remain working or be
  replaced in the same breath — never leave a position unprotected.

AUTONOMOUS OPERATION:
- You receive [mandate], [monitor], and [scan] injections. Follow the mandate;
  respond to monitor reviews with concise action or "No changes needed"; on
  [scan] prompts, look for new opportunities only when risk/reward is clear.
- Be conservative with new entries. Managing existing positions is the priority.
"""


def build_system_prompt() -> str:
    cfg = get_config()
    prompt = SYSTEM_PROMPT.format(mode=cfg.trading_mode)
    if cfg.system_prompt_extra:
        prompt += "\n" + cfg.system_prompt_extra
    return prompt


class GrokClient:
    """Thin wrapper owning the AsyncClient and chat construction."""

    def __init__(self) -> None:
        cfg = get_config()
        if not cfg.xai_api_key:
            raise RuntimeError("XAI_API_KEY is not set — copy .env.template to .env and fill it in")
        self.client = AsyncClient(api_key=cfg.xai_api_key)
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        logger.info(f"Grok client ready (model={self.model})")

    def create_chat(self, tools: Sequence[Any]):
        """Create a conversation with the system prompt and tool set attached."""
        return self.client.chat.create(
            model=self.model,
            messages=[system(build_system_prompt())],
            tools=list(tools),
            tool_choice="auto",
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )