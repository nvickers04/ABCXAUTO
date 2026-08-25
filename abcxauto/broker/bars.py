"""IBKR bars for the candles tool. Shape matches MDA OHLCV."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from abcxauto.broker.connection import safe_sleep as _safe_sleep
from abcxauto.prints import bar_time_fields

logger = logging.getLogger(__name__)


def normalize_resolution(resolution: str) -> str:
    """Grok writes 5, 5m, 5min, 5-min — they all mean the same IBKR hist."""
    key = str(resolution or "").strip().upper().replace(" ", "").replace("-", "")
    aliases = {
        "5": "5",
        "5M": "5",
        "5MIN": "5",
        "5MINS": "5",
        "5MINUTE": "5",
        "5MINUTES": "5",
        "15": "15",
        "15M": "15",
        "15MIN": "15",
        "15MINS": "15",
        "15MINUTE": "15",
        "15MINUTES": "15",
        "60": "60",
        "60M": "60",
        "60MIN": "60",
        "60MINS": "60",
        "1H": "60",
        "H": "60",
        "1HOUR": "60",
        "D": "D",
        "1D": "D",
        "1DAY": "D",
        "DAY": "D",
        "DAILY": "D",
    }
    return aliases.get(key, key or "D")


def hist_spec(resolution: str) -> tuple[str, str]:
    """IBKR (barSizeSetting, durationStr) for a Grok candles resolution."""
    key = normalize_resolution(resolution)
    if key == "60":
        return "1 hour", "10 D"
    if key == "15":
        return "15 mins", "5 D"
    if key == "5":
        return "5 mins", "3 D"
    return "1 day", "6 M"


def session_countback(
    resolution: str,
    *,
    n_symbols: int = 1,
    now: datetime | None = None,
) -> int:
    """Keep enough intraday bars to include today's 09:30 ET print.

    A multi-symbol 40-bar cap at 15:00 ET starts at 11:40 — hold-above-open
    then uses a midday print as the session open.
    """
    key = normalize_resolution(resolution)
    width = {"5": 5, "15": 15, "60": 60}.get(key)
    default = 40 if n_symbols > 1 else 80
    if width is None:
        return default
    clock = now if isinstance(now, datetime) else datetime.now(ZoneInfo("America/New_York"))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        clock = clock.astimezone(ZoneInfo("America/New_York"))
    elapsed = max(0, clock.hour * 60 + clock.minute - (9 * 60 + 30))
    need = elapsed // width + 6
    return max(default, min(120, int(need)))


def _bar_stamp(bar: Any) -> str:
    dt = getattr(bar, "date", None)
    if dt is None:
        dt = getattr(bar, "time", None)
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt or "")


def _bar_open(bar: Any) -> Any:
    val = getattr(bar, "open", None)
    if val is None:
        val = getattr(bar, "open_", None)
    return val


def bars_from_ibkr(raw: Any) -> list[dict[str, Any]]:
    """Normalize ib_insync BarData / RealTimeBar list to {t,o,h,l,c,v}."""
    out: list[dict[str, Any]] = []
    for bar in raw or []:
        stamp = getattr(bar, "date", None)
        if stamp is None:
            stamp = getattr(bar, "time", None)
        try:
            close = float(getattr(bar, "close"))
        except (TypeError, ValueError):
            continue
        try:
            vol = int(getattr(bar, "volume", 0) or 0)
        except (TypeError, ValueError):
            vol = 0
        row: dict[str, Any] = {**bar_time_fields(stamp), "c": close, "v": vol}
        if not row.get("t"):
            row["t"] = _bar_stamp(bar)
        for src, key in (("high", "h"), ("low", "l")):
            try:
                row[key] = float(getattr(bar, src))
            except (TypeError, ValueError):
                pass
        try:
            row["o"] = float(_bar_open(bar))
        except (TypeError, ValueError):
            pass
        out.append(row)
    return out


def ibkr_bar_freshness(resolution: str) -> str:
    key = normalize_resolution(resolution)
    if key in ("5S", "RT", "RT5"):
        return "ibkr_rt_5s"
    if key in ("D", "1D", "1DAY", "DAY"):
        return "ibkr_rth"
    return "ibkr_rth"


class IBKRBarsMixin:
    """Connector-side bar feeds: RTH history plus the live 5s stream.

    Mixed into :class:`~abcxauto.broker.connector.IBKRConnector`, which supplies
    ``ib``, ``async_lock``, ``_ensure_connected()`` and ``_prepare_contract()``.
    """

    async def get_historical_bars(
        self,
        symbol: str,
        *,
        resolution: str = "D",
        countback: int = 60,
    ) -> dict[str, Any]:
        """IBKR RTH OHLCV. Same {t,o,h,l,c,v} shape as MDA candles."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return {"error": "symbol required", "source": "ibkr", "symbol": sym}
        if not await self._ensure_connected():
            return {"error": "Not connected", "source": "ibkr", "symbol": sym}
        try:
            n = max(5, min(int(countback or 60), 120))
        except (TypeError, ValueError):
            n = 60
        bar_size, duration = hist_spec(resolution)
        contract = None
        try:
            prepare = getattr(self, "_prepare_contract", None)
            if callable(prepare):
                contract = await prepare(sym)
        except Exception as exc:
            logger.warning("qualify %s for hist failed: %s", sym, exc)
            return {"error": str(exc), "source": "ibkr", "symbol": sym}
        if contract is None:
            return {"error": "qualify failed", "source": "ibkr", "symbol": sym}
        req = getattr(self.ib, "reqHistoricalDataAsync", None)
        if not callable(req):
            return {"error": "hist unavailable", "source": "ibkr", "symbol": sym}
        try:
            async with self.async_lock:
                raw = await req(
                    contract,
                    endDateTime="",
                    durationStr=duration,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
        except Exception as exc:
            logger.warning("IBKR hist failed for %s: %s", sym, exc)
            return {"error": str(exc), "source": "ibkr", "symbol": sym}
        bars = bars_from_ibkr(raw)[-n:]
        if not bars:
            logger.warning("IBKR hist empty for %s %s/%s", sym, bar_size, duration)
            return {"error": "no IBKR bars", "source": "ibkr", "symbol": sym}
        last_bar = bars[-1]
        out: dict[str, Any] = {
            "symbol": sym,
            "bars": bars,
            "source": "ibkr",
            "freshness": ibkr_bar_freshness(resolution),
            "resolution": str(resolution or "D").strip() or "D",
            "use": "ibkr_rth_structure",
        }
        if last_bar.get("t_unix"):
            out["asof"] = last_bar["t_unix"]
            if last_bar.get("t_iso"):
                out["asof_iso"] = last_bar["t_iso"]
        return out

    RT_BAR_S = 5
    RT_BUF = 120
    RT_SUB_CAP = 8
    RT_WAIT_S = 7.0

    def _rt_maps(self) -> tuple[dict, dict, list]:
        if getattr(self, "_rt_subs", None) is None:
            self._rt_subs = {}
            self._rt_buf = {}
            self._rt_lru = []
            self._rt_keys = {}
        if getattr(self, "_rt_keys", None) is None:
            self._rt_keys = {}
        return self._rt_subs, self._rt_buf, self._rt_lru

    def _rt_sub_key(self, symbol: str, contract: Any) -> Any:
        try:
            cid = int(getattr(contract, "conId", 0) or 0)
        except (TypeError, ValueError):
            cid = 0
        return cid if cid > 0 else f"sym:{symbol}"

    def realtime_bar_buffer(self, symbol: str) -> list[dict[str, Any]]:
        """Warm 5s bars already in memory. Empty if the stream is cold."""
        _subs, buf, _lru = self._rt_maps()
        return list(buf.get(str(symbol or "").strip().upper()) or [])

    def abandon_realtime_bars(self) -> None:
        """Drop RT bar handles after a socket death. Cancel if the client is up."""
        subs, buf, lru = self._rt_maps()
        ib = getattr(self, "ib", None)
        cancel = getattr(ib, "cancelRealTimeBars", None) if ib is not None else None
        for bars in list(subs.values()):
            if callable(cancel):
                try:
                    cancel(bars)
                except Exception:
                    pass
        subs.clear()
        buf.clear()
        lru.clear()
        getattr(self, "_rt_keys", {}).clear()

    def _stop_rt_key(self, key: Any, symbol: str | None = None) -> None:
        subs, buf, lru = self._rt_maps()
        bars = subs.pop(key, None)
        if symbol:
            buf.pop(symbol, None)
            keys = getattr(self, "_rt_keys", {})
            if keys.get(symbol) == key:
                keys.pop(symbol, None)
            if symbol in lru:
                lru.remove(symbol)
        if bars is None:
            return
        cancel = getattr(getattr(self, "ib", None), "cancelRealTimeBars", None)
        if callable(cancel):
            try:
                cancel(bars)
            except Exception:
                pass

    def _stop_rt_bars(self, symbol: str) -> None:
        sym = str(symbol or "").strip().upper()
        keys = getattr(self, "_rt_keys", {})
        key = keys.pop(sym, None)
        if key is None:
            key = f"sym:{sym}"
        self._stop_rt_key(key, sym)

    def _rt_touch(self, symbol: str) -> None:
        _subs, _buf, lru = self._rt_maps()
        if symbol in lru:
            lru.remove(symbol)
        lru.append(symbol)
        while len(lru) > self.RT_SUB_CAP:
            old = lru.pop(0)
            if old != symbol:
                self._stop_rt_bars(old)

    def _rt_ingest(self, symbol: str, bars: Any) -> None:
        if not bars:
            return
        rows = bars_from_ibkr([bars[-1]])
        if not rows:
            return
        _subs, buf, _lru = self._rt_maps()
        bucket = buf.setdefault(symbol, [])
        row = rows[0]
        if bucket and bucket[-1].get("t") == row.get("t"):
            bucket[-1] = row
            return
        bucket.append(row)
        del bucket[: -self.RT_BUF]

    def start_realtime_bars(self, symbol: str, contract: Any, *, what: str = "TRADES") -> Any:
        """Subscribe 5s OHLCV. Idempotent per symbol. Does not wait for a bar."""
        sym = str(symbol or "").strip().upper()
        if not sym or contract is None:
            return None
        ib = getattr(self, "ib", None)
        req = getattr(ib, "reqRealTimeBars", None)
        if not callable(req):
            return None
        subs, buf, _lru = self._rt_maps()
        keys = getattr(self, "_rt_keys", {})
        key = self._rt_sub_key(sym, contract)
        old = keys.get(sym)
        if old is not None and old != key:
            self._stop_rt_key(old, sym)
        existing = subs.get(key)
        if existing is not None:
            keys[sym] = key
            self._rt_touch(sym)
            return existing
        bars = req(contract, self.RT_BAR_S, what, False)
        ev = getattr(bars, "updateEvent", None)
        if ev is not None:
            ev += lambda b, has_new=True, _sym=sym: self._rt_ingest(_sym, b)
        subs[key] = bars
        keys[sym] = key
        buf.setdefault(sym, [])
        self._rt_touch(sym)
        if bars:
            self._rt_ingest(sym, bars)
        return bars

    async def get_realtime_bars(
        self,
        symbol: str,
        *,
        resolution: str = "D",
        countback: int = 60,
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """Live IBKR 5-second bars. Buffer stays warm while the sub is up."""
        sym = str(symbol or "").strip().upper()
        req_res = str(resolution or "D").strip() or "D"
        if not sym:
            return {"error": "symbol required", "source": "ibkr", "symbol": sym}
        if not await self._ensure_connected():
            return {"error": "Not connected", "source": "ibkr", "symbol": sym}
        try:
            n = max(1, min(int(countback or 60), self.RT_BUF))
        except (TypeError, ValueError):
            n = 60
        contract = None
        try:
            prepare = getattr(self, "_prepare_contract", None)
            if callable(prepare):
                contract = await prepare(sym)
        except Exception as exc:
            logger.warning("qualify %s for realtime bars failed: %s", sym, exc)
            return {"error": str(exc), "source": "ibkr", "symbol": sym}
        if contract is None:
            return {"error": "qualify failed", "source": "ibkr", "symbol": sym}
        wait = self.RT_WAIT_S if wait_s is None else max(0.0, float(wait_s))
        _subs, buf, _lru = self._rt_maps()
        try:
            started = self.start_realtime_bars(sym, contract, what="TRADES")
        except Exception as exc:
            logger.warning("IBKR realtime bars failed for %s: %s", sym, exc)
            return {"error": str(exc), "source": "ibkr", "symbol": sym}
        if started is None and not buf.get(sym):
            return {"error": "realtime bars unavailable", "source": "ibkr", "symbol": sym}

        async def _wait(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if buf.get(sym):
                    return
                await _safe_sleep(0.25)

        if not buf.get(sym) and wait > 0:
            await _wait(wait)
        if not buf.get(sym):
            self._stop_rt_bars(sym)
            try:
                started = self.start_realtime_bars(sym, contract, what="MIDPOINT")
            except Exception as exc:
                logger.warning("IBKR midpoint bars failed for %s: %s", sym, exc)
                return {"error": str(exc), "source": "ibkr", "symbol": sym}
            if started is None and not buf.get(sym):
                return {"error": "realtime bars unavailable", "source": "ibkr", "symbol": sym}
            if wait > 0:
                await _wait(min(3.0, wait))
        bars = list(buf.get(sym) or [])[-n:]
        if not bars:
            logger.warning("IBKR realtime bars empty for %s", sym)
            return {"error": "no IBKR realtime bars", "source": "ibkr", "symbol": sym}
        return {
            "symbol": sym,
            "bars": bars,
            "source": "ibkr",
            "freshness": ibkr_bar_freshness("5s"),
            "resolution": "5s",
            "requested_resolution": req_res,
            "use": "live_5s_not_hist",
        }
