import copy
import unittest

from study_companion.outbox import SyncOutbox


def snapshot(**changes):
    row = {
        "user_id": "user-a",
        "group_id": "room-a",
        "device_id": "device-a",
        "study_day": "2026-10-03",
        "ledger_id": "user-a|room-a",
        "revision": 1,
        "active_seconds": 60,
        "answer_count": 5,
        "time_goal_minutes": 60,
        "card_goal": 100,
        "status": "paused",
    }
    row.update(changes)
    return row


class OfflineOutboxTests(unittest.TestCase):
    def test_retry_survives_restart_with_the_exact_revision(self):
        state = {}
        first = SyncOutbox(state)
        first.enqueue(snapshot(revision=7, active_seconds=120))

        restored = SyncOutbox(copy.deepcopy(state))
        pending = restored.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["revision"], 7)
        self.assertEqual(pending[0]["active_seconds"], 120)

    def test_new_cumulative_snapshot_replaces_older_retry(self):
        outbox = SyncOutbox({})
        outbox.enqueue(snapshot(revision=2, active_seconds=60))
        outbox.enqueue(snapshot(revision=3, active_seconds=90))
        outbox.enqueue(snapshot(revision=2, active_seconds=30))

        pending = outbox.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["revision"], 3)
        self.assertEqual(pending[0]["active_seconds"], 90)

    def test_acknowledgement_only_removes_the_exact_revision(self):
        outbox = SyncOutbox({})
        old = snapshot(revision=2)
        current = snapshot(revision=3, active_seconds=90)
        outbox.enqueue(current)

        self.assertFalse(outbox.acknowledge(old, 2))
        self.assertEqual(len(outbox.state["entries"]), 1)
        self.assertTrue(outbox.acknowledge(current, 3))
        self.assertEqual(outbox.state["entries"], {})

    def test_account_and_room_queues_are_isolated(self):
        outbox = SyncOutbox({})
        outbox.enqueue(snapshot())
        outbox.enqueue(snapshot(user_id="user-b", revision=4))
        outbox.enqueue(snapshot(group_id="room-b", revision=5))

        pending = outbox.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )
        self.assertEqual([row["revision"] for row in pending], [1])

    def test_newest_day_is_sent_before_failed_history(self):
        outbox = SyncOutbox({})
        outbox.enqueue(snapshot(study_day="2026-10-02", revision=9))
        outbox.enqueue(snapshot(study_day="2026-10-04", revision=1))
        outbox.enqueue(snapshot(study_day="2026-10-03", revision=4))

        days = [
            row["study_day"]
            for row in outbox.pending(
                user_id="user-a", group_id="room-a", device_id="device-a"
            )
        ]
        self.assertEqual(days, ["2026-10-04", "2026-10-03", "2026-10-02"])

    def test_leaving_one_room_discards_only_that_rooms_queue(self):
        outbox = SyncOutbox({})
        outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-03",
        )
        outbox.enqueue(snapshot())
        outbox.enqueue(snapshot(group_id="room-b", revision=2))
        outbox.enqueue(snapshot(user_id="user-b", revision=3))

        self.assertEqual(outbox.discard_room("user-a", "room-a"), 1)
        self.assertIsNone(outbox.route("device-a"))
        self.assertEqual(len(outbox.state["entries"]), 2)

    def test_copied_installation_does_not_replay_another_devices_queue(self):
        state = {"installation_id": "old-device"}
        outbox = SyncOutbox(state)
        outbox.enqueue(snapshot())
        outbox.bind_device("new-device")

        self.assertEqual(outbox.state["entries"], {})
        self.assertEqual(outbox.state["installation_id"], "new-device")

    def test_legacy_ledger_owner_survives_leaving_and_cannot_move_rooms(self):
        outbox = SyncOutbox({})
        self.assertTrue(outbox.claim_legacy_ledger(
            user_id="user-a", group_id="room-a", device_id="device-a"
        ))
        outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-03",
        )
        outbox.discard_room("user-a", "room-a")

        self.assertFalse(outbox.claim_legacy_ledger(
            user_id="user-a", group_id="room-b", device_id="device-a"
        ))
        self.assertTrue(outbox.claim_legacy_ledger(
            user_id="user-a", group_id="room-a", device_id="device-a"
        ))

    def test_leave_and_rejoin_same_room_gets_a_new_route_epoch(self):
        outbox = SyncOutbox({})
        outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-03",
        )
        first = outbox.route_epoch("device-a")
        outbox.discard_room("user-a", "room-a")
        outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-03",
        )
        self.assertGreater(outbox.route_epoch("device-a"), first)


if __name__ == "__main__":
    unittest.main()
