from datetime import datetime, timedelta, timezone
from unittest import TestCase

from study_companion.study_day import (
    day_bounds,
    quarter_hour_slot,
    split_interval,
    study_day,
)


KST = timezone(timedelta(hours=9))


class StudyDayTests(TestCase):
    def test_day_changes_at_four_in_room_time(self):
        self.assertEqual(
            study_day(datetime(2026, 10, 4, 3, 59, 59, tzinfo=KST)).isoformat(),
            "2026-10-03",
        )
        self.assertEqual(
            study_day(datetime(2026, 10, 4, 4, 0, tzinfo=KST)).isoformat(),
            "2026-10-04",
        )

    def test_bounds_and_slot_use_four_am_origin(self):
        start, end = day_bounds("2026-10-04")
        self.assertEqual(start, datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(quarter_hour_slot(start), 0)
        self.assertEqual(quarter_hour_slot(end - timedelta(minutes=1)), 95)

    def test_interval_is_split_at_room_boundary(self):
        start = datetime(2026, 10, 4, 3, 59, 50, tzinfo=KST)
        self.assertEqual(
            list(split_interval(start, start + timedelta(seconds=20))),
            [("2026-10-03", 10.0), ("2026-10-04", 10.0)],
        )

