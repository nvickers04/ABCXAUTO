"""Universe watchlist — Grok sets arenas/custom via self_tune.

Scan may seed from this list. send is not limited to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO_ROOT / "universe_allowlist.json"
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")
# Units / warrants / rights often pollute scanners (AACOU, DMAAR…).
# 5+ char suffix only — keep SHORT names (LOW, AIR, MU) and 4-char CORP/ETF.
_JUNK_SUFFIX_RE = re.compile(r"^[A-Z]{4,}[UWRX]$")
_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "legal": [],
    "source": "",
    "arenas": [],
    "membership": [],
}
_CACHE_TTL_S = 300.0

def _stk_scan_arena(
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
        "mda_fallback": [],
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

# Arena catalog: IBKR scanner params and/or seed symbols for qualify / MDA fallback.
# Membership is refreshed dynamically — not a frozen mega prison.
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
        "mda_fallback": [
            "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "BRK.B", "AVGO", "JPM",
        ],
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
        "mda_fallback": [
            "AMD", "CRM", "COST", "NFLX", "ORCL", "ADBE", "PEP", "KO", "XOM", "CVX",
            "WMT", "V", "MA", "BAC", "UNH",
        ],
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
        "mda_fallback": [
            "DECK", "FIX", "CASY", "WSM", "TOL", "RCL", "DKNG", "ROKU", "AFRM", "SOFI",
        ],
    },
    **{
        arena_id: _stk_scan_arena(label, code, **kw)
        for arena_id, label, code, kw in _SCAN_ARENA_SPECS
    },
    "index_etfs": {
        "label": "Index ETFs",
        "group": "etfs",
        "ibkr": None,
        "mda_fallback": ["SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "XLK", "XLF", "XLE"],
    },
    "commodities": {
        "label": "Commodities / macro ETFs",
        "group": "commodities",
        "ibkr": None,
        "mda_fallback": ["GLD", "SLV", "USO", "UNG", "TLT", "HYG", "UUP", "DBC"],
    },
    # Industry arenas: no honest IBKR industry code in our scanner path yet —
    # MDA seed lists only (avoid re-pulling generic HOT_BY_VOLUME junk).
    "technology": {
        "label": "Technology",
        "group": "industries",
        "ibkr": None,
        "mda_fallback": [
            "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "INTC",
            "QCOM", "TXN", "AMAT", "NOW", "PANW",
        ],
    },
    "healthcare": {
        "label": "Healthcare",
        "group": "industries",
        "ibkr": None,
        "mda_fallback": [
            "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "PFE", "AMGN", "ISRG",
        ],
    },
    "energy": {
        "label": "Energy",
        "group": "industries",
        "ibkr": None,
        "mda_fallback": [
            "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "WMB",
        ],
    },
    "financials": {
        "label": "Financials",
        "group": "industries",
        "ibkr": None,
        "mda_fallback": [
            "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "SPGI",
        ],
    },
}

_DEFAULT_ENABLED = ("most_active", "index_etfs", "mega_cap")


def _build_known_scan_codes() -> dict[str, dict[str, Any]]:
    """Standing IBKR scanCodes from group=scans arenas (documented TWS ids only)."""
    out: dict[str, dict[str, Any]] = {}
    for meta in ARENA_CATALOG.values():
        if meta.get("group") != "scans":
            continue
        ibkr = meta.get("ibkr") or {}
        code = str(ibkr.get("scanCode") or "").strip().upper()
        if not code:
            continue
        # First catalog row for a code wins (friendly defaults).
        out.setdefault(code, dict(ibkr))
    return out


def _build_scan_code_to_arena() -> dict[str, str]:
    out: dict[str, str] = {}
    for arena_id, meta in ARENA_CATALOG.items():
        if meta.get("group") != "scans":
            continue
        ibkr = meta.get("ibkr") or {}
        code = str(ibkr.get("scanCode") or "").strip().upper()
        if code and code not in out:
            out[code] = arena_id
    return out


# Bare IBKR scanCodes accepted by scan(arena=…) / scan(scan_code=…).
# Sourced from ARENA_CATALOG group=scans — same class as MOST_ACTIVE / TOP_PERC_*.
KNOWN_SCAN_CODES: dict[str, dict[str, Any]] = _build_known_scan_codes()

# Arena catalog id aliases for the same standing screens.
_SCAN_CODE_TO_ARENA: dict[str, str] = _build_scan_code_to_arena()


def known_scan_codes() -> list[str]:
    """Documented IBKR scanCodes the clerk will run (tool JSON scan_code=)."""
    return list(KNOWN_SCAN_CODES.keys())


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

# Bounded TagValue allowlist (clerk, not SYSTEM). Exact IBKR tag names.
_SCAN_TAG_FILTERS: frozenset[str] = frozenset(
    {"usdMarketCapAbove", "optVolumeAbove", "avgVolumeAbove"}
)

# P/E TagValues — accepted only after reqScannerParameters XML verifies the code.
_PE_TAG_CANDIDATES: frozenset[str] = frozenset({"peRatioAbove", "peRatioBelow"})

# Non-filter keys allowed on scan() after tool_args normalize.
_SCAN_BASE_KEYS: frozenset[str] = frozenset(
    {"arena", "scan_code", "symbols", "symbol", "with", "include"}
)

_PE_TAG_CACHE: dict[str, Any] = {"ts": 0.0, "tags": frozenset()}
_PE_TAG_CACHE_TTL_S = 3600.0


def known_screen_keys() -> list[str]:
    """Tool JSON arenas= keys: catalog ids + standing IBKR scanCodes."""
    out = list(ARENA_CATALOG.keys())
    for code in KNOWN_SCAN_CODES:
        if code not in out:
            out.append(code)
    return out


def _xml_has_scanner_code(xml: str, code: str) -> bool:
    """True when reqScannerParameters XML lists this filter code (not a guess)."""
    if not xml or not code:
        return False
    # IBKR XML uses <code>tagName</code> on AbstractField / RangeFilter rows.
    needle = f"<code>{code}</code>"
    if needle in xml:
        return True
    # Some dumps quote the code attribute.
    return f'code="{code}"' in xml or f"code='{code}'" in xml


def _pe_tags_from_xml(xml: str) -> frozenset[str]:
    return frozenset(t for t in _PE_TAG_CANDIDATES if _xml_has_scanner_code(xml, t))


def reset_pe_tag_cache() -> None:
    """Tests."""
    _PE_TAG_CACHE.update(ts=0.0, tags=frozenset())


async def verified_pe_tags(connector: Any = None) -> frozenset[str]:
    """P/E TagValues present in live reqScannerParameters XML. Empty if unverified."""
    now = time.monotonic()
    cached = _PE_TAG_CACHE.get("tags") or frozenset()
    if cached and (now - float(_PE_TAG_CACHE.get("ts") or 0)) < _PE_TAG_CACHE_TTL_S:
        return frozenset(cached)
    if connector is None or not getattr(connector, "connected", False):
        return frozenset()
    ib = getattr(connector, "ib", None)
    if ib is None or not hasattr(ib, "reqScannerParametersAsync"):
        return frozenset()
    try:
        lock = getattr(connector, "async_lock", None)
        if lock is not None:
            async with lock:
                xml = await ib.reqScannerParametersAsync()
        else:
            xml = await ib.reqScannerParametersAsync()
    except Exception:
        logger.exception("reqScannerParameters failed")
        return frozenset()
    found = _pe_tags_from_xml(str(xml or ""))
    _PE_TAG_CACHE.update(ts=now, tags=found)
    return found


def parse_scan_filters(
    args: dict[str, Any] | None,
    *,
    pe_tags: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Clerk allowlist for optional IBKR filters this look. Unknown keys → error.

    P/E tags are accepted only when ``pe_tags`` contains the verified XML code.
    No persist. Does not invent TagValue names.
    """
    src = dict(args) if isinstance(args, dict) else {}
    allowed_pe = frozenset(pe_tags or ())
    allowed_tags = set(_SCAN_TAG_FILTERS) | set(allowed_pe)
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
    """Echo the arena's cap so an empty mega/large screen is not a silent miss."""
    out = dict(applied or {})
    if not spec:
        return out
    if spec.get("marketCapAbove") is not None and "market_cap_above" not in out:
        out["market_cap_above"] = spec["marketCapAbove"]
    if spec.get("marketCapBelow") is not None and "market_cap_below" not in out:
        out["market_cap_below"] = spec["marketCapBelow"]
    return out


def _resolve_one_selector(key: str) -> dict[str, Any]:
    """One catalog id or standing IBKR scanCode."""
    if not key:
        return {"ok": False, "error": "arena or scan_code required"}
    lower = key.lower()
    if lower in ARENA_CATALOG:
        meta = ARENA_CATALOG[lower]
        return {
            "ok": True,
            "arena_id": lower,
            "scan_code": str((meta.get("ibkr") or {}).get("scanCode") or "") or None,
            "ibkr": dict(meta["ibkr"]) if meta.get("ibkr") else None,
            "mda_fallback": list(meta.get("mda_fallback") or []),
        }
    code = key.upper()
    if code in KNOWN_SCAN_CODES:
        arena_id = _SCAN_CODE_TO_ARENA.get(code)
        return {
            "ok": True,
            "arena_id": arena_id,
            "scan_code": code,
            "ibkr": dict(KNOWN_SCAN_CODES[code]),
            "mda_fallback": list(
                (ARENA_CATALOG.get(arena_id) or {}).get("mda_fallback") or []
            )
            if arena_id
            else [],
        }
    return {
        "ok": False,
        "error": f"unknown arena/scan_code: {key}",
        "arenas": known_screen_keys(),
    }


def resolve_screen(
    arena: str | None = None,
    scan_code: str | None = None,
) -> dict[str, Any]:
    """Resolve one screen. arena is the universe; scan_code is the sort.

    Both together is a compose, not an error: mega_cap + TOP_PERC_LOSE keeps the
    cap filter and ranks losers. Dropping the sort was why that call came back
    as HOT_BY_VOLUME and the model spent the look retrying.
    """
    raw_arena = str(arena or "").strip()
    raw_code = str(scan_code or "").strip().upper()
    if raw_arena and raw_code:
        if raw_code not in KNOWN_SCAN_CODES:
            return {
                "ok": False,
                "error": f"unknown arena/scan_code: {raw_code}",
                "arenas": known_screen_keys(),
            }
        base = _resolve_one_selector(raw_arena)
        if not base.get("ok"):
            return base
        ibkr = dict(base.get("ibkr") or {})
        if not ibkr:
            return {
                "ok": False,
                "error": "scan filters require an IBKR arena|scan_code",
            }
        ibkr["scanCode"] = raw_code
        return {
            "ok": True,
            "arena_id": base.get("arena_id"),
            "scan_code": raw_code,
            "ibkr": ibkr,
            "mda_fallback": list(base.get("mda_fallback") or []),
        }
    return _resolve_one_selector(raw_arena or raw_code)


async def pull_one_screen(
    connector: Any = None,
    *,
    arena: str | None = None,
    scan_code: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One IBKR screen (or MDA industry seed if no IBKR) this look. No persist."""
    resolved = resolve_screen(arena=arena, scan_code=scan_code)
    if not resolved.get("ok"):
        return resolved
    ibkr_spec, applied = merge_scan_filters_into_spec(resolved.get("ibkr"), filters)
    try:
        from abcxauto.lab_playbook import apply_card_constraints_to_spec

        ibkr_spec, extra = apply_card_constraints_to_spec(ibkr_spec)
        applied.update(extra)
    except Exception:
        logger.debug("card scan constraints apply failed", exc_info=True)
    applied = _with_spec_cap_applied(ibkr_spec, applied)
    if (filters or {}).get("applied") and ibkr_spec is None:
        return {
            "ok": False,
            "error": "scan filters require an IBKR arena|scan_code",
            "applied": dict((filters or {}).get("applied") or {}),
        }
    pulled: list[str] = []
    rows: list[dict[str, Any]] = []
    source = ""
    # MDA industry seed only when there is no IBKR connector (or no IBKR spec).
    # If _ibkr_scan ran and returned empty — stay empty; do not dump catalog names.
    if ibkr_spec and connector is not None:
        scan_out = await _ibkr_scan(connector, ibkr_spec)
        if isinstance(scan_out, dict) and not scan_out.get("ok", True):
            return {
                "ok": False,
                "error": scan_out.get("error") or "IBKR scanner error",
                "arena_id": resolved.get("arena_id"),
                "scan_code": resolved.get("scan_code"),
                "applied": applied,
                "persisted": False,
            }
        if isinstance(scan_out, dict):
            pulled = list(scan_out.get("symbols") or [])
            rows = list(scan_out.get("rows") or [])
        else:
            pulled = list(scan_out or [])
        source = "ibkr" if pulled else "empty"
    else:
        pulled = normalize_symbols(resolved.get("mda_fallback") or [])
        if pulled:
            source = "mda_seed"
    return {
        "ok": True,
        "arena_id": resolved.get("arena_id"),
        "scan_code": resolved.get("scan_code"),
        "source": source or "empty",
        "symbols": list(pulled),
        "rows": rows,
        "applied": applied,
        "persisted": False,
    }


def _path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_UNIVERSE_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def default_allowlist() -> dict[str, Any]:
    return {
        "enabled_arenas": list(_DEFAULT_ENABLED),
        "custom_symbols": [],
        "exclude_symbols": [],
        "legal_symbols": [],
        "membership": [],  # [{symbol, arena, source}] scan/arena order
        "source": "",
        "refreshed_at": "",
    }


def _normalize_membership(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not is_common_equity_symbol(sym) or sym in seen:
            continue
        seen.add(sym)
        out.append(
            {
                "symbol": sym,
                "arena": str(row.get("arena") or "").strip() or "?",
                "source": str(row.get("source") or "").strip() or "?",
            }
        )
    return out


def load_allowlist(path: Path | None = None) -> dict[str, Any]:
    p = path or _path()
    base = default_allowlist()
    if not p.is_file():
        return base
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return base
        out = dict(base)
        arenas = raw.get("enabled_arenas")
        if isinstance(arenas, list):
            out["enabled_arenas"] = [
                str(a) for a in arenas if str(a) in ARENA_CATALOG
            ]
        for key in ("custom_symbols", "exclude_symbols", "legal_symbols"):
            vals = raw.get(key)
            if isinstance(vals, list):
                out[key] = normalize_symbols(vals)
        out["membership"] = _normalize_membership(raw.get("membership"))
        # Rebuild membership stubs from legal list if missing (legacy files).
        if not out["membership"] and out["legal_symbols"]:
            out["membership"] = [
                {"symbol": s, "arena": "?", "source": "persisted"}
                for s in out["legal_symbols"]
            ]
        out["source"] = str(raw.get("source") or "")
        out["refreshed_at"] = str(raw.get("refreshed_at") or "")
        if not out["enabled_arenas"] and not out["custom_symbols"]:
            out["enabled_arenas"] = list(_DEFAULT_ENABLED)
        return out
    except Exception:
        logger.exception("load universe allowlist failed")
        return base


def save_allowlist(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or _path()
    cur = load_allowlist(p)
    if "enabled_arenas" in data and isinstance(data["enabled_arenas"], list):
        cur["enabled_arenas"] = [
            str(a) for a in data["enabled_arenas"] if str(a) in ARENA_CATALOG
        ]
    for key in ("custom_symbols", "exclude_symbols", "legal_symbols"):
        if key in data and isinstance(data[key], list):
            cur[key] = normalize_symbols(data[key])
    if "membership" in data:
        cur["membership"] = _normalize_membership(data.get("membership"))
    if "source" in data:
        cur["source"] = str(data.get("source") or "")
    if "refreshed_at" in data:
        cur["refreshed_at"] = str(data.get("refreshed_at") or "")
    if not cur["enabled_arenas"] and not cur["custom_symbols"]:
        cur["enabled_arenas"] = list(_DEFAULT_ENABLED)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


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


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stock_type_ok(stock_type: str, allowed: set[str]) -> bool:
    st = str(stock_type or "").strip().upper()
    if not st:
        return True  # unknown — keep; suffix filter already applied
    return st in allowed


def _scan_row_facts(row: Any, symbol: str, *, rank_fallback: int) -> dict[str, Any]:
    """Keep what the scanner already told us. ``distance``/``benchmark`` are the
    scanCode's own metric (e.g. % gain for TOP_PERC_GAIN) — free triage data.
    """
    out: dict[str, Any] = {"symbol": symbol}
    try:
        rank = getattr(row, "rank", None)
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
        if val not in (None, ""):
            out[dst] = str(val)
    return out


async def _ibkr_scan(connector: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Run one IBKR market scanner subscription.

    Returns ``{"ok": True, "symbols": [...]}`` or ``{"ok": False, "error": ...}``.
    Empty successful pull → ok with symbols=[] (caller must not MDA-fallback).
    """
    if connector is None or not getattr(connector, "connected", False):
        return {"ok": True, "symbols": []}
    ib = getattr(connector, "ib", None)
    if ib is None:
        return {"ok": True, "symbols": []}
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
                # Brief settle so the next arena scan is not cancelled by TWS.
                await asyncio.sleep(0.35)
    except Exception as exc:
        msg = str(exc).lower()
        if "cancel" in msg or "subscription" in msg:
            logger.warning(
                "IBKR scanner ended early scanCode=%s: %s",
                spec.get("scanCode"),
                exc,
            )
            return {"ok": True, "symbols": []}
        logger.exception("IBKR scanner failed scanCode=%s", spec.get("scanCode"))
        return {"ok": False, "error": str(exc), "symbols": []}
    try:
        syms: list[str] = []
        rows: list[dict[str, Any]] = []
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
            rows.append(_scan_row_facts(row, sym, rank_fallback=len(rows)))
        return {"ok": True, "symbols": syms, "rows": rows}
    except Exception as exc:
        logger.exception("IBKR scanner parse failed scanCode=%s", spec.get("scanCode"))
        return {"ok": False, "error": str(exc), "symbols": [], "rows": []}


async def refresh_legal_set(
    connector: Any = None,
    *,
    allowlist: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Pull IBKR (preferred) / MDA-fallback symbols for enabled arenas."""
    al = dict(allowlist if allowlist is not None else load_allowlist())
    enabled = list(al.get("enabled_arenas") or _DEFAULT_ENABLED)
    custom = normalize_symbols(al.get("custom_symbols") or [])
    exclude = set(normalize_symbols(al.get("exclude_symbols") or []))

    legal: list[str] = []
    membership: list[dict[str, str]] = []
    sources: list[str] = []

    for arena_id in enabled:
        meta = ARENA_CATALOG.get(arena_id) or {}
        pulled: list[str] = []
        pull_src = ""
        ibkr_spec = meta.get("ibkr")
        if ibkr_spec and connector is not None:
            scan_out = await _ibkr_scan(connector, ibkr_spec)
            if isinstance(scan_out, dict):
                pulled = list(scan_out.get("symbols") or []) if scan_out.get("ok", True) else []
            else:
                pulled = list(scan_out or [])
            if pulled:
                pull_src = "ibkr"
                sources.append(f"{arena_id}:ibkr")
        if not pulled:
            pulled = normalize_symbols(meta.get("mda_fallback") or [])
            if pulled:
                pull_src = "mda_fallback"
                sources.append(f"{arena_id}:mda_fallback")
        for sym in pulled:
            if sym not in legal and sym not in exclude:
                legal.append(sym)
                membership.append(
                    {"symbol": sym, "arena": str(arena_id), "source": pull_src or "?"}
                )

    for sym in custom:
        if sym not in legal and sym not in exclude:
            legal.append(sym)
            membership.append({"symbol": sym, "arena": "custom", "source": "custom"})

    # Fail-closed minimal default if empty
    if not legal:
        legal = ["SPY", "QQQ", "IWM"]
        membership = [
            {"symbol": s, "arena": "default", "source": "default"} for s in legal
        ]
        sources.append("default:index")

    # Keep arena / scanner order — do not alphabetize (that biases SCAN TAPE to A*).
    source = "+".join(sources) if sources else "empty"
    al["legal_symbols"] = legal
    al["membership"] = membership
    al["source"] = source
    al["refreshed_at"] = _utc_now()
    if persist:
        save_allowlist(al)
    _CACHE.update(
        ts=time.monotonic(),
        legal=list(legal),
        source=source,
        arenas=enabled,
        membership=list(membership),
    )
    return al


def legal_symbols(*, use_cache: bool = True) -> list[str]:
    """Current legal set (from cache or last persisted refresh)."""
    now = time.monotonic()
    if (
        use_cache
        and _CACHE.get("legal")
        and (now - float(_CACHE.get("ts") or 0)) < _CACHE_TTL_S
    ):
        return list(_CACHE["legal"])
    al = load_allowlist()
    legal = normalize_symbols(al.get("legal_symbols") or [])
    if not legal:
        # Build from fallbacks without IBKR (offline)
        for arena_id in al.get("enabled_arenas") or _DEFAULT_ENABLED:
            meta = ARENA_CATALOG.get(str(arena_id)) or {}
            for sym in normalize_symbols(meta.get("mda_fallback") or []):
                if sym not in legal:
                    legal.append(sym)
        for sym in normalize_symbols(al.get("custom_symbols") or []):
            if sym not in legal:
                legal.append(sym)
        exclude = set(normalize_symbols(al.get("exclude_symbols") or []))
        legal = [s for s in legal if s not in exclude]
        if not legal:
            legal = ["SPY", "QQQ", "IWM"]
    _CACHE.update(
        ts=now,
        legal=list(legal),
        source=str(al.get("source") or "persisted"),
        arenas=list(al.get("enabled_arenas") or []),
    )
    return list(legal)


def is_legal_symbol(symbol: str) -> bool:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    return sym in set(legal_symbols())


def filter_to_legal(symbols: list[str] | None) -> list[str]:
    """Normalize asked tickers. Not a sandbox — Grok may quote any name."""
    out: list[str] = []
    for s in normalize_symbols(symbols or []):
        if s not in out:
            out.append(s)
    return out


def membership_rows(*, query: str = "") -> list[dict[str, str]]:
    """Legal-set rows in arena/scan order (never ranked). Optional substring filter."""
    al = load_allowlist()
    rows = _normalize_membership(al.get("membership"))
    if not rows:
        rows = [
            {"symbol": s, "arena": "?", "source": "persisted"}
            for s in normalize_symbols(al.get("legal_symbols") or [])
        ]
    q = str(query or "").upper().strip()
    if not q:
        return rows
    return [
        r
        for r in rows
        if q in r["symbol"] or q in r["arena"].upper() or q in r["source"].upper()
    ]


def universe_glance_line() -> str:
    """One-line Fact for Dashboard Agent World."""
    al = load_allowlist()
    n = len(legal_symbols())
    arenas = ", ".join(al.get("enabled_arenas") or []) or "(none)"
    ts = al.get("refreshed_at") or "never"
    src = al.get("source") or "n/a"
    short_src = src if len(src) <= 72 else src[:69] + "…"
    return f"Universe: {n} legal · arenas={arenas} · {short_src} · {ts}"


def reset_universe_cache() -> None:
    _CACHE.update(ts=0.0, legal=[], source="", arenas=[], membership=[])
