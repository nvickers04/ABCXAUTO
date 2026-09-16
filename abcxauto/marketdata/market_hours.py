"""NYSE session clock — holidays, early closes, pre/RTH/post edges.

One implementation. ``park_clock`` and ``opportunity_scan`` call these
helpers. Regular hours come from exchange_calendars; premarket (04:00)
and postmarket (to 20:00 ET) are the same overlays the desk already uses.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PREMARKET_START = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
POSTMARKET_END = time(20, 0)
DEFAULT_EARLY_CLOSE = time(13, 0)

_calendar = None
_calendar_failed = False
_market_hours_provider: MarketHoursProvider | None = None


def _load_market_hours_config() -> dict:
    """Return empty dict — market hours use exchange_calendars defaults."""
    return {}


def as_eastern(dt: datetime | date | None = None) -> datetime:
    """Wall clock in America/New_York. Naive datetimes are treated as ET."""
    if isinstance(dt, datetime):
        clock = dt
    elif isinstance(dt, date):
        clock = datetime(dt.year, dt.month, dt.day, tzinfo=ET)
    else:
        clock = datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=ET)
    return clock.astimezone(ET)


def nyse_calendar():
    """Cached NYSE calendar, or None when exchange_calendars cannot load."""
    global _calendar, _calendar_failed
    if _calendar is not None or _calendar_failed:
        return _calendar
    try:
        import exchange_calendars as ecals

        _calendar = ecals.get_calendar("NYSE")
        logger.info("Initialized market hours calendar NYSE")
    except Exception:
        logger.exception("Failed to load NYSE exchange calendar")
        _calendar_failed = True
        _calendar = None
    return _calendar


def is_trading_day(dt: datetime | date | None = None) -> bool:
    """True on a NYSE session day (holidays off). Weekday fallback if calendar misses."""
    et = as_eastern(dt)
    day = et.date()
    cal = nyse_calendar()
    if cal is None:
        return day.weekday() < 5
    try:
        return bool(cal.is_session(day))
    except Exception:
        logger.debug("is_session failed for %s", day, exc_info=True)
        return day.weekday() < 5


def regular_close_time(dt: datetime | date | None = None) -> time:
    """RTH close in ET, including published early closes (typically 13:00)."""
    et = as_eastern(dt)
    if not is_trading_day(et):
        return REGULAR_CLOSE
    cal = nyse_calendar()
    if cal is None:
        return REGULAR_CLOSE
    try:
        close = cal.session_close(et.date())
        close_et = close.tz_convert("America/New_York")
        return time(int(close_et.hour), int(close_et.minute))
    except Exception:
        logger.debug("session_close failed for %s", et.date(), exc_info=True)
        return REGULAR_CLOSE


def is_early_close(dt: datetime | date | None = None) -> bool:
    return regular_close_time(dt) < REGULAR_CLOSE


def rth_minute_bounds(dt: datetime | date | None = None) -> tuple[int, int]:
    """Inclusive-start exclusive-end RTH minutes past midnight ET."""
    close = regular_close_time(dt)
    start = REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute
    end = close.hour * 60 + close.minute
    return start, end


def session_of(dt: datetime | date | None = None) -> str:
    """``premarket`` / ``regular`` / ``postmarket`` / ``closed``."""
    et = as_eastern(dt)
    if not is_trading_day(et):
        return "closed"
    now_t = et.time()
    close = regular_close_time(et)
    if PREMARKET_START <= now_t < REGULAR_OPEN:
        return "premarket"
    if REGULAR_OPEN <= now_t < close:
        return "regular"
    if close <= now_t < POSTMARKET_END:
        return "postmarket"
    return "closed"


def rth_now(*, now: datetime | None = None) -> bool:
    """True during the NYSE regular session (holidays and early closes)."""
    return session_of(now) == "regular"


def minutes_to_rth_open(*, now: datetime | None = None) -> float | None:
    """Minutes to today's 09:30 ET. None when already open or not a session day."""
    et = as_eastern(now)
    if not is_trading_day(et):
        return None
    bell = et.replace(hour=9, minute=30, second=0, microsecond=0)
    if et >= bell:
        return None
    return (bell - et).total_seconds() / 60.0


def next_premarket_open(dt: datetime | date | None = None) -> datetime | None:
    """Next 04:00 ET premarket open (today, if still before 04:00 on a session)."""
    et = as_eastern(dt)
    if is_trading_day(et) and et.time() < PREMARKET_START:
        return datetime.combine(et.date(), PREMARKET_START, tzinfo=ET)
    cal = nyse_calendar()
    nxt: date | None = None
    if cal is not None:
        try:
            nxt = cal.date_to_session(et.date() + timedelta(days=1), direction="next")
            if hasattr(nxt, "date"):
                nxt = nxt.date()
        except Exception:
            logger.debug("date_to_session failed after %s", et.date(), exc_info=True)
            nxt = None
    if nxt is None:
        day = et.date() + timedelta(days=1)
        for _ in range(10):
            probe = datetime(day.year, day.month, day.day, tzinfo=ET)
            if is_trading_day(probe):
                nxt = day
                break
            day += timedelta(days=1)
    if nxt is None:
        return None
    return datetime.combine(nxt, PREMARKET_START, tzinfo=ET)


class MarketSession(Enum):
    """Market trading session types."""

    CLOSED = "closed"
    PREMARKET = "premarket"
    REGULAR = "regular"
    POSTMARKET = "postmarket"


class MarketHoursProvider:
    """Session labels + next-transition facts. Clock math lives in the module."""

    def __init__(self, exchange: str = "NYSE"):
        mh_cfg = _load_market_hours_config()
        self.exchange = mh_cfg.get("exchange", exchange)
        self.calendar = nyse_calendar() if self.exchange == "NYSE" else None
        if self.calendar is None and self.exchange != "NYSE":
            try:
                import exchange_calendars as ecals

                self.calendar = ecals.get_calendar(self.exchange)
            except Exception:
                logger.exception("Failed to load exchange calendar for %s", self.exchange)
                self.calendar = None
        self.premarket_start = PREMARKET_START
        self.regular_open = REGULAR_OPEN
        self.regular_close = REGULAR_CLOSE
        self.postmarket_end = POSTMARKET_END
        self._early_close_time = DEFAULT_EARLY_CLOSE
        self._et_tz = ET

    def _to_eastern(self, dt: datetime) -> datetime:
        return as_eastern(dt)

    def _effective_close(self, et_time: datetime) -> time:
        return regular_close_time(et_time)

    def get_current_session(self, dt: datetime | None = None) -> MarketSession:
        return MarketSession(session_of(dt))

    def is_trading_day(self, dt: datetime | None = None) -> bool:
        return is_trading_day(dt)

    def get_session_info(self, dt: datetime | None = None) -> dict[str, Any]:
        if dt is None:
            dt = datetime.now(timezone.utc)
        session = self.get_current_session(dt)
        et_time = as_eastern(dt)
        close = regular_close_time(et_time)
        info: dict[str, Any] = {
            "session": session.value,
            "is_trading_day": is_trading_day(et_time),
            "current_time_et": et_time.strftime("%H:%M:%S"),
            "current_date": et_time.date().isoformat(),
            "exchange": self.exchange,
        }
        if is_early_close(et_time):
            info["early_close"] = True
            info["close_time"] = close.strftime("%H:%M")

        now_minutes = et_time.hour * 60 + et_time.minute
        open_minutes = REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute
        close_minutes = close.hour * 60 + close.minute
        if session == MarketSession.PREMARKET:
            info["minutes_to_open"] = open_minutes - now_minutes
        elif session == MarketSession.REGULAR:
            info["minutes_to_close"] = close_minutes - now_minutes

        if session == MarketSession.PREMARKET:
            next_open = datetime.combine(et_time.date(), REGULAR_OPEN)
            info["next_transition"] = "regular_open"
            info["next_transition_time"] = next_open.isoformat()
        elif session == MarketSession.REGULAR:
            next_close = datetime.combine(et_time.date(), close)
            info["next_transition"] = "regular_close"
            info["next_transition_time"] = next_close.isoformat()
        elif session == MarketSession.POSTMARKET:
            next_end = datetime.combine(et_time.date(), POSTMARKET_END)
            info["next_transition"] = "market_closed"
            info["next_transition_time"] = next_end.isoformat()
        else:
            nxt = next_premarket_open(et_time)
            info["next_transition"] = "premarket_open"
            info["next_transition_time"] = nxt.isoformat() if nxt else None
        return info


def get_market_hours_provider(exchange: str = "NYSE") -> MarketHoursProvider:
    """Get or create global market hours provider instance."""
    global _market_hours_provider
    if _market_hours_provider is None or _market_hours_provider.exchange != exchange:
        _market_hours_provider = MarketHoursProvider(exchange)
    return _market_hours_provider


def get_session_info(dt: datetime | None = None) -> dict[str, Any]:
    """Get comprehensive session information."""
    return get_market_hours_provider().get_session_info(dt)
