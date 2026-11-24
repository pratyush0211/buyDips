"""Which market a symbol trades on, and when that market is live.

An alert is only valid while its own market is open (plus a short window after the close, to catch
the final price), and only for *that market's current trading day*. That keeps a closed market, a
weekend, a holiday or a stale Yahoo bar from ever producing an alert."""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import catalog

# name -> (timezone, open, close), all in the market's own local time. Only India and the US are supported.
MARKETS = {
    "India": ("Asia/Kolkata", time(9, 15), time(15, 30)),
    "US": ("America/New_York", time(9, 30), time(16, 0)),
}
# Full-day closures that fall on weekdays (weekends are handled separately). Refresh every December.
_HOLIDAY_DATA = {k: v for k, v in json.loads((Path(__file__).parent / "holidays.json").read_text()).items()
                 if not k.startswith("_")}
HOLIDAYS = {m: set(days) for m, days in _HOLIDAY_DATA.items()}
HOLIDAY_NAMES = _HOLIDAY_DATA  # market -> {"YYYY-MM-DD": "Holiday name"}
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
    """True on a weekday that isn't a listed holiday, between the market's open and (close + grace), local time."""
    market = market_of(symbol)
    if market is None:
        return False
    here = local_now(market, now)
    if here.weekday() >= 5 or here.date().isoformat() in HOLIDAYS.get(market, ()):
        return False
    _, opens, closes = MARKETS[market]
    start = here.replace(hour=opens.hour, minute=opens.minute, second=0, microsecond=0)
    end = here.replace(hour=closes.hour, minute=closes.minute, second=0, microsecond=0) + GRACE
    return start <= here <= end


def is_fresh(symbol: str, session: str, now: datetime) -> bool:
    """True only if `session` (YYYY-MM-DD) is today's trading day in the symbol's own market."""
    today = local_today(symbol, now)
    return today is not None and session == today.isoformat()


def missing_holiday_year(now: datetime) -> Optional[int]:
    """The current year if the holiday list has no dates for it (so someone should refresh it), else None."""
    year = str(now.year)
    return None if all(any(d.startswith(year) for d in HOLIDAYS[m]) for m in HOLIDAYS) else now.year


# ---------- what the Markets tab shows ----------

LABELS = {"India": "India (NSE)", "US": "United States (NYSE, Nasdaq)"}
IST = ZoneInfo("Asia/Kolkata")


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").lower().replace(" ", "\u202f")


def _trading_day(market: str, d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS.get(market, ())


def _open_dt(market: str, d: date) -> datetime:
    tz, opens, _ = MARKETS[market]
    return datetime.combine(d, opens, tzinfo=ZoneInfo(tz))


def market_status(market: str, now: datetime) -> dict:
    """Everything the Markets tab needs to describe one market right now."""
    tz, opens, closes = MARKETS[market]
    here = local_now(market, now)
    today = here.date()
    trading = _trading_day(market, today)
    is_open = trading and opens <= here.time() < closes
    if is_open:
        reason = "Trading now"
    elif today.isoformat() in HOLIDAYS.get(market, ()):
        reason = "Closed today: " + HOLIDAY_NAMES[market][today.isoformat()]
    elif today.weekday() >= 5:
        reason = "Closed for the weekend"
    elif here.time() < opens:
        reason = "Opens later today"
    else:
        reason = "Closed for the day"
    day = today if (trading and here.time() < opens) else today + timedelta(days=1)
    while not _trading_day(market, day):
        day += timedelta(days=1)
    nxt = _open_dt(market, day)
    zone = here.tzname()
    mine = nxt.astimezone(IST)
    return {
        "market": market, "label": LABELS[market], "open": is_open, "reason": reason,
        "local_now": here.strftime("%a %d %b, ") + _clock(here) + " " + zone,
        "hours": f"{_clock(_open_dt(market, today))} \u2013 {_clock(datetime.combine(today, closes, tzinfo=ZoneInfo(tz)))} {zone}",
        "next_open": nxt.strftime("%a %d %b, ") + _clock(nxt) + " " + zone,
        "next_open_ist": None if tz == "Asia/Kolkata" else mine.strftime("%a ") + _clock(mine) + " IST",
    }


def upcoming_holidays(market: str, now: datetime, n: int = 4) -> list:
    today = local_now(market, now).date().isoformat()
    days = sorted(d for d in HOLIDAY_NAMES.get(market, {}) if d >= today)[:n]
    return [{"date": date.fromisoformat(d).strftime("%a %d %b"), "name": HOLIDAY_NAMES[market][d]} for d in days]
