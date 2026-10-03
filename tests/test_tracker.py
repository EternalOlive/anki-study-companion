import unittest
from datetime import datetime, timedelta

from study_companion.tracker import (
    StudyTracker,
    TIMEZONE,
    answers_per_minute,
    compare_card_pace,
    pace_density,
)


def moment(hour=10, minute=0, second=0):
    return datetime(2026, 9, 30, hour, minute, second, tzinfo=TIMEZONE)


class StudyTrackerTest(unittest.TestCase):
    def test_review_input_resumes_without_counting_time_away(self):
        tracker = StudyTracker()
        tracker.enter_review(moment())
        tracker.pause(moment(second=10))
        tracker.input(moment(minute=5))
        tracker.tick(moment(minute=5, second=20))
        self.assertEqual(tracker.status, "studying")
        self.assertEqual(tracker.today(moment()), {"seconds": 30, "answers": 0})

    def test_input_after_idle_starts_a_new_interval(self):
        tracker = StudyTracker()
        tracker.enter_review(moment())
        tracker.input(moment(minute=5))
        tracker.tick(moment(minute=5, second=10))
        self.assertEqual(tracker.today(moment())["seconds"], 70)

    def test_input_outside_review_does_not_start_timer(self):
        tracker = StudyTracker()
        tracker.input(moment())
        tracker.tick(moment(second=30))
        self.assertEqual(tracker.status, "stopped")
        self.assertEqual(tracker.today(moment())["seconds"], 0)

    def test_answers_per_minute_keeps_fractional_pace(self):
        self.assertAlmostEqual(answers_per_minute(1500, 42), 1.68)
        self.assertIsNone(answers_per_minute(0, 42))

    def test_pace_density_uses_my_pace_as_the_middle_baseline(self):
        self.assertEqual(pace_density(2.0, 2.0), "▦")
        self.assertEqual(pace_density(1.7, 2.0), "▤")
        self.assertEqual(pace_density(2.3, 2.0), "▩")
        self.assertEqual(pace_density(None, 2.0), "□")

    def test_records_time_and_answers_for_the_current_deck(self):
        tracker = StudyTracker()
        tracker.set_deck("10", "생물", moment())
        tracker.enter_review(moment())
        tracker.answer(moment(second=10))
        tracker.tick(moment(second=30))
        self.assertEqual(
            tracker.today_deck(moment()),
            {"name": "생물", "seconds": 30, "answers": 1},
        )

    def test_switching_decks_splits_active_time(self):
        tracker = StudyTracker()
        tracker.set_deck("10", "생물", moment())
        tracker.enter_review(moment())
        tracker.set_deck("20", "화학", moment(second=20))
        tracker.tick(moment(second=40))
        self.assertEqual(tracker.deck_records["2026-09-30"]["10"]["seconds"], 20)
        self.assertEqual(tracker.deck_records["2026-09-30"]["20"]["seconds"], 20)

    def test_card_pace_compares_equal_active_time(self):
        comparison = compare_card_pace(600, 8, 1500, 42)
        self.assertEqual(comparison, {"shared_seconds": 600, "card_gap": 9})

    def test_card_pace_requires_both_people_to_have_studied(self):
        self.assertIsNone(compare_card_pace(0, 0, 1500, 42))

    def test_idle_pauses_at_one_minute_and_answer_resumes_without_backfill(self):
        tracker = StudyTracker()
        tracker.enter_review(moment())
        tracker.tick(moment(minute=2))
        self.assertEqual(tracker.status, "paused")
        self.assertEqual(tracker.today(moment())["seconds"], 60)
        tracker.answer(moment(minute=3))
        tracker.tick(moment(minute=3, second=20))
        self.assertEqual(tracker.today(moment()), {"seconds": 80, "answers": 1})

    def test_repeated_answers_each_count_once(self):
        tracker = StudyTracker()
        tracker.enter_review(moment())
        tracker.answer(moment(second=10))
        tracker.answer(moment(second=20))
        self.assertEqual(tracker.today(moment())["answers"], 2)

    def test_time_splits_at_room_four_am_boundary(self):
        tracker = StudyTracker()
        start = datetime(2026, 9, 30, 3, 59, 50, tzinfo=TIMEZONE)
        tracker.enter_review(start)
        tracker.tick(start + timedelta(seconds=20))
        self.assertEqual(tracker.records["2026-09-29"]["seconds"], 10)
        self.assertEqual(tracker.records["2026-09-30"]["seconds"], 10)

    def test_timezone_namespaces_restore_each_rooms_live_counters(self):
        tracker = StudyTracker()
        tracker._record("2026-09-30")["answers"] = 3
        tracker.set_time_zone("UTC")
        tracker._record("2026-09-30")["answers"] = 7
        tracker.set_time_zone("Asia/Seoul")
        self.assertEqual(tracker.records["2026-09-30"]["answers"], 3)
        tracker.set_time_zone("UTC")
        self.assertEqual(tracker.records["2026-09-30"]["answers"], 7)


if __name__ == "__main__":
    unittest.main()
