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
    """Whether new risk is blocked by this dossier. Exits are not decided here."""
    if dossier is None or dossier == {}:
        return True, "no_dossier"
    earnings = dossier.get("earnings")
    if earnings is None or earnings == "" or earnings == "unknown":
        return True, "earnings_unknown"
    earnings_in = dossier.get("earnings_in")
    if (
        isinstance(earnings_in, int)
        and not isinstance(earnings_in, bool)
        and earnings_in <= 2
    ):
        return True, "earnings_window"
    return False, ""
