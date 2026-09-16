"""Call-time lookups through abcxauto.pro_desktop so tests can patch one module."""

from __future__ import annotations

from typing import Any


def get_config():
    import abcxauto.pro_desktop as pro

    return pro.get_config()


def last_card_send_label(*args: Any, **kwargs: Any):
    import abcxauto.pro_desktop as pro

    return pro.last_card_send_label(*args, **kwargs)
