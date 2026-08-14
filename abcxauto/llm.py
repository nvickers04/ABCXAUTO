"""xAI AsyncClient wrapper — chat + streaming."""

from __future__ import annotations

import logging

from xai_sdk import AsyncClient

from abcxauto.config import get_config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You own an Interactive Brokers {mode} book. Strategy is yours.
Live only follows a promoted playbook. Risk is code.
Use tools. send tickets that match ORDER EXAMPLES.
Size vs max_risk_per_trade_pct of NetLiq.
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
