import copy
import unittest

from study_companion.record_status import (
    LOCAL_READ,
    LOCAL_SAVE,
    MEMBERS,
    UPLOAD,
    RecordStatusLedger,
    merge_pending_summaries,
)
from study_companion.reviews import ReviewHistory


DAY = "2026-10-04"


def review(review_id, *, time_ms=1000):
    return (review_id, review_id + 100, time_ms, 3, 1)


class RecordStatusTests(unittest.TestCase):
    def test_success_times_survive_restart_and_remote_scopes_do_not_leak(self):
        state = {}
        ledger = RecordStatusLedger(state, clock=lambda: 100)
        ledger.mark_success(LOCAL_READ)
        ledger.mark_success(UPLOAD, user_id="user-a", group_id="room-a", at=120)

        restored = RecordStatusLedger(copy.deepcopy(state), clock=lambda: 200)
        room_a = restored.snapshot(user_id="user-a", group_id="room-a")
        room_b = restored.snapshot(user_id="user-a", group_id="room-b")

        self.assertEqual(room_a["local_read_at"], 100)
        self.assertEqual(room_a["upload_at"], 120)
        self.assertIsNone(room_b["upload_at"])

    def test_transient_errors_clear_on_stage_success_and_are_not_persisted(self):
        state = {}
        ledger = RecordStatusLedger(state, clock=lambda: 100)
        ledger.set_error(UPLOAD, "offline", user_id="user-a", group_id="room-a")
        self.assertEqual(
            ledger.snapshot(user_id="user-a", group_id="room-a")["primary_issue"],
            UPLOAD,
        )
        self.assertNotIn("errors", state)

        ledger.mark_success(UPLOAD, user_id="user-a", group_id="room-a")
        self.assertIsNone(
            ledger.snapshot(user_id="user-a", group_id="room-a")["primary_issue"]
        )

    def test_issue_priority_and_sixty_second_pending_threshold(self):
        ledger = RecordStatusLedger({}, clock=lambda: 100)
        ledger.set_error(
            MEMBERS, "refresh", user_id="user-a", group_id="room-a"
        )
        ledger.set_error(LOCAL_READ, "read")
        ledger.set_error(LOCAL_SAVE, "save")
        snapshot = ledger.snapshot(
            user_id="user-a", group_id="room-a",
            pending={"count": 1, "oldest_age_seconds": 500},
        )
        self.assertEqual(snapshot["primary_issue"], LOCAL_SAVE)

        clean = RecordStatusLedger({}, clock=lambda: 100)
        self.assertIsNone(clean.snapshot(
            user_id="user-a", group_id="room-a",
            pending={"count": 1, "oldest_age_seconds": 59},
        )["primary_issue"])
        self.assertEqual(clean.snapshot(
            user_id="user-a", group_id="room-a",
            pending={"count": 1, "oldest_age_seconds": 60},
        )["primary_issue"], "pending")

    def test_pending_summaries_combine_device_and_review_queues(self):
        combined = merge_pending_summaries(
            {"count": 2, "oldest_queued_at": 100, "oldest_age_seconds": 80},
            {"count": 1, "oldest_queued_at": 120, "oldest_age_seconds": 60},
        )

        self.assertEqual(combined, {
            "count": 3,
            "oldest_queued_at": 100,
            "oldest_age_seconds": 80,
        })

    def test_review_pending_age_survives_restart_and_clears_after_ack(self):
        state = {}
        history = ReviewHistory(state, now_ms=lambda: 100_000, clock=lambda: 100)
        history.observe("collection", DAY, [review(1)])

        first = history.pending_summary(
            "user", "room", since_day=DAY, now=175
        )
        restored = ReviewHistory(
            copy.deepcopy(state), now_ms=lambda: 200_000, clock=lambda: 200
        )
        retry = restored.pending_summary(
            "user", "room", since_day=DAY, now=200
        )

        self.assertEqual(first["count"], 1)
        self.assertEqual(first["oldest_queued_at"], 100)
        self.assertEqual(first["oldest_age_seconds"], 75)
        self.assertEqual(retry["oldest_queued_at"], 100)

        restored.acknowledge("user", "room", restored.pending("user", "room")[0])
        self.assertEqual(
            restored.pending_summary("user", "room", now=220),
            {"count": 0, "oldest_queued_at": None, "oldest_age_seconds": None},
        )

    def test_late_review_ack_preserves_age_when_newer_change_remains(self):
        history = ReviewHistory({}, now_ms=lambda: 100_000, clock=lambda: 100)
        history.observe("collection", DAY, [review(1, time_ms=1000)])
        in_flight = history.pending("user", "room")[0]
        history.observe(
            "collection", DAY,
            [review(1, time_ms=1200), review(2, time_ms=500)],
        )

        history.acknowledge("user", "room", in_flight)
        summary = history.pending_summary("user", "room", now=180)

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["oldest_queued_at"], 100)
        self.assertEqual(summary["oldest_age_seconds"], 80)


if __name__ == "__main__":
    unittest.main()
