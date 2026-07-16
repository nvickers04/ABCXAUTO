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

Order surface is ORDER EXAMPLES only: hold; set_risk (retune capital knobs inside
the operator risk_posture envelope); bracket / market_bracket (entries with
stop + target); oca; modify_stop / modify_target; cancel_order; bare exits with
closing_position; close_option. Never invent prices or order types.

Size each entry so stop risk fits max_risk_per_trade_pct. You may not change
risk_posture. Act via exact ORDER EXAMPLE structures. Options expirations use
YYYYMMDD from live chain data. Journal memory is part of your context — use it.
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
