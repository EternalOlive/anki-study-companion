from __future__ import annotations

import json
from unittest import TestCase

from study_companion.realtime import RealtimeClient


class RealtimeClientTests(TestCase):
    def test_initial_state(self):
        client = RealtimeClient(apikey="test-key")
        self.assertEqual(client.apikey, "test-key")
        self.assertIsNone(client.group_id)
        self.assertIsNone(client.user_id)
        self.assertFalse(client.is_connected())

    def test_handle_broadcast_review_tick(self):
        client = RealtimeClient(apikey="test-key")
        client.user_id = "my-user-id"

        received = []
        client.review_tick_received.connect(
            lambda uid, slot, ans, ms: received.append((uid, slot, ans, ms))
        )

        msg = {
            "event": "broadcast",
            "topic": "realtime:room:g1",
            "payload": {
                "event": "review_tick",
                "payload": {
                    "user_id": "friend-1",
                    "slot": 42,
                    "answers": 1,
                    "time_ms": 3200,
                },
            },
        }
        client._on_message(json.dumps(msg))

        self.assertEqual(received, [("friend-1", 42, 1, 3200)])

    def test_ignore_own_broadcast(self):
        client = RealtimeClient(apikey="test-key")
        client.user_id = "my-user-id"

        received = []
        client.review_tick_received.connect(
            lambda uid, slot, ans, ms: received.append((uid, slot, ans, ms))
        )

        msg = {
            "event": "broadcast",
            "topic": "realtime:room:g1",
            "payload": {
                "event": "review_tick",
                "payload": {
                    "user_id": "my-user-id",
                    "slot": 42,
                    "answers": 1,
                    "time_ms": 3200,
                },
            },
        }
        client._on_message(json.dumps(msg))

        self.assertEqual(received, [])

    def test_handle_presence_state_and_diff(self):
        client = RealtimeClient(apikey="test-key")
        client.user_id = "my-user-id"

        presence_updates = []
        client.presence_changed.connect(lambda p: presence_updates.append(dict(p)))

        # 1. State
        state_msg = {
            "event": "presence_state",
            "topic": "realtime:room:g1",
            "payload": {
                "u1": {"metas": [{"status": "studying", "deck": "English"}]},
                "u2": {"metas": [{"status": "paused", "deck": "Math"}]},
            },
        }
        client._on_message(json.dumps(state_msg))

        self.assertEqual(len(presence_updates), 1)
        self.assertEqual(
            presence_updates[-1],
            {
                "u1": {"status": "studying", "deck": "English"},
                "u2": {"status": "paused", "deck": "Math"},
            },
        )
        self.assertEqual(client.current_presences()["u1"]["status"], "studying")

        # 2. Diff (u1 leaves, u3 joins)
        diff_msg = {
            "event": "presence_diff",
            "topic": "realtime:room:g1",
            "payload": {
                "joins": {
                    "u3": {"metas": [{"status": "studying", "deck": "Science"}]}
                },
                "leaves": {"u1": {}},
            },
        }
        client._on_message(json.dumps(diff_msg))

        self.assertEqual(len(presence_updates), 2)
        self.assertNotIn("u1", client.current_presences())
        self.assertEqual(client.current_presences()["u3"]["status"], "studying")
        self.assertEqual(client.current_presences()["u2"]["status"], "paused")

    def test_apply_review_tick_to_members(self):
        from study_companion.realtime import apply_review_tick_to_members

        members = [
            {"user_id": "u1", "answer_count": 10, "activity_buckets": [{"slot": 5, "answer_count": 2, "time_ms": 1000}]},
            {"user_id": "u2", "answer_count": 5, "activity_buckets": []},
        ]

        # Existing slot update
        applied = apply_review_tick_to_members(members, "u1", slot=5, answers=1, time_ms=500)
        self.assertTrue(applied)
        self.assertEqual(members[0]["answer_count"], 11)
        self.assertEqual(members[0]["activity_buckets"][0]["answer_count"], 3)
        self.assertEqual(members[0]["activity_buckets"][0]["time_ms"], 1500)

        # New slot creation
        applied2 = apply_review_tick_to_members(members, "u2", slot=12, answers=2, time_ms=2000)
        self.assertTrue(applied2)
        self.assertEqual(members[1]["answer_count"], 7)
        self.assertEqual(members[1]["activity_buckets"], [{"slot": 12, "answer_count": 2, "time_ms": 2000}])

        # Unknown user
        self.assertFalse(apply_review_tick_to_members(members, "unknown", slot=1, answers=1, time_ms=100))

    def test_apply_presence_to_members(self):
        from study_companion.realtime import apply_presence_to_members

        members = [
            {"user_id": "u1", "status": "online", "current_deck_name": None},
            {"user_id": "u2", "status": "paused", "current_deck_name": "OldDeck"},
        ]

        presences = {
            "u1": {"status": "studying", "current_deck_name": "NewDeck", "display_name": "CoolUser"},
        }

        changed = apply_presence_to_members(members, presences, "2026-10-06T22:00:00")
        self.assertTrue(changed)
        self.assertEqual(members[0]["status"], "studying")
        self.assertEqual(members[0]["current_deck_name"], "NewDeck")
        self.assertEqual(members[0]["display_name"], "CoolUser")
        self.assertEqual(members[0]["updated_at"], "2026-10-06T22:00:00")
        # u2 was not in presences
        self.assertEqual(members[1]["status"], "paused")
        self.assertEqual(members[1]["current_deck_name"], "OldDeck")

    def test_apply_presence_sanitizes_malicious_display_name(self):
        from study_companion.realtime import apply_presence_to_members
        from study_companion.nicknames import canonical_nickname

        members = [
            {"user_id": "u1", "status": "online", "display_name": "OldName"},
        ]
        presences = {
            "u1": {"display_name": "<script>alert('xss')</script>"},
        }
        changed = apply_presence_to_members(members, presences, "2026-10-06T22:00:00")
        self.assertTrue(changed)
        self.assertEqual(members[0]["display_name"], canonical_nickname("u1"))

    def test_member_state_broadcast_sanitizes_display_name(self):
        from study_companion.nicknames import canonical_nickname

        client = RealtimeClient(apikey="test-key")
        client.user_id = "my-user"

        store = {}
        client.presence_changed.connect(lambda p: store.update(p))

        msg = {
            "event": "broadcast",
            "topic": "realtime:room:g1",
            "payload": {
                "event": "member_state",
                "payload": {
                    "user_id": "peer-1",
                    "status": "online",
                    "display_name": "<img src=x onerror=alert(1)>",
                },
            },
        }
        client._on_message(json.dumps(msg))
        self.assertIn("peer-1", store)
        self.assertEqual(store["peer-1"]["display_name"], canonical_nickname("peer-1"))

