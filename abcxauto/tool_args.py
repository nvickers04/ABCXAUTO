"""Normalize Grok tool calls so IBKR/MDA tools actually run.

Models often send the wrong name or key (positions, get_quote, ticker=).
This is clerk work — not a prompt lecture.
"""

from __future__ import annotations

from typing import Any

TOOL_ALIASES = {
    "quotes": "quote",
    "get_quote": "quote",
    "last": "quote",
    "price": "quote",
    "ticker": "quote",
    "positions": "book",
    "position": "book",
    "account": "book",
    "inventory": "book",
    "orders": "book",
    "open_orders": "book",
    "account_summary": "book",
    "hours": "status",
    "session": "status",
    "market_hours": "status",
    "order": "send",
    "place": "send",
    "ticket": "send",
    "place_order": "send",
    "set_risk": "self_tune",
    "chain": "option_chain",
    "options": "option_chain",
    "greeks": "option_quote",
    "headlines": "news",
    "bars": "candles",
    "ohlcv": "candles",
    "history": "candles",
    "screener": "scan",
    "tape": "scan",
    "polymarket": "odds",
    "kalshi": "odds",
    "betting": "odds",
    "prediction": "odds",
}

_ARG_KEYS = {
    "symbol": ("symbol", "ticker", "underlying", "sym"),
    "symbols": ("symbols", "tickers", "tickers_list"),
    "arena": ("arena", "screen", "scan_arena"),
    "scan_code": ("scan_code", "scanCode", "code"),
    "with": ("with", "include"),
    "market_cap_above": ("market_cap_above", "marketCapAbove"),
    "market_cap_below": ("market_cap_below", "marketCapBelow"),
    "above_price": ("above_price", "abovePrice"),
    "below_price": ("below_price", "belowPrice"),
    "above_volume": ("above_volume", "aboveVolume"),
    "average_option_volume_above": (
        "average_option_volume_above",
        "averageOptionVolumeAbove",
    ),
    "usdMarketCapAbove": ("usdMarketCapAbove", "usd_market_cap_above"),
    "optVolumeAbove": ("optVolumeAbove", "opt_volume_above"),
    "avgVolumeAbove": ("avgVolumeAbove", "avg_volume_above"),
    # P/E only accepted at runtime after XML verify (not in tool schema).
    "peRatioAbove": ("peRatioAbove", "pe_ratio_above"),
    "peRatioBelow": ("peRatioBelow", "pe_ratio_below"),
    "expiration": ("expiration", "expiry", "exp", "expiration_date", "lastTradeDateOrContractMonth"),
    "strike": ("strike", "strike_price"),
    "right": ("right", "cp", "call_put", "put_call"),
    "resolution": ("resolution", "interval", "timeframe", "tf"),
    "countback": ("countback", "count", "bars", "n"),
    "strategy": ("strategy", "action", "order_type", "type"),
    "rationale": ("rationale", "reason", "why", "note"),
    "query": ("query", "q", "search", "event"),
    "target_conId": ("target_conId", "target_conid", "conId", "con_id"),
}

_SEND_HOIST = (
    "symbol",
    "direction",
    "quantity",
    "qty",
    "shares",
    "stop_price",
    "target_price",
    "entry_price",
    "price_hint",
    "action",
    "limit_price",
    "trail_percent",
    "limit_offset",
    "expiration",
    "expiry",
    "strike",
    "right",
    "conId",
    "con_id",
    "order_id",
    "new_stop_price",
    "new_limit_price",
    "closing_position",
    # Optional % of NL annotation next to quantity. Clerk hoist only —
    # qty stays on the wire; never invent shares from %.
    "size_pct_nl",
    # Playbook card this ticket comes from. Hoisted so the new-risk gate and
    # the attribution log read the same key wherever Grok put it.
    "card",
)

# Clerk-owned send annotation (not brain schema / not ORDER EXAMPLES).
SEND_SIZE_PCT_NL = "size_pct_nl"

def _first(args: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in args and args[key] not in (None, ""):
            return args[key]
    return None


def _as_symbols(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    for item in items:
        sym = str(item or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def _norm_right(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in ("C", "CALL"):
        return "C"
    if s in ("P", "PUT"):
        return "P"
    return s[:1] if s else ""


def _norm_expiration(raw: Any) -> str:
    s = str(raw or "").strip().replace("-", "").replace("/", "")
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]
    return s


OPTION_QUOTE_CAP = 8
CANDLE_CAP = 8
CHAIN_CAP = 4


def _norm_option_spec(src: dict[str, Any]) -> dict[str, Any] | None:
    symbol = _first(src, "symbol", "ticker", "underlying", "sym")
    expiration = _first(
        src, "expiration", "expiry", "exp", "expiration_date",
        "lastTradeDateOrContractMonth",
    )
    strike = _first(src, "strike", "strike_price")
    right = _first(src, "right", "cp", "call_put", "put_call")
    if symbol in (None, "") or expiration in (None, "") or strike in (None, "") or right in (None, ""):
        return None
    return {
        "symbol": str(symbol).strip().upper(),
        "expiration": _norm_expiration(expiration),
        "strike": strike,
        "right": _norm_right(right),
    }


def option_quote_specs(args: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One contract or contracts[] (max 8). Clerk normalizes aliases."""
    src = dict(args) if isinstance(args, dict) else {}
    specs: list[dict[str, Any]] = []
    raw_list = src.get("contracts") or src.get("quotes") or src.get("options")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                spec = _norm_option_spec(item)
                if spec:
                    specs.append(spec)
    one = _norm_option_spec(src)
    if one and one not in specs:
        specs.insert(0, one)
    return specs[:OPTION_QUOTE_CAP]


def _collect_symbols(*blobs: Any) -> list[str]:
    """Symbols from scan_hits rows, opportunity rows, or a flat name list."""
    out: list[str] = []

    def add(raw: Any) -> None:
        name = str(raw or "").strip().upper()
        if name and name not in out:
            out.append(name)

    for blob in blobs:
        if blob is None:
            continue
        if isinstance(blob, dict):
            rows = blob.get("rows")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        add(row.get("symbol"))
                continue
            add(blob.get("symbol"))
            continue
        if isinstance(blob, (list, tuple)):
            for item in blob:
                if isinstance(item, dict):
                    add(item.get("symbol"))
                else:
                    add(item)
            continue
        add(blob)
    return out


def fallback_quote_symbols(world: Any = None, snap: dict | None = None) -> list[str]:
    """Open STK, then this look's scan hits, then last look — SPY only if empty.

    Bare quote/news/candles used to land on SPY while flat. The live card
    forbids a same-session SPY scrape, so that fallback skipped the screen
    Grok had just pulled.
    """
    out: list[str] = []
    rows = []
    if world is not None:
        rows.extend(list(getattr(world, "positions", None) or []))
    if isinstance(snap, dict):
        rows.extend(list(snap.get("positions") or []))
    for pos in rows:
        if not isinstance(pos, dict):
            continue
        sec = str(pos.get("sec_type") or pos.get("secType") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        sym = str(pos.get("symbol") or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= 8:
            return out
    hits = []
    if isinstance(snap, dict):
        hits.extend(
            _collect_symbols(
                snap.get("scan_hits"),
                snap.get("opportunities"),
                snap.get("scan_fetched"),
            )
        )
    if world is not None:
        hits.extend(
            _collect_symbols(
                getattr(world, "opportunities", None),
                getattr(world, "scan_fetched", None),
            )
        )
    if not hits:
        try:
            from abcxauto.think_stream import last_look_for_hunt

            hits = _collect_symbols((last_look_for_hunt() or {}).get("scan_hits"))
        except Exception:
            hits = []
    for sym in hits:
        if sym not in out:
            out.append(sym)
        if len(out) >= 8:
            return out
    if not out:
        try:
            from abcxauto.lab_playbook import live_card_send_facts, live_card_skips_spy

            if live_card_send_facts().get("card") or live_card_skips_spy():
                return []
        except Exception:
            pass
        out.append("SPY")
    return out[:8]


def hoist_send_params(args: dict[str, Any]) -> dict[str, Any]:
    """Move top-level ticket fields into params (Grok often skips the nest)."""
    out = dict(args)
    params = out.get("params")
    if not isinstance(params, dict):
        params = {}
    else:
        params = dict(params)
    for key in _SEND_HOIST:
        if key in out and key not in ("strategy", "rationale", "params") and _missing(params, key):
            params[key] = out[key]
    if _missing(params, "quantity"):
        qty = _first(params, "qty", "shares") or _first(out, "qty", "shares")
        if qty not in (None, ""):
            params["quantity"] = qty
    if _missing(params, "expiration") and params.get("expiry"):
        params["expiration"] = params["expiry"]
    if params.get("expiration"):
        params["expiration"] = _norm_expiration(params["expiration"])
    if params.get("right"):
        params["right"] = _norm_right(params["right"])
    out["params"] = params
    if out.get("strategy") in (None, "") and out.get("action"):
        out["strategy"] = out["action"]
    return out


def _missing(params: dict[str, Any], key: str) -> bool:
    return params.get(key) in (None, "")


def normalize_tool_call(
    name: str,
    args: dict[str, Any] | None,
    *,
    fallback_symbols: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    raw_name = str(name or "").strip()
    canon = TOOL_ALIASES.get(raw_name.lower(), raw_name)
    src = dict(args) if isinstance(args, dict) else {}
    out: dict[str, Any] = dict(src)

    for dest, keys in _ARG_KEYS.items():
        if out.get(dest) in (None, ""):
            got = _first(src, *keys)
            if got not in (None, ""):
                out[dest] = got

    if canon in ("quote", "news", "odds", "candles"):
        if out.get("symbol") in (None, "") and out.get("symbols") in (None, "", []):
            cap = 4 if canon == "candles" else 8
            fb = [s for s in (fallback_symbols or []) if s][:cap]
            if fb:
                out["symbols"] = fb
        if canon == "quote" and isinstance(out.get("symbol"), list) and not out.get("symbols"):
            out["symbols"] = out["symbol"]
            out.pop("symbol", None)

    if canon in ("news", "odds") and out.get("symbols") in (None, "", []):
        if out.get("symbol"):
            out["symbols"] = _as_symbols(out.get("symbol"))

    if canon == "scan":
        # Empty scan must stay empty — do not seed legal_symbols / canned tape.
        if out.get("symbols") in (None, ""):
            if out.get("symbol"):
                out["symbols"] = _as_symbols(out.get("symbol"))
            else:
                out["symbols"] = []
        if isinstance(out.get("with"), str):
            out["with"] = [out["with"]]
        # Drop alias spellings after hoist so filter allowlist sees one key set.
        for alias in (
            "marketCapAbove",
            "marketCapBelow",
            "abovePrice",
            "belowPrice",
            "aboveVolume",
            "averageOptionVolumeAbove",
            "usd_market_cap_above",
            "opt_volume_above",
            "avg_volume_above",
            "pe_ratio_above",
            "pe_ratio_below",
            "screen",
            "scan_arena",
            "scanCode",
            "code",
            "include",
            "tickers",
            "tickers_list",
            "ticker",
        ):
            out.pop(alias, None)
        # Both selectors compose: arena is the universe, scan_code is the sort.
        # Dropping the sort made mega_cap+TOP_PERC_LOSE run HOT_BY_VOLUME.

    if canon in ("candles", "option_chain"):
        if isinstance(out.get("symbol"), list) and not out.get("symbols"):
            out["symbols"] = out["symbol"]
            out.pop("symbol", None)

    if canon == "option_quote":
        specs = option_quote_specs(out)
        if specs:
            out["contracts"] = specs
            out.update(specs[0])
        elif out.get("right") or out.get("expiration"):
            if out.get("right"):
                out["right"] = _norm_right(out["right"])
            if out.get("expiration"):
                out["expiration"] = _norm_expiration(out["expiration"])

    if canon == "send":
        out = hoist_send_params(out)

    return canon, out


def strip_ambiguous_last(row: dict[str, Any]) -> dict[str, Any]:
    """MDA rows keep mda_last; drop last so it cannot be used as live."""
    if not isinstance(row, dict):
        return row
    if str(row.get("source") or "").lower() != "mda":
        return row
    out = dict(row)
    if out.get("mda_last") is None and out.get("last") is not None:
        out["mda_last"] = out["last"]
    out.pop("last", None)
    return out
