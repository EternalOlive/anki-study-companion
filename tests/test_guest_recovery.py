import ast
from pathlib import Path
import unittest
from unittest.mock import Mock


class ExpiredGuestRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        controller = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Controller"
        )
        controller.body = [
            node for node in controller.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "restart_expired_guest"
        ]
        scope = {"DEFAULT_TIME_ZONE": "Asia/Seoul"}
        exec(
            compile(ast.Module(body=[controller], type_ignores=[]), str(source), "exec"),
            scope,
        )
        cls.Controller = scope["Controller"]

    def make_controller(self, online):
        controller = self.Controller()
        controller.config_read_error = None
        controller.online = online
        controller._access_token = lambda: (
            controller.online.get("auth") or {}
        ).get("access_token")
        controller._cancel_identity_bootstrap = Mock()
        controller.save = Mock()
        controller.refresh = Mock()
        controller.ensure_online_identity = Mock()
        controller.apply_room_time_zone = Mock()
        controller._member_cache_key = "cached"
        controller._last_member_fetch_at = 123
        return controller

    def test_restarts_only_expired_unlinked_guest_and_preserves_scoped_data(self):
        controller = self.make_controller({
            "guest_id": "old-user",
            "account_kind": "guest",
            "display_name": "OLD-USER",
            "auth": {"access_token": "", "user_id": "old-user"},
            "group": {"id": "old-room"},
            "members": [{"user_id": "old-user"}],
            "published_deck": {"group_id": "old-room", "name": "Deck"},
            "sync_outbox": {"old-user|old-room|2026-10-04": {"answer_count": 2}},
            "local_preference": "kept",
            "last_error": "expired",
        })

        self.assertTrue(controller.restart_expired_guest())
        self.assertNotIn("guest_id", controller.online)
        self.assertNotIn("group", controller.online)
        self.assertNotIn("published_deck", controller.online)
        self.assertEqual(controller.online["local_preference"], "kept")
        self.assertIn("old-user|old-room|2026-10-04", controller.online["sync_outbox"])
        controller.save.assert_called_once_with()
        controller.ensure_online_identity.assert_called_once_with()

    def test_save_failure_restores_the_old_identity(self):
        original = {"guest_id": "old-user", "account_kind": "guest"}
        controller = self.make_controller(original)
        controller.save.side_effect = OSError("locked")
        with self.assertRaises(OSError):
            controller.restart_expired_guest()
        self.assertIs(controller.online, original)
        controller.ensure_online_identity.assert_not_called()

    def test_does_not_replace_linked_or_temporarily_offline_identity(self):
        for state in (
            {"guest_id": "user", "account_kind": "guest", "auth": {"access_token": "live"}},
            {"guest_id": "user", "account_kind": "username", "username": "reader"},
            {"account_kind": "guest"},
        ):
            with self.subTest(state=state):
                controller = self.make_controller(state)
                self.assertFalse(controller.restart_expired_guest())
                controller.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
