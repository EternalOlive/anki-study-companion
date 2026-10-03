"""Integration tests for native review collection and room delivery.

The production methods are compiled from ``addon.py`` so these tests exercise
the actual controller boundaries without importing Anki or Qt.
"""

from __future__ import annotations

import ast
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from study_companion.online import (
    DeviceSnapshotConflict,
    DeviceSyncLedger,
    SupabaseError,
)
from study_companion.outbox import SyncOutbox
from study_companion.reviews import ReviewHistory
from study_companion.activity import weekly_activity


ADDON = Path(__file__).parents[1] / "study_companion" / "addon.py"
KST = timezone(timedelta(hours=9))


def _controller_with(*method_names: str, scope: dict | None = None):
    tree = ast.parse(ADDON.read_text(encoding="utf-8"))
    original = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Controller"
    )
    wanted = set(method_names)
    methods = [
        node for node in original.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = dict(scope or {})
    module = ast.fix_missing_locations(
        ast.Module(
            body=[ast.ClassDef("Controller", [], [], methods, [])],
            type_ignores=[],
        )
    )
    exec(
        compile(
            module,
            str(ADDON),
            "exec",
        ),
        namespace,
    )
    return namespace["Controller"]


class _QueryOp:
    instances = []

    def __init__(self, parent, op, success):
        self.parent = parent
        self.op = op
        self.success = success
        self.on_failure = None
        self.ran = False
        self.__class__.instances.append(self)

    def failure(self, callback):
        self.on_failure = callback
        return self

    def run_in_background(self):
        self.ran = True


class _Future:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


class ReviewCollectionIntegrationTests(TestCase):
    def setUp(self):
        _QueryOp.instances.clear()
        self.current = datetime(2026, 10, 4, 12, 34, 56, tzinfo=KST)
        self.col = SimpleNamespace(
            crt=1700000000,
            db=SimpleNamespace(all=Mock(return_value=[
                (int(self.current.timestamp() * 1000), 20, 1750, 3, 1)
            ])),
        )
        self.mw = SimpleNamespace(col=self.col)
        self.controller_type = _controller_with(
            "refresh_review_history",
            scope={
                "mw": self.mw,
                "now": lambda: self.current,
                "datetime": datetime,
                "timedelta": timedelta,
                "TIMEZONE": KST,
                "weekly_activity": weekly_activity,
            },
        )
        self.controller = self.controller_type()
        self.controller.closed = False
        self.controller.review_query_in_flight = False
        self.controller.review_syncing = False
        self.controller.review_dirty = True
        self.controller.review_allow_removals = False
        self.controller.review_upload_requested = True
        self.controller.review_history = ReviewHistory({}, now_ms=lambda: 123)
        self.controller._weekly_activity = None
        self.controller.online = {}
        self.controller.save = Mock()
        self.controller.refresh = Mock()
        self.controller.sync_async = Mock()
        self.controller.t = lambda ko, en: ko

    def _install_query_op(self):
        # The production method imports QueryOp lazily. Supplying a tiny aqt
        # module keeps this an Anki-independent boundary test.
        import sys
        import types

        previous_aqt = sys.modules.get("aqt")
        previous_operations = sys.modules.get("aqt.operations")
        aqt = types.ModuleType("aqt")
        operations = types.ModuleType("aqt.operations")
        operations.QueryOp = _QueryOp
        sys.modules["aqt"] = aqt
        sys.modules["aqt.operations"] = operations
        self.addCleanup(self._restore_module, "aqt", previous_aqt)
        self.addCleanup(self._restore_module, "aqt.operations", previous_operations)

    @staticmethod
    def _restore_module(name, previous):
        import sys

        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous

    def test_queryop_reads_exact_korean_day_bounds_and_updates_native_total(self):
        self._install_query_op()
        self.controller.refresh_review_history()

        self.assertEqual(len(_QueryOp.instances), 1)
        operation = _QueryOp.instances[0]
        self.assertTrue(operation.ran)
        self.assertTrue(self.controller.review_query_in_flight)
        result = operation.op(self.col)

        expected_start = int(
            datetime(2026, 9, 21, 0, 0, tzinfo=KST).timestamp() * 1000
        )
        expected_end = int(
            datetime(2026, 10, 5, 0, 0, tzinfo=KST).timestamp() * 1000
        )
        self.col.db.all.assert_called_once_with(
            "select id, cid, time, ease, type from revlog where id >= ? and id < ? order by id",
            expected_start,
            expected_end,
        )

        operation.success(result)
        self.assertFalse(self.controller.review_query_in_flight)
        self.assertEqual(
            self.controller.review_history.today("2026-10-04"),
            {"seconds": 1.75, "answers": 1},
        )
        self.controller.save.assert_called_once()
        self.controller.refresh.assert_called_once()
        self.controller.sync_async.assert_called_once_with(force=True)
        self.assertEqual(self.controller._weekly_activity["record"]["answers"], 1)

    def test_midnight_restart_recovers_the_previous_days_last_review(self):
        self._install_query_op()
        self.current = datetime(2026, 10, 5, 0, 0, 30, tzinfo=KST)
        previous_review = datetime(2026, 10, 4, 23, 59, 58, tzinfo=KST)
        self.col.db.all.return_value = [
            (int(previous_review.timestamp() * 1000), 99, 3200, 2, 1)
        ]

        self.controller.refresh_review_history()
        operation = _QueryOp.instances[0]
        operation.success(operation.op(self.col))

        self.assertEqual(
            self.controller.review_history.today("2026-10-04"),
            {"seconds": 3.2, "answers": 1},
        )
        self.assertEqual(
            self.controller.review_history.today("2026-10-05"),
            {"seconds": 0.0, "answers": 0},
        )
        pending_days = {
            batch["target_day"]
            for batch in self.controller.review_history.pending("user", "room")
        }
        self.assertEqual(pending_days, {"2026-10-04", "2026-10-05"})

    def test_complete_empty_query_returns_known_zero_week(self):
        self._install_query_op()
        self.col.db.all.return_value = []

        self.controller.refresh_review_history()
        operation = _QueryOp.instances[0]
        operation.success(operation.op(self.col))

        weekly = self.controller._weekly_activity["record"]
        self.assertEqual(len(weekly["days"]), 7)
        self.assertEqual(weekly["answers"], 0)
        self.assertEqual(weekly["previous_answers"], 0)
        self.assertEqual(
            self.controller.review_history.today("2026-10-04"),
            {"seconds": 0.0, "answers": 0},
        )

    def test_weekly_cutoff_advances_while_query_waits_in_queue(self):
        self._install_query_op()
        self.controller.refresh_review_history()
        operation = _QueryOp.instances[0]

        self.current = self.current + timedelta(minutes=1)
        latest = int((self.current - timedelta(seconds=1)).timestamp() * 1000)
        self.col.db.all.return_value = [(latest, 77, 1000, 3, 1)]
        operation.success(operation.op(self.col))

        weekly = self.controller._weekly_activity["record"]
        self.assertEqual(weekly["answers"], 1)
        self.assertEqual(weekly["as_of"], self.current.isoformat())

    def test_explicit_undo_removals_are_limited_to_today(self):
        self._install_query_op()
        yesterday_first = int(
            datetime(2026, 10, 3, 20, 0, tzinfo=KST).timestamp() * 1000
        )
        yesterday_second = int(
            datetime(2026, 10, 3, 21, 0, tzinfo=KST).timestamp() * 1000
        )
        today_review = int(
            datetime(2026, 10, 4, 9, 0, tzinfo=KST).timestamp() * 1000
        )
        self.controller.review_history.observe(
            "1700000000", "2026-10-03",
            [(yesterday_first, 1, 1000, 3, 1), (yesterday_second, 2, 1000, 3, 1)],
        )
        self.controller.review_history.observe(
            "1700000000", "2026-10-04", [(today_review, 3, 1000, 3, 1)]
        )
        self.controller.review_allow_removals = True
        # The previous-day result is intentionally incomplete, while today's
        # empty result represents the explicit undo operation.
        self.col.db.all.return_value = [(yesterday_second, 2, 1000, 3, 1)]

        self.controller.refresh_review_history()
        operation = _QueryOp.instances[0]
        operation.success(operation.op(self.col))

        self.assertEqual(
            self.controller.review_history.today("2026-10-03")["answers"], 2
        )
        self.assertEqual(
            self.controller.review_history.today("2026-10-04")["answers"], 0
        )
        weekly = self.controller._weekly_activity["record"]
        self.assertEqual(weekly["days"][-1]["answers"], 0)
        self.assertEqual(weekly["answers"], 1)

    def test_query_is_serialized_and_not_started_during_anki_sync(self):
        self._install_query_op()
        self.controller.review_syncing = True
        self.controller.refresh_review_history()
        self.assertEqual(_QueryOp.instances, [])

        self.controller.review_syncing = False
        self.controller.review_query_in_flight = True
        self.controller.refresh_review_history()
        self.assertEqual(_QueryOp.instances, [])

    def test_failed_native_history_save_does_not_trigger_upload(self):
        self._install_query_op()
        self.controller.review_allow_removals = True
        self.controller.save.side_effect = OSError("disk full")
        self.controller.refresh_review_history()
        operation = _QueryOp.instances[0]
        operation.success(operation.op(self.col))

        self.controller.sync_async.assert_not_called()
        self.assertEqual(self.controller.online["review_error"], "학습 기록 저장 실패")
        self.assertTrue(self.controller.review_dirty)
        self.assertTrue(self.controller.review_allow_removals)
        self.assertTrue(self.controller.review_upload_requested)

    def test_failed_query_clears_previous_weekly_snapshot(self):
        self._install_query_op()
        self.controller._weekly_activity = {"day": "2026-10-04", "record": {}}

        self.controller.refresh_review_history()
        _QueryOp.instances[0].on_failure(RuntimeError("database unavailable"))

        self.assertIsNone(self.controller._weekly_activity)
        self.assertTrue(self.controller.review_dirty)

    def test_weekly_record_distinguishes_unknown_and_stale_snapshots(self):
        controller_type = _controller_with(
            "weekly_record", scope={"mw": self.mw}
        )
        controller = controller_type()
        controller.review_history = ReviewHistory({}, now_ms=lambda: 123)
        controller._weekly_activity = None
        self.assertIsNone(controller.weekly_record(self.current))

        controller.review_history.observe("1700000000", "2026-10-04", [])
        record = {"days": [], "answers": 0, "seconds": 0.0}
        controller._weekly_activity = {
            "day": "2026-10-04",
            "collection_key": "1700000000",
            "collection_identity": id(self.mw.col),
            "record": record,
        }
        self.assertIs(controller.weekly_record(self.current), record)

        original_collection = self.mw.col
        self.mw.col = SimpleNamespace(crt=1800000000)
        self.assertIsNone(controller.weekly_record(self.current))
        self.mw.col = original_collection
        self.assertIsNone(
            controller.weekly_record(self.current + timedelta(days=1))
        )

    def test_weekly_record_never_reads_collection_crt_on_ui_thread(self):
        class DatabaseBackedCollection:
            @property
            def crt(self):
                raise AssertionError("weekly_record must not query collection.crt")

        collection = DatabaseBackedCollection()
        controller_type = _controller_with(
            "weekly_record", scope={"mw": SimpleNamespace(col=collection)}
        )
        controller = controller_type()
        controller.review_history = ReviewHistory({}, now_ms=lambda: 123)
        controller.review_history.state["active_collection"] = "collection"
        record = {"days": [], "answers": 0, "seconds": 0.0}
        controller._weekly_activity = {
            "day": "2026-10-04",
            "collection_key": "collection",
            "collection_identity": id(collection),
            "record": record,
        }

        self.assertIs(controller.weekly_record(self.current), record)


class ReviewDeliveryIntegrationTests(TestCase):
    def setUp(self):
        self.current = datetime(2026, 10, 4, 8, 0, tzinfo=KST)
        self.scheduled = []
        self.timer_callbacks = []
        self.mw = SimpleNamespace(
            taskman=SimpleNamespace(run_in_background=self._schedule)
        )
        self.controller_type = _controller_with(
            "sync_async",
            scope={
                "now": lambda: self.current,
                "canonical_nickname": lambda uid: "ABC-DEF",
                "mw": self.mw,
                "time": time,
                "SupabaseError": SupabaseError,
                "DeviceSnapshotConflict": DeviceSnapshotConflict,
                "QTimer": SimpleNamespace(
                    singleShot=lambda delay, callback: self.timer_callbacks.append(callback)
                ),
            },
        )
        self.controller = self.controller_type()
        self.controller.closed = False
        self.controller.sync_in_flight = False
        self.controller.sync_pending = False
        self.controller.identity_generation = 4
        self.controller.online = {
            "auth": {"user_id": "user", "access_token": "token"},
            "group": {"id": "room"},
            "display_name": "ABC-DEF",
            "share_deck_name": False,
        }
        self.controller._access_token = lambda: self.controller.online["auth"]["access_token"]
        self.controller.tracker = SimpleNamespace(
            records={}, current_deck_name="Biology", status="stopped",
            today=lambda current: {"seconds": 10, "answers": 2},
            time_goal_minutes=60, card_goal=100,
        )
        self.controller.device_id = "device"
        self.controller.device_ledger = DeviceSyncLedger({})
        self.controller.sync_outbox = SyncOutbox({})
        self.controller.review_history = ReviewHistory({}, now_ms=lambda: 123)
        self.controller.review_history.observe(
            "collection", "2026-10-04", [(100, 200, 2500, 3, 1)]
        )
        self.controller.save = Mock()
        self.controller.t = lambda ko, en: ko
        self.controller.client = SimpleNamespace(
            sync_review_day=Mock(return_value=None),
            record_device_day=Mock(
                side_effect=lambda token, **kwargs: {"revision": kwargs["revision"]}
            ),
            set_current_deck=Mock(return_value=None),
            fetch_group_today=Mock(return_value=[{"user_id": "friend"}]),
            upsert_profile=Mock(return_value=None),
            refresh=Mock(),
        )

    def _schedule(self, task, done):
        self.scheduled.append((task, done))

    def _run(self):
        task, done = self.scheduled[-1]
        value = task()
        done(_Future(value))
        return value

    def test_review_batch_is_uploaded_and_acknowledged_after_success(self):
        self.controller.sync_async()
        self.assertEqual(len(self.scheduled), 1)
        self._run()

        call = self.controller.client.sync_review_day.call_args
        self.assertEqual(call.args, ("token",))
        self.assertEqual(call.kwargs["group_id"], "room")
        self.assertEqual(call.kwargs["batch"]["target_day"], "2026-10-04")
        self.assertEqual(call.kwargs["batch"]["reviews"][0]["id"], "100")
        self.assertEqual(
            self.controller.review_history.pending("user", "room"), []
        )
        self.assertEqual(self.controller.online["members"], [{"user_id": "friend"}])

    def test_failed_local_save_prevents_review_upload(self):
        self.controller.save.side_effect = OSError("disk full")
        self.controller.sync_async()

        self.assertEqual(self.scheduled, [])
        self.controller.client.sync_review_day.assert_not_called()
        self.assertIn("last_error", self.controller.online)

    def test_stale_auth_callback_never_acknowledges_review_batch(self):
        self.controller.sync_async()
        task, done = self.scheduled[0]
        value = task()
        self.controller.online["auth"]["access_token"] = "new-session"
        done(_Future(value))

        self.assertTrue(
            self.controller.review_history.pending("user", "room"),
            "the current identity must still have an unacknowledged batch",
        )
        self.assertNotIn("members", self.controller.online)

    def test_unauthorized_review_upload_refreshes_once_and_retries(self):
        self.controller.online["auth"].update(
            {"refresh_token": "refresh", "expires_at": int(time.time()) + 3600}
        )
        self.controller.client.sync_review_day.side_effect = [
            SupabaseError("expired", status=401),
            None,
        ]
        self.controller.client.refresh.return_value = {
            "access_token": "fresh-token",
            "refresh_token": "fresh-refresh",
            "expires_at": int(time.time()) + 7200,
        }

        self.controller.sync_async()
        self._run()

        self.assertEqual(self.controller.client.sync_review_day.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in self.controller.client.sync_review_day.call_args_list],
            ["token", "fresh-token"],
        )
        self.controller.client.refresh.assert_called_once_with("refresh")
        self.assertEqual(self.controller.review_history.pending("user", "room"), [])


class ReviewHookIntegrationTests(TestCase):
    def test_hooks_dirty_history_and_refresh_only_after_sync_finishes(self):
        tree = ast.parse(ADDON.read_text(encoding="utf-8"))
        names = {
            "review_history_changed", "review_sync_started",
            "review_sync_finished", "review_undone",
        }
        functions = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        fake = SimpleNamespace(
            review_dirty=False,
            review_syncing=False,
            review_upload_requested=False,
            review_allow_removals=False,
            refresh_review_history=Mock(),
        )
        scope = {"controller": fake}
        exec(compile(ast.Module(functions, type_ignores=[]), str(ADDON), "exec"), scope)

        scope["review_history_changed"]("any operation")
        self.assertTrue(fake.review_dirty)
        self.assertFalse(fake.review_allow_removals)
        scope["review_history_changed"](SimpleNamespace(card=True), None)
        self.assertTrue(fake.review_allow_removals)
        scope["review_sync_started"]()
        self.assertTrue(fake.review_syncing)
        self.assertFalse(fake.review_allow_removals)
        scope["review_history_changed"](SimpleNamespace(card=True), None)
        self.assertFalse(fake.review_allow_removals)
        fake.refresh_review_history.assert_not_called()
        scope["review_sync_finished"]()
        self.assertFalse(fake.review_syncing)
        self.assertTrue(fake.review_dirty)
        fake.refresh_review_history.assert_called_once()
        scope["review_undone"]("undo")
        self.assertTrue(fake.review_allow_removals)
        self.assertTrue(fake.review_upload_requested)
