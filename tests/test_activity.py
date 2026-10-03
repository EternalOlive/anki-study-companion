"""Tests for local recent-activity aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import TestCase

from study_companion.activity import MAX_REVIEW_TIME_MS, weekly_activity


KST = timezone(timedelta(hours=9))


def row(at: datetime, card_id: int, time_ms=1000, ease=3, review_type=1):
    return (int(at.timestamp() * 1000), card_id, time_ms, ease, review_type)


class WeeklyActivityTests(TestCase):
    def test_returns_seven_known_zero_days_for_complete_empty_query(self):
        current = datetime(2026, 10, 4, 12, 30, tzinfo=KST)

        result = weekly_activity([], current)

        self.assertEqual(
            [item["day"] for item in result["days"]],
            [
                "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01",
                "2026-10-02", "2026-10-03", "2026-10-04",
            ],
        )
        self.assertEqual(result["answers"], 0)
        self.assertEqual(result["seconds"], 0)
        self.assertEqual(result["active_days"], 0)
        self.assertEqual(result["previous_answers"], 0)

    def test_deduplicates_answers_and_excludes_manual_or_future_rows(self):
        current = datetime(2026, 10, 4, 12, 30, tzinfo=KST)
        answer = row(datetime(2026, 10, 4, 8, 0, tzinfo=KST), 1, 2500)
        rows = [
            answer,
            answer,
            row(datetime(2026, 10, 3, 8, 0, tzinfo=KST), 2, -50),
            row(datetime(2026, 10, 2, 8, 0, tzinfo=KST), 3, 1000, ease=0),
            row(datetime(2026, 10, 1, 8, 0, tzinfo=KST), 4, 1000, review_type=4),
            row(datetime(2026, 10, 4, 13, 0, tzinfo=KST), 5),
        ]

        result = weekly_activity(rows, current)

        self.assertEqual(result["answers"], 2)
        self.assertEqual(result["seconds"], 2.5)
        self.assertEqual(result["active_days"], 2)

    def test_caps_single_review_time_to_server_supported_limit(self):
        current = datetime(2026, 10, 4, 12, 30, tzinfo=KST)
        result = weekly_activity(
            [row(current - timedelta(minutes=1), 1, MAX_REVIEW_TIME_MS + 5000)],
            current,
        )
        self.assertEqual(result["seconds"], MAX_REVIEW_TIME_MS / 1000)

    def test_millisecond_totals_are_converted_only_after_integer_sum(self):
        current = datetime(2026, 10, 4, 12, 30, tzinfo=KST)
        rows = [
            row(
                datetime(2026, 10, 4, 8, 0, tzinfo=KST)
                + timedelta(milliseconds=index),
                index,
                30,
            )
            for index in range(1, 101)
        ]
        rows.extend([
            row(datetime(2026, 10, 3, 8, 0, tzinfo=KST), 201, 30),
            row(datetime(2026, 10, 2, 8, 0, tzinfo=KST), 202, 30),
        ])

        result = weekly_activity(rows, current)

        self.assertEqual(result["days"][-1]["seconds"], 3.0)
        self.assertEqual(result["seconds"], 3.06)

    def test_previous_period_uses_same_elapsed_time_on_its_last_day(self):
        current = datetime(2026, 10, 4, 12, 30, tzinfo=KST)
        rows = [
            row(datetime(2026, 9, 21, 23, 0, tzinfo=KST), 1, 1000),
            row(datetime(2026, 9, 27, 12, 29, tzinfo=KST), 2, 2000),
            row(datetime(2026, 9, 27, 12, 31, tzinfo=KST), 3, 4000),
            row(datetime(2026, 9, 28, 8, 0, tzinfo=KST), 4, 8000),
        ]

        result = weekly_activity(rows, current)

        self.assertEqual(result["previous_answers"], 2)
        self.assertEqual(result["previous_seconds"], 3.0)
        self.assertEqual(result["answers"], 1)

    def test_leap_day_is_included_in_order(self):
        current = datetime(2028, 3, 3, 9, 0, tzinfo=KST)
        result = weekly_activity(
            [row(datetime(2028, 2, 29, 8, 0, tzinfo=KST), 1)], current
        )
        self.assertEqual(
            [item["day"] for item in result["days"]],
            [
                "2028-02-26", "2028-02-27", "2028-02-28", "2028-02-29",
                "2028-03-01", "2028-03-02", "2028-03-03",
            ],
        )
        self.assertEqual(result["days"][3]["answers"], 1)

    def test_requires_timezone_aware_cutoff(self):
        with self.assertRaises(ValueError):
            weekly_activity([], datetime(2026, 10, 4, 12, 30))
