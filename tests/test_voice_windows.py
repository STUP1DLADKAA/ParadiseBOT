"""Fixed-clock tests for calendar voice windows in Asia/Kolkata."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from functions.voice.time_windows import (
    IST,
    ist_date,
    next_ist_midnight,
    period_bounds,
    period_overlap_seconds,
    split_session_by_ist_midnight,
)


class TestISTReportingWindows(unittest.TestCase):
    def test_fixed_clock_uses_ist_date_and_midnight(self):
        before_midnight = datetime(2026, 10, 11, 18, 29, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 10, 11, 18, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(ist_date(before_midnight), date(2026, 10, 11))
        self.assertEqual(ist_date(after_midnight), date(2026, 10, 12))
        self.assertEqual(next_ist_midnight(before_midnight), after_midnight)

    def test_iso_week_is_monday_to_monday(self):
        start, end = period_bounds("week", date(2026, 10, 11))  # Sunday
        self.assertEqual(start, datetime(2026, 10, 5, 0, 0, tzinfo=IST))
        self.assertEqual(end, datetime(2026, 10, 12, 0, 0, tzinfo=IST))

    def test_calendar_month_is_first_to_first(self):
        start, end = period_bounds("month", date(2026, 12, 31))
        self.assertEqual(start, datetime(2026, 12, 1, 0, 0, tzinfo=IST))
        self.assertEqual(end, datetime(2027, 1, 1, 0, 0, tzinfo=IST))

    def test_session_is_split_on_actual_ist_midnight(self):
        started = datetime(2026, 10, 4, 18, 20, tzinfo=timezone.utc)  # Sun 23:50 IST
        ended = datetime(2026, 10, 4, 18, 50, tzinfo=timezone.utc)  # Mon 00:20 IST
        self.assertEqual(
            split_session_by_ist_midnight(started, ended),
            {"2026-10-04": 600, "2026-10-05": 1200},
        )
        self.assertEqual(
            period_overlap_seconds(started, ended, "today", date(2026, 10, 4)),
            600,
        )
        self.assertEqual(
            period_overlap_seconds(started, ended, "today", date(2026, 10, 5)),
            1200,
        )

    def test_fractional_endpoints_preserve_whole_seconds_across_the_split(self):
        started = datetime(2026, 10, 1, 18, 20, 0, 500000, tzinfo=timezone.utc)
        ended = datetime(2026, 10, 1, 18, 50, 0, 250000, tzinfo=timezone.utc)
        chunks = split_session_by_ist_midnight(started, ended)
        self.assertEqual(chunks, {"2026-10-01": 599, "2026-10-02": 1200})
        self.assertEqual(sum(chunks.values()), 1799)
        self.assertEqual(
            period_overlap_seconds(started, ended, "today", date(2026, 10, 1))
            + period_overlap_seconds(started, ended, "today", date(2026, 10, 2)),
            1799,
        )

    def test_session_is_clipped_to_calendar_month_boundary(self):
        started = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
        ended = datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc)  # Nov 1 00:30 IST
        self.assertEqual(
            period_overlap_seconds(started, ended, "month", date(2026, 10, 31)),
            1800,
        )

    def test_live_periods_use_fixed_now_when_target_is_omitted(self):
        now = datetime(2026, 10, 11, 18, 29, 59, tzinfo=timezone.utc)
        self.assertEqual(
            period_overlap_seconds(
                datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc),
                now,
                "today",
                now=now,
            ),
            1799,
        )


if __name__ == "__main__":
    unittest.main()
