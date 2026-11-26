"""NYSE regular-session calendar for the SPX close forecast.

Early-close days end at 13:00 ET; regular sessions end at 16:00 ET.
Full holidays are closed all day. Dates are taken from the NYSE Group
holiday and early-closings calendar (2026–2028):
https://www.nyse.com/trade/hours-calendars/

session_sec used for pin-weight and expected-move annualization stays
the standard 6.5-hour session, even on early-close days. Only
remaining time and the in-RTH / closed switch use the actual close.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

FULL_SESSION_SEC = 6.5 * 3600  # 09:30–16:00 ET
OPEN_TIME = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Full-day closures (observed dates when the holiday falls on a weekend).
# 2028-01-01 is a Saturday; NYSE does not observe New Year's Day that year.
NYSE_HOLIDAYS = frozenset({
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),   # Independence Day observed
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),  # Juneteenth observed
    date(2027, 7, 5),   # Independence Day observed
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # Christmas Day observed (full close, not an early close)
    date(2028, 1, 17),
    date(2028, 2, 21),
    date(2028, 4, 14),
    date(2028, 5, 29),
    date(2028, 6, 19),
    date(2028, 7, 4),
    date(2028, 9, 4),
    date(2028, 11, 23),
    date(2028, 12, 25),
})

# Core session closes at 13:00 ET (1:15 p.m. for eligible options; unused here).
# 2027-12-24 is a full holiday, not an early close. There is no early close
# before Independence Day in 2026 or 2027 (those observed dates are full closes).
NYSE_EARLY_CLOSES = frozenset({
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 11, 26),  # day after Thanksgiving
    date(2028, 7, 3),    # day before Independence Day
    date(2028, 11, 24),  # day after Thanksgiving
})


def as_et(now=None):
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def is_holiday(d):
    return d in NYSE_HOLIDAYS


def is_early_close(d):
    return d in NYSE_EARLY_CLOSES


def is_trading_day(d):
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def session_close_time(d):
    """Clock time of the regular-session close on trading day d, or None."""
    if not is_trading_day(d):
        return None
    return EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE


def session_close_dt(d, tz=ET):
    t = session_close_time(d)
    if t is None:
        return None
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=tz)


def next_trading_day(d):
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_between(start, end):
    """Count trading days in (start, end], matching the prior weekday walk plus holidays."""
    days = 0
    x = start
    while x < end:
        x += timedelta(days=1)
        if is_trading_day(x):
            days += 1
    return max(days, 1) if end > start else 1


def in_rth(now=None):
    now = as_et(now)
    close_t = session_close_dt(now.date())
    if close_t is None:
        return False
    open_t = now.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
    return open_t <= now < close_t


def is_half_day_closed(now=None):
    """True when today is an NYSE early close and *now* is at or after 13:00 ET."""
    now = as_et(now)
    if not is_early_close(now.date()):
        return False
    close_t = session_close_dt(now.date())
    return close_t is not None and now >= close_t


def coming_session_close(now=None):
    """Datetime of the coming regular-session close (today if still open, else next trading day)."""
    now = as_et(now)
    close_t = session_close_dt(now.date())
    if close_t is not None and now < close_t:
        return close_t
    return session_close_dt(next_trading_day(now.date()))


def session_remaining(now=None):
    """Return (remaining_sec, session_sec, in_rth).

    Outside RTH / before open / weekend / holiday: full 6.5h session remaining.
    remaining_sec uses the actual close (13:00 ET on early-close days).
    session_sec is always FULL_SESSION_SEC so weights and EM annualization
    stay on the standard 6.5-hour year.
    """
    now = as_et(now)
    sess = FULL_SESSION_SEC
    close_t = session_close_dt(now.date())
    if close_t is None:
        return sess, sess, False
    open_t = now.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
    if now < open_t or now >= close_t:
        return sess, sess, False
    rem = (close_t - now).total_seconds()
    return max(0.0, rem), sess, True
