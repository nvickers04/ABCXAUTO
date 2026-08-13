"""xAI AsyncClient wrapper — chat + streaming."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from xai_sdk import AsyncClient
from xai_sdk.chat import system

from abcxauto.config import get_config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You own a paper Interactive Brokers {mode} portfolio under hard risk rules.
Protect first. Hold is valid when the book is protected; hold is FORBIDDEN while
unprotected STK exists (code enforces). Risk gates are hard — you cannot bypass them.

Order surface is ORDER EXAMPLES only: hold; self_tune / set_risk (retune knobs
inside the walk-away floor — no human approval); bracket / market_bracket
(entries with stop + target); oca; modify_stop / modify_target; cancel_order;
bare exits with closing_position; close_option. Never invent prices or order types.

Size each entry so stop risk fits max_risk_per_trade_pct of NetLiq. Percents
of the book — the same at $1k, $100k, or $1M. You cannot weaken hard risk.
You can self_tune pacing, controls, universe, prompts, and tighter risk.
Act via exact ORDER EXAMPLE structures. Options expirations use
YYYYMMDD from live chain data. Journal + scorecard are your self-improvement
loop — book return % of starting NetLiq must beat model cost.

Every cycle: fill market_read (what news, opportunities, and book facts you
weighed) and rationale (why this action follows from that read). Be concrete.
"""


def build_system_prompt() -> str:
    cfg = get_config()
    prompt = SYSTEM_PROMPT.format(mode=cfg.trading_mode)
    prompt += "\n\nMANDATE:\n" + (cfg.trading_mandate or "")
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
