import json
import unittest

from study_companion.reviews import MAX_BATCH_ITEMS, ReviewHistory


DAY = "2026-10-04"


def row(review_id, card_id=None, time_ms=1000, ease=3, review_type=1):
    return (review_id, card_id or review_id + 1000, time_ms, ease, review_type)


class ReviewHistoryTests(unittest.TestCase):
    def test_new_room_only_sends_recent_days(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])
        history.observe("collection", "2026-10-03", [(2, 20, 600, 3, 1)])

        pending = history.pending("user", "new-room", since_day="2026-10-03")

        self.assertEqual([batch["target_day"] for batch in pending], ["2026-10-03"])

    def test_old_attempted_day_is_retried_outside_recent_window(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])
        first = history.pending("user", "room")
        self.assertEqual(first[0]["target_day"], "2026-09-01")

        retry = history.pending("user", "room", since_day="2026-10-03")

        self.assertEqual(retry, first)

    def test_new_route_keeps_its_original_sharing_boundary(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", "2026-10-03", [(1, 10, 500, 3, 1)])
        history.pending("user", "room", since_day="2026-10-03")
        history.observe("collection", "2026-10-04", [(2, 20, 600, 3, 1)])

        history.pending("user", "room", since_day="2026-10-04")

        self.assertEqual(
            history.state["routes"]["user"]["room"]["sources"]["collection"]["since_day"],
            "2026-10-03",
        )

    def test_legacy_route_without_boundary_preserves_ambiguous_old_days(self):
        state = {
            "collections": {},
            "routes": {
                "user": {"room": {"sources": {"collection": {"days": {}}}}}
            },
        }
        history = ReviewHistory(state, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])

        pending = history.pending("user", "room", since_day="2026-10-03")

        self.assertEqual([batch["target_day"] for batch in pending], ["2026-09-01"])
        self.assertEqual(
            history.state["routes"]["user"]["room"]["sources"]["collection"]["since_day"],
            "2026-09-01",
        )

    def test_legacy_route_boundary_is_preserved_when_refresh_marks_recent_days(self):
        state = {
            "collections": {},
            "routes": {
                "user": {"room": {"sources": {"collection": {"days": {}}}}}
            },
        }
        history = ReviewHistory(state, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])
        history.observe("collection", "2026-10-03", [(2, 20, 600, 3, 1)])

        history.mark_route_days(
            "user",
            "room",
            "collection",
            ["2026-10-03"],
            since_day="2026-10-03",
        )
        pending = history.pending("user", "room", since_day="2026-10-03")

        self.assertEqual(
            [batch["target_day"] for batch in pending],
            ["2026-09-01", "2026-10-03"],
        )

    def test_compact_removes_only_fully_acknowledged_old_days(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])
        history.observe("collection", "2026-09-02", [(2, 20, 600, 3, 1)])
        old_batches = history.pending("user", "room")
        history.acknowledge("user", "room", old_batches[0])

        removed = history.compact("2026-10-01")

        self.assertEqual(removed, 1)
        self.assertNotIn(
            "2026-09-01",
            history.state["collections"]["collection"]["days"],
        )
        self.assertIn(
            "2026-09-02",
            history.state["collections"]["collection"]["days"],
        )

    def test_unattempted_room_day_survives_compaction_and_retries(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])
        history.mark_route_days(
            "user", "room", "collection", ["2026-09-01"]
        )

        removed = history.compact("2026-10-01")
        retry = history.pending("user", "room", since_day="2026-10-03")

        self.assertEqual(removed, 0)
        self.assertEqual([batch["target_day"] for batch in retry], ["2026-09-01"])

    def test_unrouted_local_cache_can_be_compacted(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", "2026-09-01", [(1, 10, 500, 3, 1)])

        removed = history.compact("2026-10-01", discard_unrouted=True)

        self.assertEqual(removed, 1)
        self.assertEqual(history.state["collections"], {})

    def test_repeated_observation_and_mobile_additions_are_deduplicated(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [row(1, time_ms=1500)])
        history.observe("collection", DAY, [row(1, time_ms=1500), row(2, time_ms=750)])

        self.assertEqual(history.today(DAY), {"seconds": 2.25, "answers": 2})
        pending = history.pending("user", "room")
        self.assertEqual(len(pending), 1)
        self.assertEqual([item["id"] for item in pending[0]["reviews"]], ["1", "2"])

    def test_routes_are_isolated_and_retry_until_acknowledged(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [row(1)])

        first = history.pending("user", "room-a")
        self.assertEqual(history.pending("user", "room-a"), first)
        self.assertEqual(history.pending("user", "room-b"), first)
        self.assertEqual(history.pending("other-user", "room-a"), first)

        history.acknowledge("user", "room-a", first[0])
        self.assertEqual(history.pending("user", "room-a"), [])
        self.assertEqual(len(history.pending("user", "room-b")), 1)
        self.assertEqual(len(history.pending("other-user", "room-a")), 1)

    def test_state_round_trips_through_json(self):
        state = {}
        history = ReviewHistory(state)
        history.observe("collection", DAY, [row(1, time_ms=1234)])
        history.acknowledge("user", "room", history.pending("user", "room")[0])

        restored_state = json.loads(json.dumps(state))
        restored = ReviewHistory(restored_state)
        self.assertEqual(restored.today(DAY), {"seconds": 1.234, "answers": 1})
        self.assertEqual(restored.pending("user", "room"), [])

    def test_empty_observed_day_activates_once_after_ack(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [])

        batch = history.pending("user", "room")
        self.assertEqual(
            batch,
            [{
                "source_collection": "collection",
                "target_day": DAY,
                "reviews": [],
                "removed_ids": [],
            }],
        )
        self.assertEqual(history.pending("user", "room"), batch)
        history.acknowledge("user", "room", batch[0])
        self.assertEqual(history.pending("user", "room"), [])

    def test_millisecond_precision_and_filters(self):
        history = ReviewHistory({})
        history.observe(
            "collection",
            DAY,
            [
                row(1, time_ms=333),
                row(2, time_ms=667, review_type=3),
                row(3, time_ms=-20),
                row(4, ease=0),
                row(5, review_type=4),
                (True, 99, 1000, 3, 1),
            ],
        )
        self.assertEqual(history.today(DAY), {"seconds": 1.0, "answers": 3})

    def test_late_acknowledgement_cannot_swallow_changed_or_new_review(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [row(1, time_ms=1000)])
        in_flight = history.pending("user", "room")[0]

        history.observe(
            "collection",
            DAY,
            [row(1, time_ms=1200), row(2, time_ms=500)],
        )
        history.acknowledge("user", "room", in_flight)

        remaining = history.pending("user", "room")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(
            {(item["id"], item["time_ms"]) for item in remaining[0]["reviews"]},
            {("1", 1200), ("2", 500)},
        )

    def test_day_rollover_retains_unacknowledged_previous_day(self):
        history = ReviewHistory({})
        history.observe("collection", "2026-10-03", [row(1)])
        history.observe("collection", DAY, [row(2)])

        self.assertEqual(
            [batch["target_day"] for batch in history.pending("user", "room")],
            ["2026-10-03", DAY],
        )

    def test_missing_rows_do_not_delete_without_explicit_removal(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [row(1), row(2)])
        history.observe("collection", DAY, [row(2)])
        self.assertEqual(history.today(DAY)["answers"], 2)

        history.observe("collection", DAY, [row(1), row(2)])
        history.observe("collection", DAY, [row(2)], allow_removals=True)
        self.assertEqual(history.today(DAY)["answers"], 1)
        batch = history.pending("user", "room")[0]
        self.assertEqual(
            [
                {"id": item["id"], "card_id": item["card_id"]}
                for item in batch["removed_ids"]
            ],
            [{"id": "1", "card_id": "1001"}],
        )
        self.assertGreater(batch["removed_ids"][0]["changed_at"], 0)

    def test_tombstoned_review_stays_removed_if_reobserved(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [row(1)])
        initial = history.pending("user", "room")[0]
        history.acknowledge("user", "room", initial)

        history.observe("collection", DAY, [], allow_removals=True)
        removal = history.pending("user", "room")[0]
        history.observe("collection", DAY, [row(1)])
        history.acknowledge("user", "room", removal)

        self.assertEqual(history.today(DAY), {"seconds": 0.0, "answers": 0})
        self.assertEqual(history.pending("user", "room"), [])

    def test_batches_are_limited_to_500_total_items(self):
        history = ReviewHistory({})
        rows = [row(index) for index in range(1, 1102)]
        history.observe("collection", DAY, rows)

        batches = history.pending("user", "room")
        self.assertEqual(len(batches), 3)
        self.assertEqual(
            sum(len(batch["reviews"]) for batch in batches),
            len(rows),
        )
        self.assertTrue(
            all(
                len(batch["reviews"]) + len(batch["removed_ids"])
                <= MAX_BATCH_ITEMS
                for batch in batches
            )
        )

    def test_multiple_collections_with_same_review_ids_do_not_collide(self):
        history = ReviewHistory({})
        history.observe("collection-a", DAY, [row(1, time_ms=400)])
        history.observe("collection-b", DAY, [row(1, time_ms=600)])

        self.assertEqual(history.today(DAY), {"seconds": 0.6, "answers": 1})
        batches = history.pending("user", "room")
        self.assertEqual(
            {batch["source_collection"] for batch in batches},
            {"collection-a", "collection-b"},
        )

    def test_unknown_day_returns_none_but_observed_empty_day_returns_zero(self):
        history = ReviewHistory({})
        self.assertIsNone(history.today(DAY))
        history.observe("collection", DAY, [])
        self.assertEqual(history.today(DAY), {"seconds": 0.0, "answers": 0})

    def test_undo_redo_versions_override_stale_callbacks(self):
        clock = iter([100, 200])
        history = ReviewHistory({}, now_ms=lambda: next(clock))
        history.observe("collection", DAY, [row(1, time_ms=800)])
        initial = history.pending("user", "room")[0]
        self.assertEqual(initial["reviews"][0]["changed_at"], 0)
        history.acknowledge("user", "room", initial)

        history.observe("collection", DAY, [], allow_removals=True)
        undo = history.pending("user", "room")[0]
        self.assertEqual(undo["removed_ids"][0]["changed_at"], 100)

        history.observe("collection", DAY, [row(1, time_ms=800)], allow_removals=True)
        redo = history.pending("user", "room")[0]
        self.assertEqual(redo["reviews"][0]["changed_at"], 200)

        history.acknowledge("user", "room", undo)
        still_pending = history.pending("user", "room")
        self.assertEqual(still_pending, [redo])
        history.acknowledge("user", "room", redo)
        self.assertEqual(history.pending("user", "room"), [])
        self.assertEqual(history.today(DAY), {"seconds": 0.8, "answers": 1})

    def test_stale_positive_ack_cannot_swallow_newer_undo(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", DAY, [row(1)])
        stale_positive = history.pending("user", "room")[0]
        history.observe("collection", DAY, [], allow_removals=True)

        history.acknowledge("user", "room", stale_positive)
        pending = history.pending("user", "room")
        self.assertEqual(pending[0]["reviews"], [])
        self.assertEqual(pending[0]["removed_ids"][0]["changed_at"], 100)

    def test_restore_gap_then_unrelated_undo_does_not_tombstone_cached_review(self):
        history = ReviewHistory({}, now_ms=lambda: 100)
        history.observe("collection", DAY, [row(1)])

        history.observe("collection", DAY, [])
        history.observe("collection", DAY, [], allow_removals=True)

        self.assertEqual(history.today(DAY), {"seconds": 1.0, "answers": 1})
        pending = history.pending("user", "room")[0]
        self.assertEqual([item["id"] for item in pending["reviews"]], ["1"])
        self.assertEqual(pending["removed_ids"], [])

    def test_stale_reappearance_before_unrelated_undo_does_not_revive_tombstone(self):
        clock = iter([100, 200])
        history = ReviewHistory({}, now_ms=lambda: next(clock))
        history.observe("collection", DAY, [row(1)])
        history.observe("collection", DAY, [], allow_removals=True)

        history.observe("collection", DAY, [row(1)])
        history.observe("collection", DAY, [row(1)], allow_removals=True)

        self.assertEqual(history.today(DAY), {"seconds": 0.0, "answers": 0})
        pending = history.pending("user", "room")[0]
        self.assertEqual(pending["reviews"], [])
        self.assertEqual(pending["removed_ids"][0]["changed_at"], 100)

    def test_normal_observation_cannot_revive_tombstone(self):
        history = ReviewHistory({}, now_ms=lambda: 10)
        history.observe("collection", DAY, [row(1)])
        history.observe("collection", DAY, [], allow_removals=True)
        history.observe("collection", DAY, [row(1, time_ms=2000)])

        self.assertEqual(history.today(DAY), {"seconds": 0.0, "answers": 0})

    def test_today_uses_only_latest_observed_collection(self):
        history = ReviewHistory({})
        history.observe("old-collection", DAY, [row(1, time_ms=400)])
        history.observe("current-collection", DAY, [row(2, time_ms=600)])

        self.assertEqual(history.today(DAY), {"seconds": 0.6, "answers": 1})
        self.assertEqual(len(history.pending("user", "room")), 2)

    def test_invalidating_route_republishes_known_reviews(self):
        history = ReviewHistory({})
        history.observe("collection", DAY, [row(1)])
        sent = history.pending("user", "room")
        history.acknowledge("user", "room", sent[0])
        self.assertEqual(history.pending("user", "room"), [])

        history.invalidate_route("user", "room")
        self.assertEqual(history.pending("user", "room"), sent)


if __name__ == "__main__":
    unittest.main()
