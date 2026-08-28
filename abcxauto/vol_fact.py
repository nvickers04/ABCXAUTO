"""Layer-1 vol fact. Grok can read and ignore. Not a size habit, not a card.

Once per look, on names already taped (open lots + symbols fetched this look).
Yesterday vs ~20-day realized; IV / IV rank from an already-fetched chain;
IV minus recent realized. No GARCH. Session-to-session persist is the null.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

RV_WINDOW = 20
# Enough closes to compare yesterday to a trailing window.
_MIN_CLOSES = 6
_HIGH_RATIO = 1.25
_LOW_RATIO = 0.75
_FLAT_IV_RV = 0.5
BOOK_VOL_NAMES = 6
WAKE_VOL_NAMES = 2
WAKE_VOL_CHARS = 96
_BAR_KEEP = 40

_BANNED_PROMPT = ("garch", "engle", "70% accuracy")


def _sym(raw: Any) -> str:
    return str(raw or "").upper().strip()


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def iv_to_pct(raw: Any) -> float | None:
    """IBKR/MDA IV as percent. Decimal 0.22 → 22.0; already-percent stays."""
    v = _finite(raw)
    if v is None or v <= 0:
        return None
    if v <= 4.0:
        return round(v * 100.0, 1)
    return round(v, 1)


def _bar_date(bar: dict[str, Any]) -> str:
    t = bar.get("t") or bar.get("t_iso") or bar.get("date") or ""
    text = str(t).strip()
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10] if text else ""


def daily_closes(bars: list[Any] | None) -> list[float]:
    """Last close per session date. Intraday bars collapse; daily bars pass through."""
    by_day: dict[str, float] = {}
    order: list[str] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = _finite(bar.get("c") if bar.get("c") is not None else bar.get("close"))
        if close is None or close <= 0:
            continue
        day = _bar_date(bar) or f"i{len(order)}"
        if day not in by_day:
            order.append(day)
        by_day[day] = close
    return [by_day[d] for d in order]


def compact_bars(bars: list[Any] | None) -> list[dict[str, Any]]:
    """t + c only, tail clipped so the look cannot bloat."""
    out: list[dict[str, Any]] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = _finite(bar.get("c") if bar.get("c") is not None else bar.get("close"))
        if close is None or close <= 0:
            continue
        row: dict[str, Any] = {"c": close}
        day = _bar_date(bar)
        if day:
            row["t"] = day
        out.append(row)
    return out[-_BAR_KEEP:]


def _log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def realized_state(bars: list[Any] | None) -> dict[str, Any] | None:
    """Yesterday |ret| vs ~20-day daily σ → high / mid / low. Annualized RV for IV-RV."""
    rets = _log_returns(daily_closes(bars))
    if len(rets) < max(4, _MIN_CLOSES - 1):
        return None
    yesterday = abs(rets[-1])
    trail = rets[-RV_WINDOW:]
    if len(trail) < 4:
        return None
    sigma = statistics.pstdev(trail) if len(trail) > 1 else abs(trail[0])
    mean_abs = sum(abs(x) for x in trail) / float(len(trail))
    # Flat-variance tapes have σ≈0; yesterday vs typical |ret| still buckets.
    scale = max(sigma, mean_abs)
    if scale <= 1e-12:
        bucket = "low"
    else:
        ratio = yesterday / scale
        if ratio >= _HIGH_RATIO:
            bucket = "high"
        elif ratio <= _LOW_RATIO:
            bucket = "low"
        else:
            bucket = "mid"
    rv20 = round(max(sigma, mean_abs) * math.sqrt(252.0) * 100.0, 1)
    return {"rv": bucket, "rv20": rv20}


def iv_minus_rv(iv_pct: float | None, rv20: float | None) -> str | None:
    """Sign and rough size in vol points. Not a cheap/rich lecture."""
    if iv_pct is None or rv20 is None:
        return None
    delta = float(iv_pct) - float(rv20)
    if abs(delta) < _FLAT_IV_RV:
        return "~0"
    mag = int(round(abs(delta)))
    return f"+{mag}" if delta > 0 else f"-{mag}"


def _walk_ivs(node: Any, into: list[float], *, numbers_are_iv: bool = False) -> None:
    if isinstance(node, dict):
        for key in ("iv", "implied_vol", "impliedVolatility", "atm_iv"):
            pct = iv_to_pct(node.get(key))
            if pct is not None:
                into.append(pct)
        _walk_ivs(node.get("ivs"), into, numbers_are_iv=True)
        _walk_ivs(node.get("implied_vols"), into, numbers_are_iv=True)
        for key in ("strikes", "expirations", "calls", "puts"):
            _walk_ivs(node.get(key), into, numbers_are_iv=False)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            if isinstance(item, (int, float)):
                if not numbers_are_iv:
                    continue
                pct = iv_to_pct(item)
                if pct is not None:
                    into.append(pct)
            else:
                _walk_ivs(item, into, numbers_are_iv=numbers_are_iv)


def ivs_from_chain(chain: Any) -> list[float]:
    found: list[float] = []
    _walk_ivs(chain, found)
    return found


def ivs_from_option_facts(facts: list[Any] | None, symbol: str) -> list[float]:
    want = _sym(symbol)
    found: list[float] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        if _sym(fact.get("symbol")) != want:
            continue
        _walk_ivs(fact, found)
        mda = fact.get("mda") if isinstance(fact.get("mda"), dict) else {}
        _walk_ivs(mda, found)
        ibkr = fact.get("ibkr") if isinstance(fact.get("ibkr"), dict) else {}
        _walk_ivs(ibkr, found)
    return found


def iv_rank(pivot: float | None, ivs: list[float]) -> int | None:
    """Where this IV sits in the IVs already on this look's chain. Not a 52w series."""
    if pivot is None:
        return None
    vals = [v for v in ivs if v is not None]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return None
    return int(round(100.0 * (pivot - lo) / (hi - lo)))


def open_lot_symbols(positions: list[Any] | None) -> list[str]:
    out: list[str] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        try:
            qty = float(
                pos.get("quantity")
                if pos.get("quantity") is not None
                else pos.get("position")
                or 0
            )
        except (TypeError, ValueError):
            continue
        if abs(qty) < 1e-9:
            continue
        su = _sym(pos.get("symbol"))
        if su and su not in out:
            out.append(su)
    return out


def taped_symbols(world: Any = None, snap: dict[str, Any] | None = None) -> list[str]:
    """Open lots plus names already fetched this look. Not a canned universe."""
    blob = snap if isinstance(snap, dict) else {}
    order: list[str] = []

    def _add(raw: Any) -> None:
        su = _sym(raw)
        if su and su not in order:
            order.append(su)

    for su in open_lot_symbols(getattr(world, "positions", None) if world is not None else None):
        _add(su)
    for su in open_lot_symbols(blob.get("positions")):
        _add(su)
    for su in getattr(world, "scan_fetched", None) or []:
        _add(su)
    for su in blob.get("scan_fetched") or []:
        _add(su)
    hits = blob.get("scan_hits") if isinstance(blob.get("scan_hits"), dict) else {}
    for row in hits.get("rows") or []:
        if isinstance(row, dict):
            _add(row.get("symbol"))
    for key in ("candle_bars", "option_chains", "quote_ivs"):
        store = blob.get(key)
        if isinstance(store, dict):
            for su in store:
                _add(su)
    return order


def _chain_store(snap: dict[str, Any] | None) -> dict[str, Any]:
    blob = snap if isinstance(snap, dict) else {}
    store = blob.get("option_chains")
    return store if isinstance(store, dict) else {}


def _bar_store(snap: dict[str, Any] | None) -> dict[str, Any]:
    blob = snap if isinstance(snap, dict) else {}
    store = blob.get("candle_bars")
    return store if isinstance(store, dict) else {}


def _quote_ivs(snap: dict[str, Any] | None) -> dict[str, Any]:
    blob = snap if isinstance(snap, dict) else {}
    store = blob.get("quote_ivs")
    return store if isinstance(store, dict) else {}


def _chain_ok(chain: Any) -> bool:
    if not isinstance(chain, dict) or chain.get("error"):
        return False
    return bool(chain.get("expirations") or chain.get("strikes") or chain.get("n_strikes"))


def _bars_of(entry: Any) -> list[Any]:
    if isinstance(entry, list):
        return entry
    if isinstance(entry, dict):
        if isinstance(entry.get("bars"), list):
            return list(entry.get("bars") or [])
        if isinstance(entry.get("closes"), list):
            return [{"c": c} if not isinstance(c, dict) else c for c in entry["closes"]]
    return []


def fact_for_name(
    symbol: str,
    *,
    bars: list[Any] | None,
    chain: Any = None,
    option_facts: list[Any] | None = None,
    quote_iv: Any = None,
) -> dict[str, Any] | None:
    """Present only when this taped name already has candles and a chain."""
    su = _sym(symbol)
    if not su or not _chain_ok(chain):
        return None
    rv = realized_state(bars)
    if rv is None:
        return None
    ivs = ivs_from_chain(chain)
    ivs.extend(ivs_from_option_facts(option_facts, su))
    q = iv_to_pct(quote_iv)
    if q is not None:
        ivs.append(q)
    pivot = ivs[len(ivs) // 2] if ivs else q
    if pivot is None and ivs:
        pivot = ivs[0]
    row: dict[str, Any] = {"sym": su, "rv": rv["rv"]}
    if pivot is not None:
        row["iv"] = pivot
        rank = iv_rank(pivot, ivs)
        if rank is not None:
            row["ivr"] = rank
        gap = iv_minus_rv(pivot, rv.get("rv20"))
        if gap is not None:
            row["iv_rv"] = gap
    return row


def collect_vol_facts(
    world: Any = None,
    snap: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    tape = taped_symbols(world, snap)
    if not tape:
        return []
    bars = _bar_store(snap)
    chains = _chain_store(snap)
    qiv = _quote_ivs(snap)
    facts = list(getattr(world, "option_facts", None) or [])
    blob = snap if isinstance(snap, dict) else {}
    if blob.get("option_facts"):
        facts = list(blob.get("option_facts") or facts)
    out: list[dict[str, Any]] = []
    for su in tape:
        row = fact_for_name(
            su,
            bars=_bars_of(bars.get(su)),
            chain=chains.get(su),
            option_facts=facts,
            quote_iv=qiv.get(su),
        )
        if row:
            out.append(row)
        if len(out) >= BOOK_VOL_NAMES:
            break
    return out


def clip_vol_facts(rows: list[Any] | None, *, cap: int = BOOK_VOL_NAMES) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        su = _sym(row.get("sym") or row.get("symbol"))
        if not su:
            continue
        item: dict[str, Any] = {"sym": su}
        if row.get("rv") in ("high", "mid", "low"):
            item["rv"] = row["rv"]
        if row.get("iv") is not None:
            item["iv"] = row["iv"]
        if row.get("ivr") is not None:
            item["ivr"] = row["ivr"]
        if row.get("iv_rv"):
            item["iv_rv"] = row["iv_rv"]
        if "rv" not in item:
            continue
        out.append(item)
        if len(out) >= max(0, int(cap)):
            break
    return out


def wake_vol_bit(rows: list[Any] | None) -> str:
    """One clipped token. Omit when empty so wake stays a short line."""
    bits: list[str] = []
    for row in clip_vol_facts(rows, cap=WAKE_VOL_NAMES):
        su = row["sym"]
        piece = f"{su} rv={row.get('rv')}"
        if row.get("iv") is not None:
            piece += f" iv={row['iv']}"
        if row.get("ivr") is not None:
            piece += f" ivr={row['ivr']}"
        if row.get("iv_rv"):
            piece += f" iv-rv={row['iv_rv']}"
        bits.append(piece)
    text = ",".join(bits)
    if len(text) > WAKE_VOL_CHARS:
        text = text[: WAKE_VOL_CHARS - 1].rstrip(",") + "…"
    return text


def stash_look_bars(
    snap: dict[str, Any] | None,
    symbol: str,
    bars: list[Any] | None,
    *,
    resolution: str = "",
) -> None:
    su = _sym(symbol)
    compact = compact_bars(bars)
    if not isinstance(snap, dict) or not su or not compact:
        return
    store = snap.get("candle_bars")
    if not isinstance(store, dict):
        store = {}
        snap["candle_bars"] = store
    row: dict[str, Any] = {"bars": compact}
    if resolution:
        row["resolution"] = resolution
    store[su] = row


def stash_look_chain(snap: dict[str, Any] | None, chain: Any) -> None:
    if not isinstance(snap, dict) or not isinstance(chain, dict):
        return
    su = _sym(chain.get("symbol"))
    if not su or not _chain_ok(chain):
        return
    store = snap.get("option_chains")
    if not isinstance(store, dict):
        store = {}
        snap["option_chains"] = store
    slim: dict[str, Any] = {"symbol": su}
    for key in ("expirations", "strikes", "n_strikes", "iv", "ivs", "implied_vol"):
        if chain.get(key) not in (None, "", []):
            slim[key] = chain[key]
    store[su] = slim


def stash_look_iv(snap: dict[str, Any] | None, symbol: str, raw: Any) -> None:
    su = _sym(symbol)
    pct = iv_to_pct(raw)
    if not isinstance(snap, dict) or not su or pct is None:
        return
    store = snap.get("quote_ivs")
    if not isinstance(store, dict):
        store = {}
        snap["quote_ivs"] = store
    store[su] = pct


def publish_vol_facts(world: Any = None, snap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Recompute from this look's tape. Do not persist across sessions."""
    rows = clip_vol_facts(collect_vol_facts(world, snap))
    if isinstance(snap, dict):
        snap["vol_facts"] = rows
    if world is not None:
        try:
            world.vol_facts = rows
        except Exception:
            pass
    return rows


def banned_vol_prompt_terms(text: str) -> list[str]:
    blob = str(text or "").lower()
    return [w for w in _BANNED_PROMPT if w in blob]
