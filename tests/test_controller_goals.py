import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from study_companion.ux_services import (
    ANSWER_GOAL_MAX,
    TIME_GOAL_MAX_MINUTES,
    validate_goal,
)
from study_companion.record_status import LOCAL_SAVE, RecordStatusLedger


class ControllerGoalTests(unittest.TestCase):
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
            if isinstance(node, ast.FunctionDef)
            and node.name in ("save", "update_daily_goals", "update_local_settings")
        ]
        scope = {
            "json": json,
            "os": os,
            "_atomic_write_text": None,
            "LOCAL_SAVE": LOCAL_SAVE,
            "validate_goal": validate_goal,
            "TIME_GOAL_MAX_MINUTES": TIME_GOAL_MAX_MINUTES,
            "ANSWER_GOAL_MAX": ANSWER_GOAL_MAX,
        }
        # The wrapper is supplied directly here so save() remains isolated
        # from Anki imports while retaining atomic replacement semantics.
        scope["_atomic_write_text"] = lambda path, payload: path.write_text(payload, encoding="utf-8")
        exec(compile(ast.Module(body=[controller], type_ignores=[]), str(path), "exec"), scope)
        cls.Controller = scope["Controller"]

    def make_controller(self):
        controller = self.Controller()
        controller.tracker = SimpleNamespace(time_goal_minutes=60, card_goal=100)
        controller.locale = "ko"
        controller.online = {}
        controller.save = Mock()
        controller.refresh = Mock()
        controller.sync_async = Mock()
        return controller

    def test_partial_goal_change_preserves_the_other_goal_and_starts_sync(self):
        controller = self.make_controller()
        self.assertTrue(controller.update_daily_goals(card_goal=125))
        self.assertEqual(controller.tracker.time_goal_minutes, 60)
        self.assertEqual(controller.tracker.card_goal, 125)
        controller.save.assert_called_once_with()
        controller.refresh.assert_called_once_with()
        controller.sync_async.assert_called_once_with(force=True)

    def test_unchanged_goal_does_not_write_or_sync(self):
        controller = self.make_controller()
        self.assertFalse(controller.update_daily_goals(time_goal_minutes=60))
        controller.save.assert_not_called()
        controller.sync_async.assert_not_called()

    def test_save_failure_rolls_back_both_values(self):
        controller = self.make_controller()
        controller.save.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            controller.update_daily_goals(time_goal_minutes=90, card_goal=200)
        self.assertEqual(controller.tracker.time_goal_minutes, 60)
        self.assertEqual(controller.tracker.card_goal, 100)
        controller.refresh.assert_not_called()
        controller.sync_async.assert_not_called()

    def test_local_save_success_is_persisted_and_serialization_failure_is_transient(self):
        controller = self.Controller()
        with tempfile.TemporaryDirectory() as directory:
            controller.path = Path(directory) / "state.json"
            controller.online = {"record_status": {}}
            controller.record_status = RecordStatusLedger(
                controller.online["record_status"], clock=lambda: 123
            )
            controller.tracker = SimpleNamespace(snapshot=lambda: {"records": {}})
            controller.locale = "ko"
            controller.ui_state = {}
            controller.review_history = SimpleNamespace(state={})
            controller.study_day_scheme = "room-04-v1|Asia/Seoul"
            controller.legacy_tracker_records = {}

            controller.save()
            written = json.loads(controller.path.read_text(encoding="utf-8"))
            self.assertEqual(
                written["online"]["record_status"]["success"]["local|local_save"],
                123,
            )

            controller.ui_state = {"bad": object()}
            with self.assertRaises(TypeError):
                controller.save()
            self.assertEqual(controller.record_status.success_at(LOCAL_SAVE), 123)
            self.assertEqual(
                controller.record_status.snapshot()["primary_issue"], LOCAL_SAVE
            )


if __name__ == "__main__":
    unittest.main()
