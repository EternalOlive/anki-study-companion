import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from study_companion.online import DeviceSyncLedger, SupabaseError, DeviceSnapshotConflict
from study_companion.outbox import SyncOutbox
from study_companion.reviews import ReviewHistory
from study_companion.record_status import RecordStatusLedger


class FakeClient:
    def __init__(self):
        self.calls = []
        self.deck_calls = []
        self.member_calls = []
        self.review_calls = []
        self.fail_days = set()
        self.review_fail_days = set()
        self.review_failures = {}
        self.member_error = None

    def upsert_profile(self, token, user_id, display_name):
        return None

    def record_device_day(self, token, **payload):
        self.calls.append(dict(payload))
        if payload["study_day"] in self.fail_days:
            raise SupabaseError("offline", status=503)
        return {"revision": payload["revision"]}

    def fetch_group_today(self, token, group_id, study_day):
        self.member_calls.append((token, group_id, study_day))
        if self.member_error is not None:
            raise self.member_error
        return []

    def set_current_deck(self, token, group_id, device_id, deck_name):
        self.deck_calls.append((group_id, device_id, deck_name))
        return None

    def sync_review_day(self, token, *, group_id, batch):
        self.review_calls.append((token, group_id, batch["target_day"]))
        if batch["target_day"] in self.review_failures:
            raise self.review_failures[batch["target_day"]]
        if batch["target_day"] in self.review_fail_days:
            raise SupabaseError("review day archived", status=400)
        return None


class OfflineSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        controller = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Controller"
        )
        controller.body = [
            node for node in controller.body
            if isinstance(node, ast.FunctionDef) and node.name == "sync_async"
        ]
        cls.scheduled = []
        scope = {
            "now": lambda: cls.clock,
            "canonical_nickname": lambda user_id: "ABC-DEF",
            "SupabaseError": SupabaseError,
            "DeviceSnapshotConflict": DeviceSnapshotConflict,
            "time": SimpleNamespace(time=lambda: 0),
            "mw": SimpleNamespace(
                taskman=SimpleNamespace(
                    run_in_background=lambda task, done: cls.scheduled.append((task, done))
                )
            ),
            "QTimer": SimpleNamespace(singleShot=lambda delay, callback: callback()),
            "_not_group_member_error": lambda error: (
                str(error).strip().casefold() == "not a member of this group"
            ),
            "_continue_after_review_batch_error": lambda error: (
                "review day archived" in str(error).casefold()
                or getattr(error, "status", None) in (400, 409, 422)
            ),
        }
        exec(compile(ast.Module(body=[controller], type_ignores=[]), str(path), "exec"), scope)
        cls.Controller = scope["Controller"]

    def setUp(self):
        self.__class__.scheduled = []
        self.__class__.clock = datetime(2026, 10, 4, 0, 0, 30, tzinfo=timezone.utc)
        controller = self.Controller()
        controller.closed = False
        controller.sync_in_flight = False
        controller.sync_pending = False
        controller.sync_failure_count = 0
        controller.next_sync_attempt_at = 0.0
        controller.identity_generation = 0
        controller.online = {
            "auth": {"user_id": "user-a", "access_token": "token"},
            "group": {"id": "room-a"},
            "display_name": "ABC-DEF",
        }
        controller._access_token = lambda: "token"
        controller.tracker = SimpleNamespace(
            current_deck_name="English",
            records={
                "2026-10-03": {"seconds": 120, "answers": 8},
                "2026-10-04": {"seconds": 10, "answers": 1},
            },
            today=lambda current: {"seconds": 10, "answers": 1},
            time_goal_minutes=60,
            card_goal=100,
            status="studying",
        )
        controller.device_id = "device-a"
        controller.device_ledger = DeviceSyncLedger({})
        controller.device_ledger.prepare(
            user_id="user-a|room-a", day="2026-10-03", active_seconds=100,
            answer_count=7, time_goal_minutes=60, card_goal=100,
            status="studying",
        )
        controller.sync_outbox = SyncOutbox({})
        controller.review_history = ReviewHistory({})
        controller.sync_outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-03", ledger_id="user-a|room-a",
        )
        controller.client = FakeClient()
        controller.save = Mock()
        controller.t = lambda ko, en: ko
        controller._last_member_fetch_at = 0
        controller.record_status = RecordStatusLedger({})
        controller.leave_current_room_locally = Mock(return_value=True)
        self.controller = controller

    def test_upload_and_member_failures_are_reported_independently(self):
        self.controller.client.fail_days.add("2026-10-04")
        self.controller.client.member_error = SupabaseError("read offline", status=503)
        self.controller.sync_async(force=True)
        self.finish_background()
        status = self.controller.record_status.snapshot(user_id="user-a", group_id="room-a")
        self.assertEqual(set(status["errors"]), {"upload", "members"})
        self.assertEqual(status["primary_issue"], "upload")
        self.controller.client.fail_days.clear()
        self.controller.sync_async(force=True)
        self.finish_background()
        status = self.controller.record_status.snapshot(user_id="user-a", group_id="room-a")
        self.assertEqual(set(status["errors"]), {"members"})
        self.assertIsNotNone(status["upload_at"])

    def finish_background(self):
        task, done = self.scheduled.pop()
        result = task()
        done(SimpleNamespace(result=lambda: result))

    def take_background(self):
        task, done = self.scheduled.pop()
        return task, done

    def test_midnight_queues_final_previous_day_before_current_day(self):
        self.controller.sync_async()

        pending = self.controller.sync_outbox.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )
        by_day = {row["study_day"]: row for row in pending}
        self.assertEqual(set(by_day), {"2026-10-03", "2026-10-04"})
        self.assertEqual(by_day["2026-10-03"]["active_seconds"], 120)
        self.assertEqual(by_day["2026-10-03"]["answer_count"], 8)
        self.assertEqual(by_day["2026-10-03"]["status"], "stopped")
        self.controller.save.assert_called_once()

    def test_deck_name_sharing_defaults_on_and_off_clears_server_value(self):
        self.controller.sync_async()
        task, _done = self.take_background()
        task()
        self.assertEqual(
            self.controller.client.deck_calls[-1],
            ("room-a", "device-a", "English"),
        )

        self.controller.sync_in_flight = False
        self.controller.online["share_deck_name"] = False
        self.controller.sync_async()
        task, _done = self.take_background()
        task()
        self.assertEqual(
            self.controller.client.deck_calls[-1],
            ("room-a", "device-a", None),
        )

    def test_unchanged_deck_and_recent_members_skip_redundant_requests(self):
        self.controller.online["share_deck_name"] = False
        self.controller.online["published_deck"] = {
            "user_id": "user-a",
            "group_id": "room-a",
            "device_id": "device-a",
            "name": None,
            "published_at": 30,
        }
        self.controller.online["members"] = [{"user_id": "friend"}]
        self.controller._member_cache_key = ("user-a", "room-a", "2026-10-04")
        self.controller._last_member_fetch_at = 1

        with patch.object(time, "time", return_value=60):
            self.controller.sync_async()
            task, _done = self.take_background()
            _auth, _acks, members, deck_published, _error = task()

        self.assertEqual(self.controller.client.deck_calls, [])
        self.assertIsNone(members)
        self.assertFalse(deck_published)

    def test_unchanged_deck_is_republished_before_server_ttl_expires(self):
        self.controller.online["share_deck_name"] = True
        self.controller.online["published_deck"] = {
            "user_id": "user-a",
            "group_id": "room-a",
            "device_id": "device-a",
            "name": "English",
            "published_at": 30,
        }
        self.controller.online["members"] = [{"user_id": "friend"}]
        self.controller._member_cache_key = ("user-a", "room-a", "2026-10-04")
        self.controller._last_member_fetch_at = 30

        with patch.object(time, "time", return_value=91):
            self.controller.sync_async()
            task, _done = self.take_background()
            _auth, _acks, members, deck_published, _error = task()

        self.assertEqual(
            self.controller.client.deck_calls,
            [("room-a", "device-a", "English")],
        )
        self.assertIsNone(members)
        self.assertTrue(deck_published)

    def test_cleared_deck_does_not_need_a_ttl_heartbeat(self):
        self.controller.online["share_deck_name"] = False
        self.controller.online["published_deck"] = {
            "user_id": "user-a",
            "group_id": "room-a",
            "device_id": "device-a",
            "name": None,
            "published_at": 1,
        }
        self.controller.online["members"] = [{"user_id": "friend"}]
        self.controller._member_cache_key = ("user-a", "room-a", "2026-10-04")
        self.controller._last_member_fetch_at = 1
        with patch.object(time, "time", return_value=1000):
            self.controller.sync_async()
            task, _done = self.take_background()
            _auth, _acks, _members, deck_published, _error = task()

        self.assertEqual(self.controller.client.deck_calls, [])
        self.assertFalse(deck_published)

    def test_force_refresh_ignores_a_fresh_member_cache(self):
        self.controller.online["members"] = [{"user_id": "friend"}]
        self.controller._member_cache_key = ("user-a", "room-a", "2026-10-04")
        self.controller._last_member_fetch_at = 100

        with patch.object(time, "time", return_value=101):
            self.controller.sync_async(force=True)
            task, _done = self.take_background()
            _auth, _acks, members, _deck_published, _error = task()

        self.assertEqual(members, [])
        self.assertEqual(
            self.controller.client.member_calls,
            [("token", "room-a", "2026-10-04")],
        )

    def test_hidden_panel_skips_member_read_even_when_forced(self):
        self.controller.panel = SimpleNamespace(isVisible=lambda: False)

        self.controller.sync_async(force=True)
        task, _done = self.take_background()
        _auth, _acks, members, _deck_published, _error = task()

        self.assertIsNone(members)
        self.assertEqual(self.controller.client.member_calls, [])

    def test_network_failures_back_off_and_force_can_bypass_delay(self):
        self.controller.client.fail_days.add("2026-10-04")
        with patch.object(time, "monotonic", return_value=100):
            self.controller.sync_async()
            self.finish_background()

        self.assertEqual(self.controller.sync_failure_count, 1)
        self.assertEqual(self.controller.next_sync_attempt_at, 160)

        with patch.object(time, "monotonic", return_value=159):
            self.controller.sync_async()
        self.assertEqual(self.scheduled, [])

        with patch.object(time, "monotonic", return_value=159):
            self.controller.sync_async(force=True)
        self.assertEqual(len(self.scheduled), 1)

    def test_success_resets_retry_backoff(self):
        self.controller.client.fail_days.add("2026-10-04")
        with patch.object(time, "monotonic", return_value=100):
            self.controller.sync_async()
            self.finish_background()
        self.controller.client.fail_days.clear()

        with patch.object(time, "monotonic", return_value=101):
            self.controller.sync_async(force=True)
            self.finish_background()

        self.assertEqual(self.controller.sync_failure_count, 0)
        self.assertEqual(self.controller.next_sync_attempt_at, 0.0)

    def test_clock_rollback_refreshes_members_instead_of_freezing_cache(self):
        self.controller.online["members"] = [{"user_id": "friend"}]
        self.controller._member_cache_key = ("user-a", "room-a", "2026-10-04")
        self.controller._last_member_fetch_at = 500

        with patch.object(time, "time", return_value=100):
            self.controller.sync_async()
            task, _done = self.take_background()
            _auth, _acks, members, _deck_published, _error = task()

        self.assertEqual(members, [])
        self.assertEqual(len(self.controller.client.member_calls), 1)

    def test_account_change_invalidates_member_and_deck_cache(self):
        self.controller.online["share_deck_name"] = False
        self.controller.online["auth"] = {
            "user_id": "user-b", "access_token": "token-b"
        }
        self.controller.online["members"] = [{"user_id": "old-friend"}]
        self.controller._member_cache_key = ("user-a", "room-a", "2026-10-04")
        self.controller._last_member_fetch_at = 100
        self.controller.online["published_deck"] = {
            "user_id": "user-a",
            "group_id": "room-a",
            "device_id": "device-a",
            "name": None,
            "published_at": 100,
        }

        with patch.object(time, "time", return_value=101):
            self.controller.sync_async()
            task, _done = self.take_background()
            _auth, _acks, members, deck_published, _error = task()

        self.assertEqual(members, [])
        self.assertTrue(deck_published)
        self.assertEqual(self.controller.client.deck_calls, [("room-a", "device-a", None)])

    def test_member_read_failure_does_not_discard_successful_upload_acks(self):
        self.controller.review_history.observe(
            "collection", "2026-10-04", [(100, 200, 2500, 3, 1)]
        )
        self.controller.client.member_error = SupabaseError("read offline", status=503)

        self.controller.sync_async(force=True)
        self.finish_background()

        self.assertEqual(
            self.controller.review_history.pending("user-a", "room-a"), []
        )
        self.assertEqual(
            self.controller.sync_outbox.pending(
                user_id="user-a", group_id="room-a", device_id="device-a"
            ),
            [],
        )
        self.assertEqual(self.controller.online["last_error"], "read offline")

    def test_unauthorized_member_read_refreshes_session_once(self):
        self.controller.online["auth"].update(
            {"refresh_token": "refresh", "expires_at": int(time.time()) + 3600}
        )
        self.controller.client.fetch_group_today = Mock(
            side_effect=[SupabaseError("expired", status=401), []]
        )
        self.controller.client.refresh = Mock(
            return_value={
                "access_token": "fresh-token",
                "refresh_token": "fresh-refresh",
                "expires_at": int(time.time()) + 7200,
            }
        )

        self.controller.sync_async(force=True)
        self.finish_background()

        self.controller.client.refresh.assert_called_once_with("refresh")
        self.assertEqual(
            [call.args[0] for call in self.controller.client.fetch_group_today.call_args_list],
            ["token", "fresh-token"],
        )
        self.assertEqual(
            self.controller.online["auth"]["access_token"], "fresh-token"
        )

    def test_archived_old_review_does_not_block_today_and_stays_pending(self):
        self.controller.review_history.observe(
            "collection", "2026-01-01", [(1, 10, 1000, 3, 1)]
        )
        self.controller.review_history.observe(
            "collection", "2026-10-04", [(2, 20, 2000, 3, 1)]
        )
        self.controller.review_history.pending("user-a", "room-a")
        self.controller.client.review_fail_days.add("2026-01-01")

        self.controller.sync_async(force=True)
        self.finish_background()

        self.assertEqual(
            [day for _token, _room, day in self.controller.client.review_calls],
            ["2026-10-04", "2026-01-01"],
        )
        self.assertEqual(
            [
                batch["target_day"]
                for batch in self.controller.review_history.pending("user-a", "room-a")
            ],
            ["2026-01-01"],
        )

    def test_deterministic_bad_day_does_not_block_another_day(self):
        self.controller.review_history.observe(
            "collection", "2026-10-03", [(1, 10, 1000, 3, 1)]
        )
        self.controller.review_history.observe(
            "collection", "2026-10-04", [(2, 20, 2000, 3, 1)]
        )
        self.controller.client.review_failures["2026-10-04"] = SupabaseError(
            "invalid review event", status=400
        )

        self.controller.sync_async(force=True)
        self.finish_background()

        self.assertEqual(
            [day for _token, _room, day in self.controller.client.review_calls],
            ["2026-10-04", "2026-10-03"],
        )
        self.assertEqual(
            [batch["target_day"] for batch in self.controller.review_history.pending(
                "user-a", "room-a"
            )],
            ["2026-10-04"],
        )

    def test_transient_bad_day_stops_later_review_batches(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.setUp()
                self.controller.review_history.observe(
                    "collection", "2026-10-03", [(1, 10, 1000, 3, 1)]
                )
                self.controller.review_history.observe(
                    "collection", "2026-10-04", [(2, 20, 2000, 3, 1)]
                )
                self.controller.client.review_failures["2026-10-04"] = SupabaseError(
                    "temporary failure", status=status
                )

                self.controller.sync_async(force=True)
                self.finish_background()

                self.assertEqual(
                    [day for _token, _room, day in self.controller.client.review_calls],
                    ["2026-10-04"],
                )
                self.assertEqual(
                    [batch["target_day"] for batch in self.controller.review_history.pending(
                        "user-a", "room-a"
                    )],
                    ["2026-10-03", "2026-10-04"],
                )

    def test_partial_failure_acks_success_and_keeps_only_failed_day(self):
        self.controller.client.fail_days.add("2026-10-03")
        self.controller.sync_async()
        self.finish_background()

        pending = self.controller.sync_outbox.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )
        self.assertEqual([row["study_day"] for row in pending], ["2026-10-03"])
        self.assertEqual(self.controller.online["last_error"], "offline")

    def test_restart_retries_persisted_history_for_same_route(self):
        queued = SyncOutbox({})
        queued.enqueue({
            "user_id": "user-a", "group_id": "room-a", "device_id": "device-a",
            "study_day": "2026-10-02", "revision": 4,
            "active_seconds": 50, "answer_count": 3,
            "time_goal_minutes": 60, "card_goal": 100, "status": "stopped",
        })
        queued.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-04", ledger_id="user-a|room-a",
        )
        self.controller.sync_outbox = SyncOutbox(queued.state)

        self.controller.sync_async()
        self.finish_background()

        days = [call["study_day"] for call in self.controller.client.calls]
        self.assertIn("2026-10-02", days)
        self.assertEqual(self.controller.sync_outbox.state["entries"], {})

    def test_switching_rooms_does_not_replay_the_previous_rooms_totals(self):
        self.controller.sync_outbox.enqueue({
            "user_id": "user-a", "group_id": "room-a", "device_id": "device-a",
            "study_day": "2026-10-03", "ledger_id": "user-a|room-a",
            "revision": 1, "active_seconds": 120, "answer_count": 8,
            "time_goal_minutes": 60, "card_goal": 100, "status": "stopped",
        })
        self.controller.online["group"] = {"id": "room-b"}

        self.controller.sync_async()

        new_room = self.controller.sync_outbox.pending(
            user_id="user-a", group_id="room-b", device_id="device-a"
        )
        old_room = self.controller.sync_outbox.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )
        self.assertEqual(new_room[0]["active_seconds"], 0)
        self.assertEqual(new_room[0]["answer_count"], 0)
        self.assertEqual(len(old_room), 1)

    def test_upgraded_legacy_ledger_is_not_reused_after_leave_join(self):
        self.controller.sync_outbox = SyncOutbox({})
        self.controller.device_ledger = DeviceSyncLedger({})
        self.controller.device_ledger.prepare(
            user_id="user-a", day="2026-10-04", active_seconds=300,
            answer_count=20, time_goal_minutes=60, card_goal=100,
            status="paused",
        )
        self.controller.tracker.today = lambda current: {
            "seconds": 330, "answers": 22
        }

        self.controller.sync_async()
        first = self.controller.sync_outbox.pending(
            user_id="user-a", group_id="room-a", device_id="device-a"
        )[0]
        self.assertEqual(first["ledger_id"], "user-a")
        self.finish_background()
        self.controller.sync_outbox.discard_room("user-a", "room-a")
        self.controller.online["group"] = {"id": "room-b"}

        self.controller.sync_async()
        second = self.controller.sync_outbox.pending(
            user_id="user-a", group_id="room-b", device_id="device-a"
        )[0]
        self.assertEqual(second["ledger_id"], "user-a|room-b")
        self.assertEqual(second["active_seconds"], 0)
        self.assertEqual(second["answer_count"], 0)

    def test_old_same_user_session_cannot_replace_new_login(self):
        self.controller.sync_async()
        task, done = self.take_background()
        result = task()
        self.controller.online["auth"] = {
            "user_id": "user-a", "access_token": "new-token"
        }
        self.controller.online["members"] = ["new-session"]

        done(SimpleNamespace(result=lambda: result))

        self.assertEqual(
            self.controller.online["auth"]["access_token"], "new-token"
        )
        self.assertEqual(self.controller.online["members"], ["new-session"])

    def test_changed_login_generation_drops_callback_and_runs_pending_sync(self):
        self.controller.sync_async()
        task, done = self.take_background()
        result = task()
        self.controller.identity_generation += 1
        self.controller.online["members"] = ["new-generation"]
        self.controller.sync_pending = True

        done(SimpleNamespace(result=lambda: result))

        self.assertEqual(self.controller.online["members"], ["new-generation"])
        self.assertFalse(self.controller.sync_pending)
        self.assertEqual(len(self.scheduled), 1)

    def test_old_callback_cannot_mutate_same_room_after_leave_rejoin(self):
        self.controller.sync_async()
        task, done = self.take_background()
        result = task()
        first_epoch = self.controller.sync_outbox.route_epoch("device-a")
        self.controller.sync_outbox.discard_room("user-a", "room-a")
        self.controller.sync_outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-04", ledger_id="user-a|room-a",
        )
        self.controller.online["members"] = ["rejoined-session"]

        done(SimpleNamespace(result=lambda: result))

        self.assertGreater(
            self.controller.sync_outbox.route_epoch("device-a"), first_epoch
        )
        self.assertEqual(self.controller.online["members"], ["rejoined-session"])


if __name__ == "__main__":
    unittest.main()
