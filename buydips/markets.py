"""Which market a symbol trades on, and when that market is live.

An alert is only valid while its own market is open (plus a short window after the close, to catch
the final price), and only for *that market's current trading day*. That keeps a closed market, a
weekend, a holiday or a stale Yahoo bar from ever producing an alert."""

from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import catalog

# name -> (timezone, open, close), all in the market's own local time. Only India and the US are supported.
MARKETS = {
    "India": ("Asia/Kolkata", time(9, 15), time(15, 30)),
    "US": ("America/New_York", time(9, 30), time(16, 0)),
}
GRACE = timedelta(minutes=60)  # still alert this long after the close, for the closing price


def market_of(symbol: str) -> Optional[str]:
    """The market a catalog symbol belongs to, or None if we don't know it."""
    item = catalog.BY_SYMBOL.get(symbol)
    group = item["group"] if item else None
    return group if group in MARKETS else None


def local_now(market: str, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(MARKETS[market][0]))


def local_today(symbol: str, now: datetime) -> Optional[date]:
    market = market_of(symbol)
    return local_now(market, now).date() if market else None


def is_live(symbol: str, now: datetime) -> bool:
    """True on a weekday between the market's open and (close + grace), local time."""
    market = market_of(symbol)
    if market is None:
        return False
    here = local_now(market, now)
    if here.weekday() >= 5:
        return False
    _, opens, closes = MARKETS[market]
    start = here.replace(hour=opens.hour, minute=opens.minute, second=0, microsecond=0)
    end = here.replace(hour=closes.hour, minute=closes.minute, second=0, microsecond=0) + GRACE
    return start <= here <= end


def is_fresh(symbol: str, session: str, now: datetime) -> bool:
    """True only if `session` (YYYY-MM-DD) is today's trading day in the symbol's own market."""
    today = local_today(symbol, now)
    return today is not None and session == today.isoformat()
