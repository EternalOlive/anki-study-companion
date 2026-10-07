"""Unit tests for collapsed study strip controller logic and preferences."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from study_companion.ux_services import (
    ANSWER_GOAL_MAX,
    TIME_GOAL_MAX_MINUTES,
    validate_goal,
)


class CollapsedStripControllerTests(unittest.TestCase):
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
            and node.name in (
                "save",
                "update_local_settings",
                "is_panel_collapsed",
                "refresh_collapsed_strip",
            )
        ]
        scope = {
            "validate_goal": validate_goal,
            "TIME_GOAL_MAX_MINUTES": TIME_GOAL_MAX_MINUTES,
            "ANSWER_GOAL_MAX": ANSWER_GOAL_MAX,
        }
        exec(compile(ast.Module(body=[controller], type_ignores=[]), str(path), "exec"), scope)
        cls.Controller = scope["Controller"]

    def make_controller(self, in_room=True, collapsed=True, enabled=True):
        controller = self.Controller()
        controller.tracker = SimpleNamespace(time_goal_minutes=60, card_goal=100)
        controller.locale = "ko"
        controller.online = {"group": {"id": "room-1"} if in_room else None}
        controller.ui_state = {
            "panel_collapsed": collapsed,
            "show_collapsed_strip": enabled,
        }
        controller.panel = SimpleNamespace(isVisible=lambda: not collapsed)
        badge = Mock()
        badge.update_state = Mock()
        badge.show = Mock()
        badge.hide = Mock()
        controller.mini_study_badge = badge
        controller.save = Mock()
        controller.refresh = Mock()
        controller.sync_async = Mock()
        return controller

    def test_show_collapsed_strip_setting_toggle(self):
        controller = self.make_controller()
        self.assertTrue(controller.update_local_settings(show_collapsed_strip=False))
        self.assertFalse(controller.ui_state["show_collapsed_strip"])
        controller.save.assert_called_once()
        controller.sync_async.assert_called_once_with(force=True)

    def test_unchanged_show_collapsed_strip_does_not_save(self):
        controller = self.make_controller()
        self.assertFalse(controller.update_local_settings(show_collapsed_strip=True))
        controller.save.assert_not_called()

    def test_invalid_show_collapsed_strip_raises_value_error(self):
        controller = self.make_controller()
        with self.assertRaises(ValueError):
            controller.update_local_settings(show_collapsed_strip="not-a-bool")

    def test_save_error_rolls_back_show_collapsed_strip(self):
        controller = self.make_controller(enabled=True)
        controller.save.side_effect = OSError("write failed")
        with self.assertRaises(OSError):
            controller.update_local_settings(show_collapsed_strip=False)
        self.assertTrue(controller.ui_state["show_collapsed_strip"])

    def test_refresh_collapsed_strip_shows_when_in_room_collapsed_and_enabled(self):
        controller = self.make_controller(in_room=True, collapsed=True, enabled=True)
        controller.refresh_collapsed_strip()
        controller.mini_study_badge.update_state.assert_called_once()
        controller.mini_study_badge.show.assert_called_once()
        controller.mini_study_badge.hide.assert_not_called()

    def test_refresh_collapsed_strip_hides_when_not_in_room(self):
        controller = self.make_controller(in_room=False, collapsed=True, enabled=True)
        controller.refresh_collapsed_strip()
        controller.mini_study_badge.hide.assert_called_once()
        controller.mini_study_badge.show.assert_not_called()

    def test_refresh_collapsed_strip_hides_when_panel_is_not_collapsed(self):
        controller = self.make_controller(in_room=True, collapsed=False, enabled=True)
        controller.refresh_collapsed_strip()
        controller.mini_study_badge.hide.assert_called_once()
        controller.mini_study_badge.show.assert_not_called()

    def test_refresh_collapsed_strip_hides_when_setting_is_disabled(self):
        controller = self.make_controller(in_room=True, collapsed=True, enabled=False)
        controller.refresh_collapsed_strip()
        controller.mini_study_badge.hide.assert_called_once()
        controller.mini_study_badge.show.assert_not_called()

    def test_do_not_disturb_setting_toggle(self):
        controller = self.make_controller()
        self.assertTrue(controller.update_local_settings(do_not_disturb=True))
        self.assertTrue(controller.ui_state["do_not_disturb"])
        controller.save.assert_called_once()
        controller.sync_async.assert_called_once_with(force=True)

    def test_unchanged_do_not_disturb_does_not_save(self):
        controller = self.make_controller()
        controller.ui_state["do_not_disturb"] = True
        self.assertFalse(controller.update_local_settings(do_not_disturb=True))
        controller.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
