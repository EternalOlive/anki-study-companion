import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from study_companion.online import SupabaseError


class RoomLeaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]

        settings_source = root / "study_companion" / "settings.py"
        settings_tree = ast.parse(settings_source.read_text(encoding="utf-8"))
        helper = next(
            node for node in settings_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_already_left_error"
        )
        settings_scope = {}
        exec(
            compile(ast.Module(body=[helper], type_ignores=[]), str(settings_source), "exec"),
            settings_scope,
        )
        cls.already_left_error = staticmethod(settings_scope["_already_left_error"])

        addon_source = root / "study_companion" / "addon.py"
        addon_tree = ast.parse(addon_source.read_text(encoding="utf-8"))
        controller = next(
            node for node in addon_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Controller"
        )
        controller.body = [
            node for node in controller.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "leave_current_room_locally"
        ]
        addon_scope = {"DEFAULT_TIME_ZONE": "Asia/Seoul"}
        exec(
            compile(ast.Module(body=[controller], type_ignores=[]), str(addon_source), "exec"),
            addon_scope,
        )
        cls.Controller = addon_scope["Controller"]

    def make_controller(self):
        controller = self.Controller()
        controller.online = {
            "auth": {"user_id": "user-a"},
            "group": {"id": "room-a"},
            "members": [{"user_id": "user-a"}],
            "published_deck": {"group_id": "room-a", "name": "Deck"},
        }
        controller.sync_outbox = SimpleNamespace(discard_room=Mock(return_value=2))
        controller.review_history = SimpleNamespace(invalidate_route=Mock())
        controller._member_cache_key = ("cached",)
        controller._last_member_fetch_at = 123
        controller.apply_room_time_zone = Mock()
        controller.save = Mock()
        controller.refresh = Mock()
        return controller

    def test_only_explicit_not_member_error_is_idempotent(self):
        self.assertTrue(
            self.already_left_error(SupabaseError("not a member of this group", status=403))
        )
        self.assertFalse(self.already_left_error(SupabaseError("permission denied", status=403)))
        self.assertFalse(self.already_left_error(SupabaseError("", status=403)))
        self.assertFalse(
            self.already_left_error(SupabaseError("not a member of another group", status=403))
        )

    def test_matching_room_cleanup_is_scoped_and_saved_once(self):
        controller = self.make_controller()
        self.assertTrue(controller.leave_current_room_locally("user-a", "room-a"))
        controller.sync_outbox.discard_room.assert_called_once_with("user-a", "room-a")
        controller.review_history.invalidate_route.assert_called_once_with("user-a", "room-a")
        self.assertNotIn("group", controller.online)
        self.assertNotIn("members", controller.online)
        self.assertNotIn("published_deck", controller.online)
        self.assertIsNone(controller._member_cache_key)
        self.assertEqual(controller._last_member_fetch_at, 0)
        controller.apply_room_time_zone.assert_called_once_with("Asia/Seoul")
        controller.save.assert_called_once_with()
        controller.refresh.assert_called_once_with()

    def test_stale_room_or_account_cannot_clear_current_room(self):
        for user_id, group_id in (("user-b", "room-a"), ("user-a", "room-b")):
            with self.subTest(user_id=user_id, group_id=group_id):
                controller = self.make_controller()
                self.assertFalse(controller.leave_current_room_locally(user_id, group_id))
                self.assertEqual(controller.online["group"]["id"], "room-a")
                controller.sync_outbox.discard_room.assert_not_called()
                controller.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
