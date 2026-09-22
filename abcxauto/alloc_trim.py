"""Trim tickets for allocation excess — sell only the excess at the bid.

Returns tickets only. Does not place orders, cancel stops, or write wake text.
"""

from __future__ import annotations


def trim_tickets(sized: dict, *, bids: dict[str, float]) -> list[dict]:
    """Build SELL limit tickets for names with positive excess and a live bid.

    ``sized`` values look like ``{held, sized, excess, last, stop}`` from
    ``alloc_size.sized_book``. A ``None`` panel (or ``sized is None`` on the
    panel) is a bad panel — skip. Excess with no bid is skipped; no invented
    price. Never sells the sized remainder; never market; never cancels stops.
    """
    out: list[dict] = []
    if not isinstance(sized, dict):
        return out
    bid_map = bids if isinstance(bids, dict) else {}
    for symbol, panel in sized.items():
        if panel is None or not isinstance(panel, dict):
            continue
        if panel.get("sized") is None:
            continue
        try:
            excess = int(panel.get("excess") or 0)
        except (TypeError, ValueError):
            continue
        if excess <= 0:
            continue
        try:
            bid = float(bid_map.get(str(symbol), 0) or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0:
            continue
        out.append(
            {
                "symbol": str(symbol),
                "action": "SELL",
                "quantity": excess,
                "limit_price": bid,
                "reason": "alloc_excess",
            }
        )
    return out
