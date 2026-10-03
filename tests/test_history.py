import unittest
from datetime import datetime, timezone

from study_companion.history import get_comparison


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def record(seconds, answers, name="Deck"):
    return {"name": name, "seconds": seconds, "answers": answers}


class HistoryTests(unittest.TestCase):
    def test_yesterday_is_exact_and_comparable(self):
        records = {
            "2026-09-30": {"7": record(600, 30)},
            "2026-09-29": {"7": record(1200, 40)},
            "2026-09-28": {"7": record(600, 40)},
        }
        result = get_comparison(records, 7, NOW)
        self.assertEqual("2026-09-29", result["reference"]["date"])
        self.assertTrue(result["comparable"])
        self.assertAlmostEqual(50.0, result["percent_change"])

    def test_yesterday_does_not_fall_back_to_previous(self):
        records = {
            "2026-09-30": {"7": record(600, 30)},
            "2026-09-28": {"7": record(600, 20)},
        }
        result = get_comparison(records, "7", NOW, "yesterday")
        self.assertIsNone(result["reference"])
        self.assertEqual("reference_missing", result["reason"])

    def test_previous_uses_most_recent_recorded_date(self):
        records = {
            "2026-09-30": {"7": record(600, 30)},
            "2026-09-27": {"7": record(600, 20)},
            "2026-09-28": {"7": record(60, 1)},
        }
        result = get_comparison(records, "7", NOW, "previous")
        self.assertEqual("2026-09-28", result["reference"]["date"])
        self.assertFalse(result["comparable"])
        self.assertEqual("reference_below_threshold", result["reason"])

    def test_best_uses_valid_last_30_days_and_latest_tie(self):
        records = {
            "2026-09-30": {"7": record(600, 30)},
            "2026-09-29": {"7": record(600, 30)},
            "2026-09-28": {"7": record(1200, 60)},
            "2026-09-27": {"7": record(300, 50)},
            "2026-08-31": {"7": record(600, 20)},
            "2026-08-30": {"7": record(600, 200)},
        }
        result = get_comparison(records, "7", NOW, "best")
        self.assertEqual("2026-09-29", result["reference"]["date"])
        self.assertAlmostEqual(0.0, result["percent_change"])

    def test_today_requires_five_minutes_and_ten_answers(self):
        records = {
            "2026-09-30": {"7": record(299, 10)},
            "2026-09-29": {"7": record(600, 20)},
        }
        result = get_comparison(records, "7", NOW)
        self.assertFalse(result["today"]["valid"])
        self.assertEqual("today_below_threshold", result["reason"])
        self.assertIsNone(result["percent_change"])

    def test_historical_sample_requires_ten_minutes_and_twenty_answers(self):
        records = {
            "2026-09-30": {"7": record(300, 10)},
            "2026-09-29": {"7": record(599, 20)},
        }
        result = get_comparison(records, "7", NOW)
        self.assertFalse(result["reference"]["valid"])
        self.assertEqual("reference_below_threshold", result["reason"])

    def test_missing_deck_never_falls_back_to_daily_total(self):
        records = {
            "2026-09-30": {"other": record(600, 30)},
            "2026-09-29": {"other": record(600, 20)},
        }
        result = get_comparison(records, "7", NOW)
        self.assertIsNone(result["today"])
        self.assertIsNone(result["reference"])
        self.assertEqual("reference_missing", result["reason"])

    def test_future_and_malformed_dates_are_ignored(self):
        records = {
            "2026-09-30": {"7": record(600, 30)},
            "2026-10-01": {"7": record(600, 100)},
            "not-a-date": {"7": record(600, 100)},
            "2026-09-25": {"7": record(600, 20)},
        }
        result = get_comparison(records, "7", NOW, "best")
        self.assertEqual("2026-09-25", result["reference"]["date"])

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            get_comparison({}, "7", NOW, "average")


if __name__ == "__main__":
    unittest.main()
