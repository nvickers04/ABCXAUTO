"""Universe sandbox — operator arenas + IBKR (preferred) legal symbol set.

Shell builds the legal box; SCAN TAPE stays unranked; Grok picks inside.
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
    "most_active": {
        "label": "Most active (IBKR)",
        "group": "scans",
        "ibkr": {
            "scanCode": "MOST_ACTIVE",
            "locationCode": "STK.US.MAJOR",
            "stockTypeFilter": "CORP,ETF",
            "abovePrice": 5.0,
            "aboveVolume": 1_000_000,
            "rows": 40,
        },
        "mda_fallback": [],
    },
    "top_gainers": {
        "label": "Top % gainers (IBKR)",
        "group": "scans",
        "ibkr": {
            "scanCode": "TOP_PERC_GAIN",
            "locationCode": "STK.US.MAJOR",
            "stockTypeFilter": "CORP,ETF",
            "abovePrice": 5.0,
            "aboveVolume": 500_000,
            "rows": 30,
        },
        "mda_fallback": [],
    },
    "top_losers": {
        "label": "Top % losers (IBKR)",
        "group": "scans",
        "ibkr": {
            "scanCode": "TOP_PERC_LOSE",
            "locationCode": "STK.US.MAJOR",
            "stockTypeFilter": "CORP,ETF",
            "abovePrice": 5.0,
            "aboveVolume": 500_000,
            "rows": 30,
        },
        "mda_fallback": [],
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


def arena_pull_kind(arena_id: str) -> str:
    """How this arena fills membership: ibkr | mda_seed."""
    meta = ARENA_CATALOG.get(str(arena_id)) or {}
    return "ibkr" if meta.get("ibkr") else "mda_seed"


def arena_checkbox_label(arena_id: str) -> str:
    meta = ARENA_CATALOG.get(str(arena_id)) or {}
    base = str(meta.get("label") or arena_id)
    kind = arena_pull_kind(arena_id)
    if kind == "mda_seed":
        return f"{base}  ·  MDA seed"
    return f"{base}  ·  IBKR"


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


async def _ibkr_scan(connector: Any, spec: dict[str, Any]) -> list[str]:
    """Run one IBKR market scanner subscription; return symbols (scan order)."""
    if connector is None or not getattr(connector, "connected", False):
        return []
    ib = getattr(connector, "ib", None)
    if ib is None:
        return []
    try:
        from ib_insync import ScannerSubscription
    except Exception:
        return []
    sub = ScannerSubscription(
        instrument="STK",
        locationCode=str(spec.get("locationCode") or "STK.US.MAJOR"),
        scanCode=str(spec.get("scanCode") or "MOST_ACTIVE"),
        numberOfRows=int(spec.get("rows") or 25),
    )
    if spec.get("marketCapAbove") is not None:
        sub.marketCapAbove = float(spec["marketCapAbove"])
    if spec.get("marketCapBelow") is not None:
        sub.marketCapBelow = float(spec["marketCapBelow"])
    if spec.get("stockTypeFilter") is not None:
        sub.stockTypeFilter = str(spec["stockTypeFilter"])
    if spec.get("abovePrice") is not None:
        sub.abovePrice = float(spec["abovePrice"])
    if spec.get("aboveVolume") is not None:
        sub.aboveVolume = int(spec["aboveVolume"])
    if spec.get("averageOptionVolumeAbove") is not None:
        sub.averageOptionVolumeAbove = int(spec["averageOptionVolumeAbove"])
    allowed_types = {
        t.strip().upper()
        for t in str(spec.get("stockTypeFilter") or "CORP,ETF,ADR").split(",")
        if t.strip()
    }
    data = None
    try:
        async with connector.async_lock:
            try:
                data = await ib.reqScannerDataAsync(sub)
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
        else:
            logger.exception("IBKR scanner failed scanCode=%s", spec.get("scanCode"))
        return []
    try:
        syms: list[str] = []
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
        return syms
    except Exception:
        logger.exception("IBKR scanner parse failed scanCode=%s", spec.get("scanCode"))
        return []


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
            pulled = await _ibkr_scan(connector, ibkr_spec)
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
    legal = set(legal_symbols())
    out: list[str] = []
    for s in normalize_symbols(symbols or []):
        if s in legal and s not in out:
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


def universe_status_summary() -> str:
    al = load_allowlist()
    legal = legal_symbols()
    src = al.get("source") or _CACHE.get("source") or "n/a"
    ts = al.get("refreshed_at") or "never"
    arenas = al.get("enabled_arenas") or []
    ibkr_n = sum(1 for a in arenas if arena_pull_kind(a) == "ibkr")
    mda_n = len(arenas) - ibkr_n
    return (
        f"{len(legal)} legal  ·  {len(arenas)} arenas "
        f"({ibkr_n} IBKR / {mda_n} MDA seed)  ·  "
        f"refreshed {ts}  ·  {src}"
    )


def universe_fact_block() -> str:
    al = load_allowlist()
    legal = legal_symbols()
    arenas = ", ".join(al.get("enabled_arenas") or []) or "(none)"
    src = al.get("source") or _CACHE.get("source") or "n/a"
    ts = al.get("refreshed_at") or "never"
    return (
        "UNIVERSE SANDBOX (operator arenas — Fact; shell does not rank):\n"
        f"- legal_n={len(legal)} source={src} refreshed_at={ts}\n"
        f"- arenas={arenas}\n"
        f"- custom={len(al.get('custom_symbols') or [])} "
        f"exclude={len(al.get('exclude_symbols') or [])}\n"
        "Hunt / scan_request must name symbols in the legal set "
        "(held book symbols stay visible for manage/protect)."
    )


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
