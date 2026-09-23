"""One research dossier per symbol from the allocation snapshot.

Price and relative strength are copied from the snapshot. Scan gaps, calendar,
news, web, odds, and spend each keep their own clock. New-risk gates live here;
exits are not decided here. Do not grow SYSTEM_PROMPT.
"""

from __future__ import annotations

from typing import Any

# Optional helpers. Import failure => local stubs that mark fields unavailable
# so this module still imports when siblings are absent.
try:
    from abcxauto.research_color import scan_gap as _scan_gap_impl
except Exception:  # pragma: no cover - sibling may not exist yet
    _scan_gap_impl = None  # type: ignore[assignment]

try:
    from abcxauto.research_color import spend_slot as _spend_slot_impl
except Exception:  # pragma: no cover
    _spend_slot_impl = None  # type: ignore[assignment]

try:
    from abcxauto.research_news import news_package as _news_package_impl
except Exception:  # pragma: no cover
    _news_package_impl = None  # type: ignore[assignment]

try:
    from abcxauto.research_calendar import parse_calendar_report as _parse_calendar_impl
except Exception:  # pragma: no cover
    _parse_calendar_impl = None  # type: ignore[assignment]

try:
    from abcxauto import look_ledger as _look_ledger  # noqa: F401
except Exception:  # pragma: no cover
    _look_ledger = None  # type: ignore[assignment]


def _unavailable_scan() -> dict[str, Any]:
    return {"scan_asof": "", "gap": "unavailable", "vs_open": "unavailable"}


def _stub_scan_gap(rows: list, symbol: str) -> dict[str, Any]:
    """Local scan gap. Never copies row last."""
    sym = str(symbol or "").strip().upper()
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("symbol") or "").strip().upper() != sym:
            continue
        gap: Any = raw.get("open_gap_pct")
        if gap is None:
            gap = raw.get("gap%")
        if gap is None:
            gap = raw.get("gap")
        if gap is None:
            gap = "unavailable"
        vs_open = raw.get("vs_open")
        if vs_open is None:
            vs_open = "unavailable"
        scan_asof = raw.get("scan_asof") or raw.get("asof") or ""
        return {"scan_asof": scan_asof, "gap": gap, "vs_open": vs_open}
    return _unavailable_scan()


def _scan_gap(rows: list, symbol: str) -> dict[str, Any]:
    if _scan_gap_impl is not None:
        try:
            out = _scan_gap_impl(rows, symbol)
            if isinstance(out, dict):
                # Never let a helper smuggle scan last into the package.
                clean = {
                    "scan_asof": out.get("scan_asof", ""),
                    "gap": out.get("gap", "unavailable"),
                    "vs_open": out.get("vs_open", "unavailable"),
                }
                if clean["gap"] is None:
                    clean["gap"] = "unavailable"
                if clean["vs_open"] is None:
                    clean["vs_open"] = "unavailable"
                return clean
        except Exception:
            pass
    return _stub_scan_gap(rows, symbol)


def _stub_spend_slot(ledger_row: dict | None) -> dict[str, Any]:
    if isinstance(ledger_row, dict):
        asof = ledger_row.get("asof") or ledger_row.get("spend_asof")
        usd = ledger_row.get("usd")
        if usd is None:
            usd = ledger_row.get("session_usd")
        if asof not in (None, "") and usd is not None and usd != "unavailable":
            return {"spend_asof": asof, "session_usd": usd}
        if ledger_row.get("spend_asof") is not None or ledger_row.get("session_usd") is not None:
            return {
                "spend_asof": ledger_row.get("spend_asof") or "",
                "session_usd": ledger_row.get("session_usd", "unavailable"),
            }
    return {"spend_asof": "", "session_usd": "unavailable"}


def _spend_fields(spend: dict | None) -> dict[str, Any]:
    if _spend_slot_impl is not None:
        try:
            out = _spend_slot_impl(spend)
            if isinstance(out, dict):
                return {
                    "spend_asof": out.get("spend_asof") or "",
                    "session_usd": out.get("session_usd", "unavailable"),
                }
        except Exception:
            pass
    return _stub_spend_slot(spend)


def _stub_news_package(payload: dict | None, *, price_asof: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"news_asof": "", "headlines": [], "news": "unavailable"}
    if payload.get("error") or payload.get("ok") is False:
        return {
            "news_asof": payload.get("news_asof") or payload.get("asof") or "",
            "headlines": [],
            "news": "unavailable",
        }
    if "headlines" in payload or payload.get("news") in ("ok", "unavailable"):
        headlines = payload.get("headlines")
        if not isinstance(headlines, list):
            headlines = []
        news_flag = payload.get("news")
        if news_flag not in ("ok", "unavailable"):
            news_flag = "ok" if headlines else "unavailable"
        return {
            "news_asof": payload.get("news_asof") or payload.get("asof") or "",
            "headlines": list(headlines),
            "news": news_flag,
        }
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return {
            "news_asof": payload.get("news_asof") or payload.get("asof") or "",
            "headlines": [],
            "news": "unavailable",
        }
    return {
        "news_asof": payload.get("news_asof") or payload.get("asof") or price_asof or "",
        "headlines": [dict(x) for x in items if isinstance(x, dict)],
        "news": "ok",
    }


def _news_fields(news: dict, *, price_asof: str, symbol: str) -> dict[str, Any]:
    if _news_package_impl is not None:
        try:
            # Already packaged (has news_asof / headlines) — pass through shape.
            if isinstance(news, dict) and (
                "headlines" in news or news.get("news") in ("ok", "unavailable")
            ):
                pkg = {
                    "news_asof": news.get("news_asof") or news.get("asof") or "",
                    "headlines": list(news.get("headlines") or [])
                    if isinstance(news.get("headlines"), list)
                    else [],
                    "news": news.get("news")
                    if news.get("news") in ("ok", "unavailable")
                    else ("ok" if news.get("headlines") else "unavailable"),
                }
            else:
                pkg = _news_package_impl(news, price_asof=price_asof)
        except Exception:
            pkg = _stub_news_package(news, price_asof=price_asof)
    else:
        pkg = _stub_news_package(news, price_asof=price_asof)

    if not isinstance(pkg, dict):
        return {"news_asof": "", "headlines": []}

    sym = str(symbol or "").strip().upper()
    headlines = pkg.get("headlines") if isinstance(pkg.get("headlines"), list) else []
    filtered = [
        h
        for h in headlines
        if isinstance(h, dict)
        and (
            not str(h.get("symbol") or "").strip()
            or str(h.get("symbol") or "").strip().upper() == sym
        )
    ]
    # If payload was already symbol-scoped or has no symbol tags, keep all.
    if not filtered and headlines:
        tagged = any(str(h.get("symbol") or "").strip() for h in headlines if isinstance(h, dict))
        if not tagged:
            filtered = [h for h in headlines if isinstance(h, dict)]
    return {
        "news_asof": pkg.get("news_asof") or "",
        "headlines": filtered,
    }


def _pick_symbol_bag(bag: dict, symbol: str) -> dict[str, Any] | None:
    if not isinstance(bag, dict):
        return None
    sym = str(symbol or "").strip().upper()
    for key in (sym, str(symbol or "").strip(), symbol):
        if key and isinstance(bag.get(key), dict):
            return bag[key]
    for nest in ("by_symbol", "names", "symbols"):
        inner = bag.get(nest)
        if isinstance(inner, dict):
            for key in (sym, str(symbol or "").strip()):
                if key and isinstance(inner.get(key), dict):
                    return inner[key]
    return None


def _calendar_fields(calendar: dict, symbol: str) -> dict[str, Any]:
    row = _pick_symbol_bag(calendar, symbol) if isinstance(calendar, dict) else None
    if row is None and isinstance(calendar, dict):
        # Flat single-symbol calendar report.
        if any(k in calendar for k in ("earnings", "earnings_in", "calendar_asof", "ex_div")):
            row = calendar
        elif "text" in calendar or "report" in calendar:
            text = str(calendar.get("text") or calendar.get("report") or "")
            today = str(calendar.get("today") or "")
            if _parse_calendar_impl is not None and text and today:
                try:
                    row = _parse_calendar_impl(text, today=today)
                except Exception:
                    row = None
    if not isinstance(row, dict):
        return {
            "calendar_asof": "",
            "earnings": "unknown",
            "earnings_in": None,
            "ex_div": None,
        }
    earnings = row.get("earnings")
    if earnings is None or earnings == "":
        earnings = "unknown"
    return {
        "calendar_asof": row.get("calendar_asof") or "",
        "earnings": earnings,
        "earnings_in": row.get("earnings_in"),
        "ex_div": row.get("ex_div"),
    }


def _color_side(bag: dict, symbol: str, *, value_key: str, asof_key: str) -> dict[str, Any]:
    """web / odds: per-symbol override else top-level, else unavailable."""
    if not isinstance(bag, dict):
        return {asof_key: "", value_key: "unavailable"}
    row = _pick_symbol_bag(bag, symbol)
    src = row if isinstance(row, dict) else bag
    value = src.get(value_key)
    if value is None:
        # Odds payload may nest implied probs under "probs" / symbol.
        if value_key == "odds" and "odds" not in src:
            if any(k in src for k in ("probs", "implied", "markets")):
                value = {
                    k: src[k]
                    for k in ("probs", "implied", "markets", "query")
                    if k in src
                } or "unavailable"
            else:
                value = "unavailable"
        else:
            value = "unavailable"
    asof = src.get(asof_key)
    if asof in (None, ""):
        asof = src.get("asof") or ""
    return {asof_key: asof, value_key: value}


def assemble_dossiers(
    snapshot: dict,
    *,
    scan_rows: list,
    calendar: dict,
    news: dict,
    web: dict,
    odds: dict,
    spend: dict | None,
) -> dict[str, dict]:
    """Build one dossier per snapshot name. Price is copied from the snapshot only."""
    if not snapshot or snapshot.get("ok") is False:
        return {}
    names = snapshot.get("names")
    if not isinstance(names, dict) or not names:
        return {}

    spend_bits = _spend_fields(spend)
    out: dict[str, dict] = {}
    for raw_sym, name in names.items():
        sym = str(raw_sym or "").strip().upper()
        if not sym:
            continue
        bag = name if isinstance(name, dict) else {}
        # Allocation snapshot only — never a scan row last.
        price_asof = bag.get("asof") or bag.get("price_asof") or ""
        last = bag.get("last")
        bar_date = bag.get("bar_date")
        vs_spy = bag.get("vs_spy")

        gap_bits = _scan_gap(scan_rows if isinstance(scan_rows, list) else [], sym)
        cal_bits = _calendar_fields(calendar if isinstance(calendar, dict) else {}, sym)
        news_bits = _news_fields(
            news if isinstance(news, dict) else {},
            price_asof=str(price_asof or ""),
            symbol=sym,
        )
        web_bits = _color_side(
            web if isinstance(web, dict) else {},
            sym,
            value_key="web",
            asof_key="web_asof",
        )
        odds_bits = _color_side(
            odds if isinstance(odds, dict) else {},
            sym,
            value_key="odds",
            asof_key="odds_asof",
        )

        out[sym] = {
            "price_asof": price_asof,
            "bar_date": bar_date,
            "vs_spy": vs_spy,
            "last": last,
            "scan_asof": gap_bits.get("scan_asof", ""),
            "gap": gap_bits.get("gap", "unavailable"),
            "vs_open": gap_bits.get("vs_open", "unavailable"),
            "calendar_asof": cal_bits.get("calendar_asof", ""),
            "earnings": cal_bits.get("earnings", "unknown"),
            "earnings_in": cal_bits.get("earnings_in"),
            "ex_div": cal_bits.get("ex_div"),
            "news_asof": news_bits.get("news_asof", ""),
            "headlines": news_bits.get("headlines", []),
            "web_asof": web_bits.get("web_asof", ""),
            "web": web_bits.get("web", "unavailable"),
            "odds_asof": odds_bits.get("odds_asof", ""),
            "odds": odds_bits.get("odds", "unavailable"),
            "spend_asof": spend_bits.get("spend_asof", ""),
            "session_usd": spend_bits.get("session_usd", "unavailable"),
        }
        if out[sym]["gap"] is None:
            out[sym]["gap"] = "unavailable"
        if out[sym]["earnings"] in (None, ""):
            out[sym]["earnings"] = "unknown"
    return out


def dossier_blocks_new_risk(dossier: dict | None) -> tuple[bool, str]:
    """Whether new risk is blocked by this dossier. Exits are not decided here.

    Unknown / unavailable / missing earnings do not refuse. Only a known
    ``earnings_in`` inside 2 sessions refuses. A missing dossier still refuses.
    """
    if dossier is None or dossier == {}:
        return True, "no_dossier"
    earnings_in = dossier.get("earnings_in")
    if (
        isinstance(earnings_in, int)
        and not isinstance(earnings_in, bool)
        and earnings_in <= 2
    ):
        return True, "earnings_window"
    return False, ""


def _finite_last(raw: Any) -> Any:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n != n or n <= 0:  # NaN / non-positive
        return None
    return n


def _sym_match(raw: Any, sym: str) -> bool:
    name = str(raw or "").strip().upper()
    return bool(name) and name == sym


def _session_row(snap: dict, sym: str) -> dict[str, Any] | None:
    store = snap.get("session_range")
    if not isinstance(store, dict):
        return None
    row = store.get(sym)
    if row is None:
        for key, val in store.items():
            if _sym_match(key, sym):
                row = val
                break
    return row if isinstance(row, dict) else None


def _scan_hit_row(snap: dict, sym: str) -> dict[str, Any] | None:
    hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
    rows = hits.get("rows") if isinstance(hits, dict) else None
    if not isinstance(rows, list):
        return None
    for raw in rows:
        if isinstance(raw, dict) and _sym_match(raw.get("symbol"), sym):
            return raw
    return None


def _quote_on_snap(snap: dict, sym: str) -> Any:
    quotes = snap.get("ibkr_live_quotes")
    if isinstance(quotes, dict):
        if sym in quotes:
            last = _finite_last(quotes.get(sym))
            if last is not None:
                return last
        for key, raw in quotes.items():
            if _sym_match(key, sym):
                last = _finite_last(raw)
                if last is not None:
                    return last
    for blob in (snap.get("quotes"),):
        if not isinstance(blob, list):
            continue
        for row in blob:
            if not isinstance(row, dict) or not _sym_match(row.get("symbol"), sym):
                continue
            last = _finite_last(row.get("last") if row.get("last") is not None else row.get("mid"))
            if last is not None:
                return last
    return None


def _existing_last(snap: dict, sym: str) -> Any:
    """Copy a last that already exists on the snap. Never invent one."""
    row = _session_row(snap, sym)
    if isinstance(row, dict):
        last = _finite_last(row.get("last"))
        if last is not None:
            return last
    hit = _scan_hit_row(snap, sym)
    if isinstance(hit, dict):
        last = _finite_last(hit.get("last"))
        if last is not None:
            return last
    return _quote_on_snap(snap, sym)


def _news_headlines_for(snap: dict, sym: str) -> tuple[str, list[dict]]:
    items = snap.get("news_items")
    if not isinstance(items, list):
        return "", []
    out: list[dict] = []
    asof = ""
    for it in items:
        if not isinstance(it, dict):
            continue
        tagged = str(it.get("symbol") or it.get("ticker") or "").strip().upper()
        headline = str(it.get("headline") or it.get("title") or it.get("text") or "")
        if tagged == sym or (
            not tagged and headline.upper().startswith(sym)
            and (len(headline) == len(sym) or not headline[len(sym) : len(sym) + 1].isalnum())
        ):
            out.append(dict(it))
            if not asof:
                asof = str(it.get("asof") or it.get("news_asof") or it.get("ts") or "")
    return asof, out


def _candles_this_look(snap: dict, sym: str) -> bool:
    if _session_row(snap, sym) is not None:
        return True
    bag = snap.get("_research_bag")
    if not isinstance(bag, dict):
        return False
    for fact in bag.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        if str(fact.get("source") or "") != "candles":
            continue
        text = str(fact.get("text") or "")
        if text.upper().startswith(sym) and (
            len(text) == len(sym) or not text[len(sym) : len(sym) + 1].isalnum()
        ):
            return True
        for part in text.split("|"):
            bit = part.strip()
            if bit.upper().startswith(sym) and (
                len(bit) == len(sym) or not bit[len(sym) : len(sym) + 1].isalnum()
            ):
                return True
    return False


def _news_this_look(snap: dict, sym: str) -> bool:
    _asof, headlines = _news_headlines_for(snap, sym)
    return bool(headlines)


def _web_this_look(snap: dict) -> bool:
    """True for a fetched page or a search_public hit (text / cited urls)."""
    page = snap.get("research_web")
    if not isinstance(page, dict) or str(page.get("error") or "").strip():
        return False
    if str(page.get("text") or "").strip():
        return True
    if str(page.get("url") or "").strip() and str(page.get("title") or "").strip():
        return True
    results = page.get("results")
    if isinstance(results, list):
        for row in results:
            if isinstance(row, dict) and str(row.get("url") or "").strip():
                return True
    return False


def _scan_priced_this_look(snap: dict, sym: str) -> bool:
    hit = _scan_hit_row(snap, sym)
    if hit is None:
        return False
    if _finite_last(hit.get("last")) is not None:
        return True
    return _quote_on_snap(snap, sym) is not None


def this_look_has_research(snap: dict | None, symbol: str) -> bool:
    """True when this look already has news, web, candles, or a priced scan hit."""
    if not isinstance(snap, dict):
        return False
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    return bool(
        _news_this_look(snap, sym)
        or _web_this_look(snap)
        or _candles_this_look(snap, sym)
        or _scan_priced_this_look(snap, sym)
    )


def ensure_this_look_dossier(snap: dict | None, symbol: str) -> dict | None:
    """Upsert a real dossier from this-look research. None if research is thin.

    Copies price / headlines / web already on the snap. Does not invent a last,
    earnings date, or headline. Earnings unknown is allowed.
    """
    if not isinstance(snap, dict):
        return None
    sym = str(symbol or "").strip().upper()
    if not sym or not this_look_has_research(snap, sym):
        return None

    news_asof, headlines = _news_headlines_for(snap, sym)
    web_asof = ""
    web_val: Any = "unavailable"
    if _web_this_look(snap):
        page = snap.get("research_web") if isinstance(snap.get("research_web"), dict) else {}
        web_asof = str(page.get("asof") or page.get("web_asof") or "")
        web_val = page.get("text") or page.get("title") or "ok"

    session = _session_row(snap, sym) or {}
    hit = _scan_hit_row(snap, sym)
    scan_rows = [hit] if isinstance(hit, dict) else []
    gap_bits = _scan_gap(scan_rows, sym)

    bag = snap.get("_research_bag") if isinstance(snap.get("_research_bag"), dict) else {}
    calendar = bag.get("calendar") if isinstance(bag.get("calendar"), dict) else {}
    # Also accept a top-level calendar bag if present.
    if not calendar and isinstance(snap.get("calendar"), dict):
        calendar = snap["calendar"]
    cal_bits = _calendar_fields(calendar, sym)

    spend_src = bag.get("spend") if isinstance(bag.get("spend"), dict) else snap.get("session_spend")
    if not isinstance(spend_src, dict):
        spend_src = None
    spend_bits = _spend_fields(spend_src)

    last = _existing_last(snap, sym)
    price_asof = ""
    bar_date = session.get("date") or session.get("bar_date")
    vs_spy = session.get("vs_spy")
    if isinstance(hit, dict) and not price_asof:
        price_asof = str(hit.get("asof") or hit.get("scan_asof") or "")

    dossier = {
        "price_asof": price_asof,
        "bar_date": bar_date,
        "vs_spy": vs_spy,
        "last": last,
        "scan_asof": gap_bits.get("scan_asof", ""),
        "gap": gap_bits.get("gap", "unavailable"),
        "vs_open": gap_bits.get("vs_open", "unavailable")
        if gap_bits.get("vs_open") is not None
        else session.get("vs_open", "unavailable"),
        "calendar_asof": cal_bits.get("calendar_asof", ""),
        "earnings": cal_bits.get("earnings", "unknown"),
        "earnings_in": cal_bits.get("earnings_in"),
        "ex_div": cal_bits.get("ex_div"),
        "news_asof": news_asof,
        "headlines": headlines,
        "web_asof": web_asof,
        "web": web_val,
        "odds_asof": "",
        "odds": "unavailable",
        "spend_asof": spend_bits.get("spend_asof", ""),
        "session_usd": spend_bits.get("session_usd", "unavailable"),
    }
    if dossier["gap"] is None:
        dossier["gap"] = "unavailable"
    if dossier["earnings"] in (None, ""):
        dossier["earnings"] = "unknown"
    if dossier["vs_open"] is None:
        dossier["vs_open"] = "unavailable"

    store = snap.get("dossiers")
    if not isinstance(store, dict):
        store = {}
        snap["dossiers"] = store
    store[sym] = dossier
    return dossier
