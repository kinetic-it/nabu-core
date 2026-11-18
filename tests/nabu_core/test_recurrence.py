import unittest
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from nabu_core.recurrence import InvalidLocalOccurrence, next_weekly


class RecurrenceTests(unittest.TestCase):
    zone = ZoneInfo("Europe/Berlin")

    def test_monday_after_now(self):
        now = datetime(2026, 3, 18, 12, tzinfo=self.zone)
        self.assertEqual(
            next_weekly(now, time(8), {0}, self.zone), datetime(2026, 3, 23, 7, tzinfo=UTC)
        )

    def test_same_day_next_week_if_already_due(self):
        now = datetime(2026, 3, 23, 8, tzinfo=self.zone)
        self.assertEqual(
            next_weekly(now, time(8), {0}, self.zone), datetime(2026, 3, 30, 6, tzinfo=UTC)
        )

    def test_dst_spring_gap_is_explicit_error(self):
        now = datetime(2026, 3, 28, 12, tzinfo=self.zone)
        with self.assertRaisesRegex(InvalidLocalOccurrence, "nonexistent"):
            next_weekly(now, time(2, 30), {6}, self.zone)

    def test_dst_fall_fold_is_explicit_error(self):
        now = datetime(2026, 10, 24, 12, tzinfo=self.zone)
        with self.assertRaisesRegex(InvalidLocalOccurrence, "ambiguous"):
            next_weekly(now, time(2, 30), {6}, self.zone)

    def test_invalid_weekdays_are_rejected(self):
        now = datetime(2026, 3, 18, tzinfo=self.zone)
        for days in (set(), {7}, {-1}, {True}):
            with self.assertRaises(ValueError):
                next_weekly(now, time(8), days, self.zone)

    def test_naive_reference_rejected(self):
        with self.assertRaises(ValueError):
            next_weekly(datetime(2026, 3, 18), time(8), {0}, self.zone)

    def test_fold_not_skipped_when_second_occurrence_is_future(self):
        now = datetime(2026, 10, 25, 2, 30, tzinfo=self.zone, fold=0)
        with self.assertRaisesRegex(InvalidLocalOccurrence, "ambiguous"):
            next_weekly(now, time(2, 15), {6}, self.zone)
