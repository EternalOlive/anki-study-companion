import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from study_companion.online import DeviceSyncLedger, SupabaseError, DeviceSnapshotConflict
from study_companion.outbox import SyncOutbox


class FakeClient:
    def __init__(self):
        self.calls = []
        self.deck_calls = []
        self.fail_days = set()

    def upsert_profile(self, token, user_id, display_name):
        return None

    def record_device_day(self, token, **payload):
        self.calls.append(dict(payload))
        if payload["study_day"] in self.fail_days:
            raise SupabaseError("offline", status=503)
        return {"revision": payload["revision"]}

    def fetch_group_today(self, token, group_id, study_day):
        return []

    def set_current_deck(self, token, group_id, device_id, deck_name):
        self.deck_calls.append((group_id, device_id, deck_name))
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
        controller.sync_outbox.bind_route(
            user_id="user-a", group_id="room-a", device_id="device-a",
            study_day="2026-10-03", ledger_id="user-a|room-a",
        )
        controller.client = FakeClient()
        controller.save = Mock()
        controller.t = lambda ko, en: ko
        self.controller = controller

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

    def test_deck_name_sharing_is_opt_in_and_off_clears_server_value(self):
        self.controller.sync_async()
        task, _done = self.take_background()
        task()
        self.assertEqual(
            self.controller.client.deck_calls[-1], ("room-a", "device-a", None)
        )

        self.controller.sync_in_flight = False
        self.controller.online["share_deck_name"] = True
        self.controller.sync_async()
        task, _done = self.take_background()
        task()
        self.assertEqual(
            self.controller.client.deck_calls[-1],
            ("room-a", "device-a", "English"),
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
