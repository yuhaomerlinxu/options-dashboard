"""Unit tests for the NYSE half-day calendar and remaining-time hooks.

No live market data. Times are mocked.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)

import us_market_calendar as cal
import strategyapi as S
import futu_spx as F

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')
SESS = 6.5 * 3600


def et(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


class CalendarDatesTest(unittest.TestCase):
    def test_2026_early_closes(self):
        self.assertTrue(cal.is_early_close(date(2026, 11, 27)))
        self.assertTrue(cal.is_early_close(date(2026, 12, 24)))
        self.assertFalse(cal.is_holiday(date(2026, 11, 27)))
        self.assertFalse(cal.is_holiday(date(2026, 12, 24)))

    def test_2027_dates_match_nyse(self):
        # Day after Thanksgiving is the only 2027 early close.
        self.assertTrue(cal.is_early_close(date(2027, 11, 26)))
        # Christmas Day observed is a full close, not an early close.
        self.assertTrue(cal.is_holiday(date(2027, 12, 24)))
        self.assertFalse(cal.is_early_close(date(2027, 12, 24)))
        # Independence Day observed (Mon Jul 5) is a full close; Jul 2 is regular.
        self.assertTrue(cal.is_holiday(date(2027, 7, 5)))
        self.assertFalse(cal.is_early_close(date(2027, 7, 2)))
        self.assertFalse(cal.is_early_close(date(2027, 7, 5)))

    def test_thanksgiving_is_full_holiday(self):
        self.assertTrue(cal.is_holiday(date(2026, 11, 26)))
        self.assertFalse(cal.is_trading_day(date(2026, 11, 26)))

    def test_next_trading_day_skips_thanksgiving(self):
        # Wednesday after the close -> Friday early close, not Thursday.
        nxt = cal.next_trading_day(date(2026, 11, 25))
        self.assertEqual(nxt, date(2026, 11, 27))
        self.assertEqual(cal.session_close_time(nxt).hour, 13)


class SessionRemainingTest(unittest.TestCase):
    def test_half_day_noon_one_hour_left(self):
        now = et(2026, 11, 27, 12, 0)
        rem, sess, in_rth = cal.session_remaining(now)
        self.assertTrue(in_rth)
        self.assertEqual(sess, SESS)
        self.assertEqual(rem, 3600)
        # Same values through the strategyapi wrapper.
        rem2, sess2, in_rth2 = S._session_remaining(now)
        self.assertEqual((rem2, sess2, in_rth2), (rem, sess, in_rth))
        self.assertFalse(cal.is_half_day_closed(now))

    def test_half_day_after_close_is_closed(self):
        now = et(2026, 11, 27, 13, 30)
        rem, sess, in_rth = S._session_remaining(now)
        self.assertFalse(in_rth)
        self.assertEqual(sess, SESS)
        self.assertEqual(rem, SESS)  # off-hours: full next session remaining
        self.assertTrue(cal.is_half_day_closed(now))
        # At the 13:00 print itself, the session is already closed (no forecast).
        at_close = et(2026, 11, 27, 13, 0)
        self.assertFalse(cal.in_rth(at_close))
        self.assertTrue(cal.is_half_day_closed(at_close))

    def test_normal_day_noon_four_hours_left(self):
        now = et(2026, 11, 24, 12, 0)  # Tuesday, regular session
        rem, sess, in_rth = S._session_remaining(now)
        self.assertTrue(in_rth)
        self.assertEqual(sess, SESS)
        self.assertEqual(rem, 4 * 3600)
        self.assertFalse(cal.is_half_day_closed(now))

    def test_normal_day_after_16_is_closed(self):
        now = et(2026, 11, 24, 16, 30)
        rem, sess, in_rth = S._session_remaining(now)
        self.assertFalse(in_rth)
        self.assertEqual((rem, sess), (SESS, SESS))

    def test_holiday_weekday_not_in_rth(self):
        now = et(2026, 11, 26, 12, 0)  # Thanksgiving
        rem, sess, in_rth = S._session_remaining(now)
        self.assertFalse(in_rth)
        self.assertEqual((rem, sess), (SESS, SESS))


class ExpectedMoveTest(unittest.TestCase):
    def test_half_day_scales_to_13_not_16(self):
        st = {'one_sigma_move': 100.0}
        # 0DTE on the half day, still open: used as-is.
        em, note = F.expected_move_to_close(st, '2026-11-27', now=et(2026, 11, 27, 12, 0))
        self.assertEqual(em, 100.0)
        self.assertIn("today's expiry", note)

        # Later expiry: remaining fraction uses 1 hour, not 4.
        em, note = F.expected_move_to_close(st, '2026-11-30', now=et(2026, 11, 27, 12, 0))
        self.assertAlmostEqual(em, 100.0 * math.sqrt(1 / 6.5), places=6)
        # A regular Tuesday at 12:00 with the next day expiry uses 4 hours.
        em_n, _ = F.expected_move_to_close(st, '2026-11-25', now=et(2026, 11, 24, 12, 0))
        self.assertAlmostEqual(em_n, 100.0 * math.sqrt(4 / 6.5), places=6)

    def test_after_early_close_goes_to_next_session(self):
        st = {'one_sigma_move': 100.0}
        em, note = F.expected_move_to_close(st, '2026-11-27', now=et(2026, 11, 27, 13, 30))
        self.assertEqual(em, 100.0)
        self.assertIn('next session', note)
        # Coming close after Friday 13:30 is Monday 16:00.
        self.assertEqual(
            cal.coming_session_close(et(2026, 11, 27, 13, 30)),
            et(2026, 11, 30, 16, 0),
        )


class HalfDayLogTest(unittest.TestCase):
    def test_log_half_day_closed_record(self):
        sys.path.insert(0, '/workspace/spx_forecast')
        import forecast_extra
        now_bj = et(2026, 11, 27, 13, 30).astimezone(BJ)
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.jsonl', delete=False) as f:
            path = f.name
        try:
            rec = forecast_extra.log_half_day_closed('02:00', now=now_bj, log_path=path)
            self.assertEqual(rec['type'], 'half_day_closed')
            self.assertEqual(rec['slot'], '02:00')
            self.assertEqual(rec['session_date'], '2026-11-27')
            self.assertEqual(rec['close_et'], '13:00')
            with open(path, encoding='utf-8') as fh:
                line = fh.read().strip()
            self.assertEqual(json.loads(line)['type'], 'half_day_closed')
        finally:
            os.unlink(path)

    def test_02_00_bj_slot_is_at_friday_close(self):
        # 2026-11-28 02:00 BJ = 2026-11-27 13:00 ET (half-day close).
        now = datetime(2026, 11, 28, 2, 0, tzinfo=BJ)
        self.assertTrue(cal.is_half_day_closed(now))
        now_03 = datetime(2026, 11, 28, 3, 0, tzinfo=BJ)
        self.assertTrue(cal.is_half_day_closed(now_03))
        # Same clock the day before (Thu Thanksgiving night / Fri morning) is not this half day.
        before = datetime(2026, 11, 27, 1, 0, tzinfo=BJ)  # Thu 12:00 ET, holiday
        self.assertFalse(cal.is_half_day_closed(before))


if __name__ == '__main__':
    unittest.main()
