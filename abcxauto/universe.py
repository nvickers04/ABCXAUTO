"""Live IBKR screens. Nothing about where to hunt persists between looks."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")
# Units / warrants / rights often pollute scanners (AACOU, DMAAR…).
# 5+ char suffix only — keep SHORT names (LOW, AIR, MU) and 4-char CORP/ETF.
_JUNK_SUFFIX_RE = re.compile(r"^[A-Z]{4,}[UWRX]$")
_XML_CODE_RE = re.compile(r"<code>([^<]+)</code>|code=[\"']([^\"']+)[\"']")
_INDUSTRY_CODE_RE = re.compile(r"(?i)industry|sector")


def _stk_scan_screen(
    label: str,
    scan_code: str,
    *,
    above_volume: int = 500_000,
    rows: int = 30,
) -> dict[str, Any]:
    """US major STK screen — scanCode is the sort. Documented IBKR codes only."""
    return {
        "label": label,
        "group": "scans",
        "ibkr": {
            "scanCode": scan_code,
            "locationCode": "STK.US.MAJOR",
            "stockTypeFilter": "CORP,ETF",
            "abovePrice": 5.0,
            "aboveVolume": above_volume,
            "rows": rows,
        },
    }


# Screen name = sort. Codes from IBKR ScanCode / TWS scanner docs (STK.US.MAJOR class).
_SCAN_ARENA_SPECS: list[tuple[str, str, str, dict[str, int]]] = [
    ("most_active", "Most active (IBKR)", "MOST_ACTIVE", {"above_volume": 1_000_000, "rows": 40}),
    ("top_gainers", "Top % gainers (IBKR)", "TOP_PERC_GAIN", {}),
    ("top_losers", "Top % losers (IBKR)", "TOP_PERC_LOSE", {}),
    ("hot_by_volume", "Hot by volume (IBKR)", "HOT_BY_VOLUME", {}),
    ("hot_by_price", "Hot by price (IBKR)", "HOT_BY_PRICE", {}),
    ("hot_by_price_range", "Hot by price range (IBKR)", "HOT_BY_PRICE_RANGE", {}),
    ("hot_by_opt_volume", "Hot by option volume (IBKR)", "HOT_BY_OPT_VOLUME", {}),
    ("top_trade_count", "Top trade count (IBKR)", "TOP_TRADE_COUNT", {}),
    ("top_trade_rate", "Top trade rate (IBKR)", "TOP_TRADE_RATE", {}),
    ("top_volume_rate", "Top volume rate (IBKR)", "TOP_VOLUME_RATE", {}),
    ("top_price_range", "Top price range (IBKR)", "TOP_PRICE_RANGE", {}),
    ("top_open_perc_gain", "Top open % gainers (IBKR)", "TOP_OPEN_PERC_GAIN", {}),
    ("top_open_perc_lose", "Top open % losers (IBKR)", "TOP_OPEN_PERC_LOSE", {}),
    ("high_open_gap", "High open gap (IBKR)", "HIGH_OPEN_GAP", {}),
    ("low_open_gap", "Low open gap (IBKR)", "LOW_OPEN_GAP", {}),
    ("most_active_usd", "Most active USD (IBKR)", "MOST_ACTIVE_USD", {}),
    ("most_active_avg_usd", "Most active avg USD (IBKR)", "MOST_ACTIVE_AVG_USD", {}),
    ("opt_volume_most_active", "Option volume most active (IBKR)", "OPT_VOLUME_MOST_ACTIVE", {}),
    ("high_opt_imp_volat", "High option IV (IBKR)", "HIGH_OPT_IMP_VOLAT", {}),
    ("low_opt_imp_volat", "Low option IV (IBKR)", "LOW_OPT_IMP_VOLAT", {}),
    ("top_opt_imp_volat_gain", "Top option IV gainers (IBKR)", "TOP_OPT_IMP_VOLAT_GAIN", {}),
    ("top_opt_imp_volat_lose", "Top option IV losers (IBKR)", "TOP_OPT_IMP_VOLAT_LOSE", {}),
]

# Screens backed by a real IBKR ScannerSubscription spec only. No seed lists.
ARENA_CATALOG: dict[str, dict[str, Any]] = {
    "mega_cap": {
        "label": "Mega cap (IBKR)",
        "group": "caps",
        "ibkr": {
            "scanCode": "HOT_BY_VOLUME",
            "locationCode": "STK.US.MAJOR",
            "marketCapAbove": 200_000_000_000,
            "stockTypeFilter": "CORP",
            "abovePrice": 5.0,
            "aboveVolume": 500_000,
            "rows": 25,
        },
    },
    "large_cap": {
        "label": "Large cap (IBKR)",
        "group": "caps",
        "ibkr": {
            "scanCode": "HOT_BY_VOLUME",
            "locationCode": "STK.US.MAJOR",
            "marketCapAbove": 10_000_000_000,
            "marketCapBelow": 200_000_000_000,
            "stockTypeFilter": "CORP",
            "abovePrice": 5.0,
            "aboveVolume": 250_000,
            "rows": 30,
        },
    },
    "mid_cap": {
        "label": "Mid cap (IBKR)",
        "group": "caps",
        "ibkr": {
            "scanCode": "HOT_BY_VOLUME",
            "locationCode": "STK.US.MAJOR",
            "marketCapAbove": 2_000_000_000,
            "marketCapBelow": 10_000_000_000,
            "stockTypeFilter": "CORP",
            "abovePrice": 5.0,
            "aboveVolume": 250_000,
            "rows": 30,
        },
    },
    **{
        screen_id: _stk_scan_screen(label, code, **kw)
        for screen_id, label, code, kw in _SCAN_ARENA_SPECS
    },
}

# Documented meaning of each standing scanCode. Value still comes from IBKR.
_SCAN_METRIC_NAMES: dict[str, str] = {
    "MOST_ACTIVE": "volume",
    "TOP_PERC_GAIN": "percent_change",
    "TOP_PERC_LOSE": "percent_change",
    "HOT_BY_VOLUME": "volume",
    "HOT_BY_PRICE": "price",
    "HOT_BY_PRICE_RANGE": "price_range",
    "HOT_BY_OPT_VOLUME": "option_volume",
    "TOP_TRADE_COUNT": "trade_count",
    "TOP_TRADE_RATE": "trade_rate",
    "TOP_VOLUME_RATE": "volume_rate",
    "TOP_PRICE_RANGE": "price_range",
    "TOP_OPEN_PERC_GAIN": "open_percent_change",
    "TOP_OPEN_PERC_LOSE": "open_percent_change",
    "HIGH_OPEN_GAP": "open_gap",
    "LOW_OPEN_GAP": "open_gap",
    "MOST_ACTIVE_USD": "usd_volume",
    "MOST_ACTIVE_AVG_USD": "avg_usd_volume",
    "OPT_VOLUME_MOST_ACTIVE": "option_volume",
    "HIGH_OPT_IMP_VOLAT": "option_iv",
    "LOW_OPT_IMP_VOLAT": "option_iv",
    "TOP_OPT_IMP_VOLAT_GAIN": "option_iv_change",
    "TOP_OPT_IMP_VOLAT_LOSE": "option_iv_change",
}


def scan_metric_name(scan_code: str | None) -> str | None:
    """What this scanCode ranked by. None if the code is unknown."""
    code = str(scan_code or "").strip().upper()
    return _SCAN_METRIC_NAMES.get(code)


def _build_known_scan_codes() -> dict[str, dict[str, Any]]:
    """Standing IBKR scanCodes from group=scans screens (documented TWS ids only)."""
    out: dict[str, dict[str, Any]] = {}
    for meta in ARENA_CATALOG.values():
        if meta.get("group") != "scans":
            continue
        ibkr = meta.get("ibkr") or {}
        code = str(ibkr.get("scanCode") or "").strip().upper()
        if not code:
            continue
        out.setdefault(code, dict(ibkr))
    return out


def _build_scan_code_to_screen() -> dict[str, str]:
    out: dict[str, str] = {}
    for screen_id, meta in ARENA_CATALOG.items():
        if meta.get("group") != "scans":
            continue
        ibkr = meta.get("ibkr") or {}
        code = str(ibkr.get("scanCode") or "").strip().upper()
        if code and code not in out:
            out[code] = screen_id
    return out


KNOWN_SCAN_CODES: dict[str, dict[str, Any]] = _build_known_scan_codes()
_SCAN_CODE_TO_SCREEN: dict[str, str] = _build_scan_code_to_screen()


def known_scan_codes() -> list[str]:
    """Documented IBKR scanCodes the clerk will run (tool JSON scan_code=)."""
    return list(KNOWN_SCAN_CODES.keys())


def known_screen_keys() -> list[str]:
    """Tool JSON arena= keys: catalog screen ids + standing IBKR scanCodes."""
    out = list(ARENA_CATALOG.keys())
    for code in KNOWN_SCAN_CODES:
        if code not in out:
            out.append(code)
    return out


def scan_screen_catalog() -> list[dict[str, Any]]:
    """Bare scan() options. One row per live screen — not a fetch."""
    rows: list[dict[str, Any]] = []
    for screen_id, meta in ARENA_CATALOG.items():
        ibkr = meta.get("ibkr") if isinstance(meta, dict) else {}
        if not isinstance(ibkr, dict):
            ibkr = {}
        code = str(ibkr.get("scanCode") or "").strip().upper()
        rows.append(
            {
                "arena": screen_id,
                "scan_code": code,
                "metric": scan_metric_name(code) or "",
                "label": str((meta or {}).get("label") or screen_id),
                "group": str((meta or {}).get("group") or ""),
            }
        )
    return rows


def _unknown_screen_error(key: str) -> dict[str, Any]:
    valid = known_screen_keys()
    return {
        "ok": False,
        "error": f"unknown screen: {key}; valid=" + ",".join(valid),
        "arenas": valid,
        "screens": valid,
    }


# Optional clerk filters this look only — native ScannerSubscription fields.
# Snake tool args → IBKR attribute on ScannerSubscription.
_SCAN_NATIVE_FILTERS: dict[str, tuple[str, type]] = {
    "market_cap_above": ("marketCapAbove", float),
    "market_cap_below": ("marketCapBelow", float),
    "above_price": ("abovePrice", float),
    "below_price": ("belowPrice", float),
    "above_volume": ("aboveVolume", int),
    "average_option_volume_above": ("averageOptionVolumeAbove", int),
}

_STOCK_TYPE_VALUES: dict[str, str] = {
    "corp": "CORP",
    "etf": "ETF",
    "both": "CORP,ETF",
    "corp,etf": "CORP,ETF",
    "etf,corp": "CORP,ETF",
}

# Bounded TagValue allowlist (clerk, not SYSTEM). Exact IBKR tag names.
_SCAN_TAG_FILTERS: frozenset[str] = frozenset(
    {"usdMarketCapAbove", "optVolumeAbove", "avgVolumeAbove"}
)

# P/E TagValues — accepted only after reqScannerParameters XML verifies the code.
_PE_TAG_CANDIDATES: frozenset[str] = frozenset({"peRatioAbove", "peRatioBelow"})

# Non-filter keys allowed on scan() after tool_args normalize.
_SCAN_BASE_KEYS: frozenset[str] = frozenset(
    {"arena", "scan_code", "symbols", "symbol", "with", "include", "stock_type"}
)

_SCANNER_XML_CACHE: dict[str, Any] = {"ts": 0.0, "xml": ""}
_PE_TAG_CACHE: dict[str, Any] = {"ts": 0.0, "tags": frozenset()}
_INDUSTRY_TAG_CACHE: dict[str, Any] = {"ts": 0.0, "tags": frozenset()}
_SCANNER_XML_TTL_S = 3600.0


def _xml_has_scanner_code(xml: str, code: str) -> bool:
    """True when reqScannerParameters XML lists this filter code (not a guess)."""
    if not xml or not code:
        return False
    needle = f"<code>{code}</code>"
    if needle in xml:
        return True
    return f'code="{code}"' in xml or f"code='{code}'" in xml


def _scanner_codes_from_xml(xml: str) -> frozenset[str]:
    found: set[str] = set()
    for match in _XML_CODE_RE.finditer(str(xml or "")):
        code = (match.group(1) or match.group(2) or "").strip()
        if code:
            found.add(code)
    return frozenset(found)


def _pe_tags_from_xml(xml: str) -> frozenset[str]:
    return frozenset(t for t in _PE_TAG_CANDIDATES if _xml_has_scanner_code(xml, t))


def _industry_tags_from_xml(xml: str) -> frozenset[str]:
    """Codes whose names are industry/sector. Empty unless XML listed them."""
    return frozenset(
        code for code in _scanner_codes_from_xml(xml) if _INDUSTRY_CODE_RE.search(code)
    )


def reset_pe_tag_cache() -> None:
    """Tests — also clears the shared scanner-XML and industry caches."""
    _PE_TAG_CACHE.update(ts=0.0, tags=frozenset())
    _INDUSTRY_TAG_CACHE.update(ts=0.0, tags=frozenset())
    _SCANNER_XML_CACHE.update(ts=0.0, xml="")


def reset_industry_tag_cache() -> None:
    """Tests."""
    reset_pe_tag_cache()


async def _scanner_parameters_xml(connector: Any = None) -> str:
    now = time.monotonic()
    cached = str(_SCANNER_XML_CACHE.get("xml") or "")
    ts = float(_SCANNER_XML_CACHE.get("ts") or 0)
    if cached and (now - ts) < _SCANNER_XML_TTL_S:
        return cached
    if connector is None or not getattr(connector, "connected", False):
        return ""
    ib = getattr(connector, "ib", None)
    if ib is None or not hasattr(ib, "reqScannerParametersAsync"):
        return ""
    try:
        lock = getattr(connector, "async_lock", None)
        if lock is not None:
            async with lock:
                xml = await ib.reqScannerParametersAsync()
        else:
            xml = await ib.reqScannerParametersAsync()
    except Exception:
        logger.exception("reqScannerParameters failed")
        return ""
    text = str(xml or "")
    if text:
        _SCANNER_XML_CACHE.update(ts=now, xml=text)
    return text


async def verified_pe_tags(connector: Any = None) -> frozenset[str]:
    """P/E TagValues present in live reqScannerParameters XML. Empty if unverified."""
    now = time.monotonic()
    cached = _PE_TAG_CACHE.get("tags") or frozenset()
    if cached and (now - float(_PE_TAG_CACHE.get("ts") or 0)) < _SCANNER_XML_TTL_S:
        return frozenset(cached)
    xml = await _scanner_parameters_xml(connector)
    if not xml:
        return frozenset()
    found = _pe_tags_from_xml(xml)
    _PE_TAG_CACHE.update(ts=now, tags=found)
    return found


async def verified_industry_tags(connector: Any = None) -> frozenset[str]:
    """Industry/sector TagValues present in live scanner XML. Empty if unverified.

    Does not invent a field name. Codes are taken from the XML and kept only
    when the code itself names industry or sector.
    """
    now = time.monotonic()
    cached = _INDUSTRY_TAG_CACHE.get("tags") or frozenset()
    if cached and (now - float(_INDUSTRY_TAG_CACHE.get("ts") or 0)) < _SCANNER_XML_TTL_S:
        return frozenset(cached)
    xml = await _scanner_parameters_xml(connector)
    if not xml:
        return frozenset()
    found = _industry_tags_from_xml(xml)
    _INDUSTRY_TAG_CACHE.update(ts=now, tags=found)
    return found


def _normalize_stock_type(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    mapped = _STOCK_TYPE_VALUES.get(text.lower())
    if mapped:
        return mapped
    parts = [p.strip().upper() for p in text.split(",") if p.strip()]
    if parts and all(p in ("CORP", "ETF") for p in parts):
        if set(parts) == {"CORP", "ETF"}:
            return "CORP,ETF"
        return parts[0]
    return None


def parse_scan_filters(
    args: dict[str, Any] | None,
    *,
    pe_tags: frozenset[str] | None = None,
    industry_tags: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Clerk allowlist for optional IBKR filters this look. Unknown keys → error.

    P/E and industry/sector tags are accepted only when the matching verified
    XML set contains the code. stock_type is native (CORP / ETF / both).
    No persist. Does not invent TagValue names.
    """
    src = dict(args) if isinstance(args, dict) else {}
    allowed_pe = frozenset(pe_tags or ())
    allowed_industry = frozenset(industry_tags or ())
    allowed_tags = set(_SCAN_TAG_FILTERS) | set(allowed_pe) | set(allowed_industry)
    allowed_keys = set(_SCAN_BASE_KEYS) | set(_SCAN_NATIVE_FILTERS) | allowed_tags

    unknown = sorted(
        str(k)
        for k, v in src.items()
        if str(k) not in allowed_keys and v not in (None, "", [], {})
    )
    if unknown:
        return {
            "ok": False,
            "error": f"unknown scan key(s): {', '.join(unknown)}",
        }

    native: dict[str, Any] = {}
    tags: dict[str, str] = {}
    applied: dict[str, Any] = {}

    if src.get("stock_type") not in (None, ""):
        stock_type = _normalize_stock_type(src.get("stock_type"))
        if stock_type is None:
            return {"ok": False, "error": "invalid stock_type (CORP | ETF | both)"}
        native["stockTypeFilter"] = stock_type
        applied["stock_type"] = stock_type

    for snake, (ib_name, caster) in _SCAN_NATIVE_FILTERS.items():
        if src.get(snake) in (None, ""):
            continue
        try:
            val = caster(src[snake])
        except (TypeError, ValueError):
            return {"ok": False, "error": f"invalid {snake}"}
        native[ib_name] = val
        applied[snake] = val

    for tag in sorted(allowed_tags):
        if src.get(tag) in (None, ""):
            continue
        tags[tag] = str(src[tag])
        applied[tag] = tags[tag]

    return {
        "ok": True,
        "native": native,
        "tags": tags,
        "applied": applied,
    }


def merge_scan_filters_into_spec(
    ibkr_spec: dict[str, Any] | None,
    filters: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Overlay clerk native + TagValue filters onto one-look IBKR spec. No persist."""
    applied = dict((filters or {}).get("applied") or {})
    if not ibkr_spec:
        return None, applied
    spec = dict(ibkr_spec)
    for ib_name, val in ((filters or {}).get("native") or {}).items():
        spec[ib_name] = val
    tag_map = dict((filters or {}).get("tags") or {})
    if tag_map:
        spec["filterTags"] = dict(tag_map)
    elif "filterTags" in spec:
        spec.pop("filterTags", None)
    return spec, applied


def _usd_to_scanner_millions(usd: Any) -> float | None:
    """Clerk specs are raw USD. IBKR scanner cap filters are millions of USD."""
    try:
        v = float(usd)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v / 1e6 if v >= 1_000_000 else v


def _with_spec_cap_applied(
    spec: dict[str, Any] | None,
    applied: dict[str, Any],
) -> dict[str, Any]:
    """Echo the screen's cap so an empty mega/large screen is not a silent miss."""
    out = dict(applied or {})
    if not spec:
        return out
    if spec.get("marketCapAbove") is not None and "market_cap_above" not in out:
        out["market_cap_above"] = spec["marketCapAbove"]
    if spec.get("marketCapBelow") is not None and "market_cap_below" not in out:
        out["market_cap_below"] = spec["marketCapBelow"]
    if spec.get("stockTypeFilter") is not None and "stock_type" not in out:
        out["stock_type"] = spec["stockTypeFilter"]
    return out


def _resolve_one_selector(key: str) -> dict[str, Any]:
    """One catalog screen id or standing IBKR scanCode."""
    if not key:
        return {"ok": False, "error": "arena or scan_code required"}
    lower = key.lower()
    if lower in ARENA_CATALOG:
        meta = ARENA_CATALOG[lower]
        ibkr = dict(meta["ibkr"]) if meta.get("ibkr") else None
        if not ibkr:
            return _unknown_screen_error(key)
        return {
            "ok": True,
            "arena_id": lower,
            "scan_code": str(ibkr.get("scanCode") or "") or None,
            "ibkr": ibkr,
        }
    code = key.upper()
    if code in KNOWN_SCAN_CODES:
        screen_id = _SCAN_CODE_TO_SCREEN.get(code)
        return {
            "ok": True,
            "arena_id": screen_id,
            "scan_code": code,
            "ibkr": dict(KNOWN_SCAN_CODES[code]),
        }
    return _unknown_screen_error(key)


def resolve_screen(
    arena: str | None = None,
    scan_code: str | None = None,
) -> dict[str, Any]:
    """Resolve one screen. arena is the screen id; scan_code is the sort.

    Both together is a compose, not an error: mega_cap + TOP_PERC_LOSE keeps the
    cap filter and ranks losers.
    """
    raw_arena = str(arena or "").strip()
    raw_code = str(scan_code or "").strip().upper()
    if raw_arena and raw_code:
        if raw_code not in KNOWN_SCAN_CODES:
            return _unknown_screen_error(raw_code)
        base = _resolve_one_selector(raw_arena)
        if not base.get("ok"):
            return base
        ibkr = dict(base.get("ibkr") or {})
        if not ibkr:
            return {
                "ok": False,
                "error": "scan filters require an IBKR screen|scan_code",
            }
        ibkr["scanCode"] = raw_code
        return {
            "ok": True,
            "arena_id": base.get("arena_id"),
            "scan_code": raw_code,
            "ibkr": ibkr,
        }
    return _resolve_one_selector(raw_arena or raw_code)


# Three common sorts. Catalog lists them; scan() no longer auto-fetches the trio.
FLUSH_DEFAULT_SCREENS: tuple[tuple[str, str], ...] = (
    ("most_active", "MOST_ACTIVE"),
    ("top_losers", "TOP_PERC_LOSE"),
    ("top_gainers", "TOP_PERC_GAIN"),
)


def flush_default_jobs() -> list[dict[str, str]]:
    return [{"arena": arena, "scan_code": code} for arena, code in FLUSH_DEFAULT_SCREENS]


def is_flush_default_screen(arena: str = "", scan_code: str = "") -> bool:
    """True for a bare look or one of the three flush sort pages.

    Cap-screen composes (mega_cap + TOP_PERC_LOSE) are extra screens.
    """
    raw_arena = str(arena or "").strip()
    raw_code = str(scan_code or "").strip()
    if not raw_arena and not raw_code:
        return True
    resolved = resolve_screen(
        arena=raw_arena or None,
        scan_code=raw_code or None,
    )
    if not resolved.get("ok"):
        return False
    aid = str(resolved.get("arena_id") or "").strip().lower()
    code = str(resolved.get("scan_code") or "").strip().upper()
    return (aid, code) in FLUSH_DEFAULT_SCREENS


def _large_mega_cap_above_usd() -> float:
    spec = (ARENA_CATALOG.get("large_cap") or {}).get("ibkr") or {}
    try:
        val = float(spec.get("marketCapAbove") or 0)
    except (TypeError, ValueError):
        val = 0.0
    return val if val > 0 else 10_000_000_000.0


def flush_cap_filters(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Overlay large/mega floor as native USD. ``_ibkr_scan`` converts to millions tags.

    Do not send raw mega $200B as the only floor — that emptied large names.
    A clerk-supplied cap on this call wins.
    """
    out = dict(base) if isinstance(base, dict) else {}
    native = dict(out.get("native") or {})
    tags = dict(out.get("tags") or {})
    applied = dict(out.get("applied") or {})
    if native.get("marketCapAbove") is None and "usdMarketCapAbove" not in tags:
        floor = _large_mega_cap_above_usd()
        native["marketCapAbove"] = floor
        applied["market_cap_above"] = floor
    out["ok"] = True
    out["native"] = native
    out["tags"] = tags
    out["applied"] = applied
    return out


async def pull_one_screen(
    connector: Any = None,
    *,
    arena: str | None = None,
    scan_code: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One IBKR screen this look. Empty IBKR result stays empty. No persist."""
    resolved = resolve_screen(arena=arena, scan_code=scan_code)
    if not resolved.get("ok"):
        return resolved
    ibkr_spec, applied = merge_scan_filters_into_spec(resolved.get("ibkr"), filters)
    applied = _with_spec_cap_applied(ibkr_spec, applied)
    if (filters or {}).get("applied") and ibkr_spec is None:
        return {
            "ok": False,
            "error": "scan filters require an IBKR screen|scan_code",
            "applied": dict((filters or {}).get("applied") or {}),
        }
    if ibkr_spec is None:
        return _unknown_screen_error(str(arena or scan_code or ""))
    if connector is None:
        return {
            "ok": False,
            "error": "IBKR required for live screen",
            "arena_id": resolved.get("arena_id"),
            "scan_code": resolved.get("scan_code"),
            "applied": applied,
        }
    scan_out = await _ibkr_scan(connector, ibkr_spec)
    if isinstance(scan_out, dict) and not scan_out.get("ok", True):
        return {
            "ok": False,
            "error": scan_out.get("error") or "IBKR scanner error",
            "arena_id": resolved.get("arena_id"),
            "scan_code": resolved.get("scan_code"),
            "applied": applied,
            "empty": False,
        }
    if isinstance(scan_out, dict):
        pulled = list(scan_out.get("symbols") or [])
        rows = list(scan_out.get("rows") or [])
        ibkr_rows = int(scan_out.get("ibkr_rows") or len(rows) or len(pulled))
        kept = int(scan_out.get("kept") or len(pulled))
    else:
        pulled = list(scan_out or [])
        rows = []
        ibkr_rows = len(pulled)
        kept = len(pulled)
    empty = kept == 0
    return {
        "ok": True,
        "arena_id": resolved.get("arena_id"),
        "scan_code": resolved.get("scan_code"),
        "source": "empty" if empty else "ibkr",
        "empty": empty,
        "symbols": list(pulled),
        "rows": rows,
        "applied": applied,
        "ibkr_rows": ibkr_rows,
        "kept": kept,
    }


def is_common_equity_symbol(symbol: str) -> bool:
    """Reject obvious unit/warrant/right tickers that scanners often dump."""
    sym = str(symbol or "").upper().strip()
    if not sym or not _TICKER_RE.match(sym):
        return False
    # Prefer dotted class shares (BRK.B); reject digit junk / when-issued noise.
    if any(ch.isdigit() for ch in sym.replace(".", "")):
        return False
    if _JUNK_SUFFIX_RE.match(sym):
        return False
    return True


# Ranking only. Not a send floor. 2x/3x/inverse/vol/crypto-beta products the
# IBKR gainer screens dump as lottery prints.
LEVERED_SCAN_ETFS = frozenset(
    {
        "TQQQ", "SQQQ", "QLD", "QID", "UPRO", "SPXU", "SPXL", "SPXS", "SSO", "SDS",
        "UDOW", "SDOW", "TNA", "TZA", "FAS", "FAZ", "ERX", "ERY", "DIG", "DUG",
        "SOXL", "SOXS", "TECL", "TECS", "LABU", "LABD", "NAIL", "DPST", "CURE",
        "NUGT", "DUST", "JNUG", "JDST", "YINN", "YANG", "TMF", "TMV", "TBT", "TBF",
        "UVXY", "SVXY", "VIXY", "VIXM", "BOIL", "KOLD", "AGQ", "ZSL", "UGL", "GLL",
        "FNGU", "FNGD", "WEBL", "WEBS", "HIBL", "HIBS", "TSLL", "TSLQ", "NVDL",
        "NVDQ", "NVDX", "CONL", "CONI", "MSTU", "MSTZ", "MSTX", "BITX", "BITI",
        "ETHU", "ETHD", "AMZU", "AAPX", "MSFX", "GOOX", "METU", "PSQ",
    }
)
_LEVERED_NAME_RE = re.compile(
    r"(?i)\b(?:2x|3x|-2x|-3x|ultrapro|ultrashort|ultra |inverse |levered |direxion)\b"
)
# Cheap lottery prints. Code constant, not parsed from card prose.
MICRO_LAST_MAX = 15.0
MICRO_MARKET_CAP_MAX = 300_000_000.0


def scan_skip_class(row: dict[str, Any] | None) -> str:
    """``levered`` / ``micro`` / ``""``. Scan ranking color, not a send refuse."""
    if not isinstance(row, dict):
        return ""
    tagged = str(row.get("skip_class") or "").strip().lower()
    if tagged in ("levered", "micro"):
        return tagged
    sym = str(row.get("symbol") or "").upper().strip()
    if sym in LEVERED_SCAN_ETFS:
        return "levered"
    st = str(row.get("stock_type") or row.get("stockType") or "").upper()
    if st in ("ETF", "ETN"):
        blob = " ".join(
            str(row.get(k) or "") for k in ("long_name", "name", "description")
        )
        if _LEVERED_NAME_RE.search(blob):
            return "levered"
    last = row.get("last")
    try:
        if last is not None and 0 < float(last) < MICRO_LAST_MAX:
            return "micro"
    except (TypeError, ValueError):
        pass
    cap = row.get("market_cap")
    if cap is None:
        cap = row.get("marketCap")
    try:
        if cap is not None and 0 < float(cap) < MICRO_MARKET_CAP_MAX:
            return "micro"
    except (TypeError, ValueError):
        pass
    return ""


def normalize_symbols(raw: Any) -> list[str]:
    out: list[str] = []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    for item in items:
        sym = str(item or "").upper().strip()
        if not is_common_equity_symbol(sym):
            continue
        if sym not in out:
            out.append(sym)
    return out


def _stock_type_ok(stock_type: str, allowed: set[str]) -> bool:
    st = str(stock_type or "").strip().upper()
    if not st:
        return True  # unknown — keep; suffix filter already applied
    return st in allowed


def _optional_num(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        text = str(raw).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            val = float(text)
        except (TypeError, ValueError):
            return None
    if val != val:
        return None
    return val


def _first_attr(obj: Any, names: tuple[str, ...]) -> Any:
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, dict):
            val = obj.get(name)
        else:
            val = getattr(obj, name, None)
        if val not in (None, ""):
            return val
    return None


def _scan_row_facts(row: Any, symbol: str, *, rank_fallback: int) -> dict[str, Any]:
    """Keep what the scanner already told us. Never invent a last."""
    out: dict[str, Any] = {"symbol": symbol}
    try:
        rank = getattr(row, "rank", None)
        if rank is None and isinstance(row, dict):
            rank = row.get("rank")
        out["rank"] = int(rank) if rank is not None else int(rank_fallback)
    except (TypeError, ValueError):
        out["rank"] = int(rank_fallback)
    for src, dst in (
        ("distance", "distance"),
        ("benchmark", "benchmark"),
        ("projection", "projection"),
        ("legsStr", "legs"),
    ):
        val = getattr(row, src, None)
        if val in (None, "") and isinstance(row, dict):
            val = row.get(src) if src != "legsStr" else row.get("legs")
        if val not in (None, ""):
            out[dst] = str(val)
    cd = getattr(row, "contractDetails", None)
    if cd is None and isinstance(row, dict):
        cd = row.get("contractDetails")
    if cd is not None:
        st = str(getattr(cd, "stockType", "") or "").strip()
        if not st and isinstance(cd, dict):
            st = str(cd.get("stockType") or cd.get("stock_type") or "").strip()
        if st:
            out["stock_type"] = st
        long_name = str(getattr(cd, "longName", "") or "").strip()
        if not long_name and isinstance(cd, dict):
            long_name = str(cd.get("longName") or cd.get("long_name") or "").strip()
        if long_name:
            out["long_name"] = long_name
        cap = _optional_num(_first_attr(cd, ("marketCap", "market_cap")))
        if cap is not None and cap > 0:
            out["market_cap"] = cap
    last = _optional_num(_first_attr(row, ("last", "lastPrice", "last_price")))
    if last is None and isinstance(row, dict):
        last = _optional_num(row.get("last"))
    if last is not None and last > 0:
        out["last"] = last
    volume = _optional_num(_first_attr(row, ("volume", "avgVolume")))
    if volume is None and isinstance(row, dict):
        volume = _optional_num(row.get("volume"))
    if volume is not None and volume > 0:
        out["volume"] = int(volume) if volume >= 1 else volume
    if "market_cap" not in out:
        cap = _optional_num(_first_attr(row, ("marketCap", "market_cap")))
        if cap is None and isinstance(row, dict):
            cap = _optional_num(row.get("market_cap") or row.get("marketCap"))
        if cap is not None and cap > 0:
            out["market_cap"] = cap
    if "stock_type" not in out and isinstance(row, dict):
        st = str(row.get("stock_type") or row.get("stockType") or "").strip()
        if st:
            out["stock_type"] = st
    return out


async def _ibkr_scan(connector: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Run one IBKR market scanner subscription.

    Returns ``{"ok": True, "symbols": [...], "rows": [...]}`` or
    ``{"ok": False, "error": ...}``. Empty successful pull → ok with
    symbols=[] (caller must not substitute catalog names).
    """
    if connector is None or not getattr(connector, "connected", False):
        return {"ok": False, "error": "IBKR required for live screen", "symbols": [], "rows": []}
    ib = getattr(connector, "ib", None)
    if ib is None:
        return {"ok": False, "error": "IBKR required for live screen", "symbols": [], "rows": []}
    try:
        from ib_insync import ScannerSubscription, TagValue
    except Exception:
        return {"ok": False, "error": "ib_insync unavailable", "symbols": []}
    sub = ScannerSubscription(
        instrument="STK",
        locationCode=str(spec.get("locationCode") or "STK.US.MAJOR"),
        scanCode=str(spec.get("scanCode") or "MOST_ACTIVE"),
        numberOfRows=int(spec.get("rows") or 25),
    )
    existing_tags = {
        str(k)
        for k in ((spec.get("filterTags") or {}) if isinstance(spec.get("filterTags"), dict) else {})
    }
    cap_above = _usd_to_scanner_millions(spec.get("marketCapAbove"))
    cap_below = _usd_to_scanner_millions(spec.get("marketCapBelow"))
    if cap_above is not None:
        # Native field is millions. Sending raw USD (200e9) made mega/large
        # TOP_PERC_LOSE come back empty every look.
        sub.marketCapAbove = cap_above
    if cap_below is not None:
        sub.marketCapBelow = cap_below
    if spec.get("stockTypeFilter") is not None:
        sub.stockTypeFilter = str(spec["stockTypeFilter"])
    if spec.get("abovePrice") is not None:
        sub.abovePrice = float(spec["abovePrice"])
    if spec.get("belowPrice") is not None:
        sub.belowPrice = float(spec["belowPrice"])
    if spec.get("aboveVolume") is not None:
        sub.aboveVolume = int(spec["aboveVolume"])
    if spec.get("averageOptionVolumeAbove") is not None:
        sub.averageOptionVolumeAbove = int(spec["averageOptionVolumeAbove"])
    filter_opts: list[Any] = []
    raw_tags = spec.get("filterTags") or {}
    if isinstance(raw_tags, dict):
        for tag, val in raw_tags.items():
            filter_opts.append(TagValue(str(tag), str(val)))
    # IBKR's documented cap filter is millions via TagValue, not raw USD.
    if cap_above is not None and "marketCapAbove1e6" not in existing_tags:
        filter_opts.append(TagValue("marketCapAbove1e6", str(int(round(cap_above)))))
    if cap_below is not None and "marketCapBelow1e6" not in existing_tags:
        filter_opts.append(TagValue("marketCapBelow1e6", str(int(round(cap_below)))))
    allowed_types = {
        t.strip().upper()
        for t in str(spec.get("stockTypeFilter") or "CORP,ETF,ADR").split(",")
        if t.strip()
    }
    data = None
    try:
        async with connector.async_lock:
            try:
                data = await ib.reqScannerDataAsync(sub, [], filter_opts)
            finally:
                # IBKR allows one scanner sub at a time; always release it.
                try:
                    ib.cancelScannerSubscription(sub)
                except Exception:
                    pass
                # Brief settle so the next screen is not cancelled by TWS.
                await asyncio.sleep(0.35)
    except Exception as exc:
        msg = str(exc).lower()
        if "cancel" in msg or "subscription" in msg:
            logger.warning(
                "IBKR scanner ended early scanCode=%s: %s",
                spec.get("scanCode"),
                exc,
            )
            return {"ok": True, "symbols": [], "rows": [], "ibkr_rows": 0, "kept": 0}
        logger.exception("IBKR scanner failed scanCode=%s", spec.get("scanCode"))
        return {"ok": False, "error": str(exc), "symbols": []}
    try:
        raw_n = len(data or [])
        syms: list[str] = []
        rows: list[dict[str, Any]] = []
        scan_code = str(spec.get("scanCode") or "").strip().upper()
        for row in data or []:
            cd = getattr(row, "contractDetails", None)
            contract = None
            if cd is not None:
                contract = getattr(cd, "contract", None)
            if contract is None:
                contract = getattr(row, "contract", None)
            if contract is None:
                continue
            sec = str(getattr(contract, "secType", "STK") or "STK").upper()
            if sec != "STK":
                continue
            sym = str(getattr(contract, "symbol", "") or "").upper()
            if not is_common_equity_symbol(sym) or sym in syms:
                continue
            st = str(getattr(cd, "stockType", "") or "") if cd is not None else ""
            if not _stock_type_ok(st, allowed_types):
                continue
            syms.append(sym)
            facts = _scan_row_facts(row, sym, rank_fallback=len(rows))
            if scan_code:
                facts["scan_code"] = scan_code
            rows.append(facts)
        return {
            "ok": True,
            "symbols": syms,
            "rows": rows,
            "ibkr_rows": raw_n,
            "kept": len(syms),
        }
    except Exception as exc:
        logger.exception("IBKR scanner parse failed scanCode=%s", spec.get("scanCode"))
        return {"ok": False, "error": str(exc), "symbols": [], "rows": []}
