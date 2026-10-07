"""Tests for room management features: i18n, API RPC calls, settings dialog, and sync kick handling."""

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlparse

from study_companion import i18n
from study_companion import api
from study_companion.api import SupabaseClient, SupabaseError
from study_companion import sync
from study_companion.settings_dialog import SettingsDialog


class FakeResponse:
    def __init__(self, payload=None):
        self.raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self):
        return self.raw

    def close(self):
        pass


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)


def body_of(request):
    return json.loads(request.data.decode("utf-8"))


class I18nTests(unittest.TestCase):
    def test_supported_locales_present(self):
        for locale in ("ko", "en", "ja", "zh_CN"):
            self.assertIn(locale, i18n.SUPPORTED_LOCALES)
            self.assertIn(locale, i18n.LOCALE_NAMES)

    def test_required_translation_keys_present_in_all_locales(self):
        required_keys = (
            "change_timezone",
            "transfer_owner",
            "kick_member",
            "cleanup_inactive",
            "kicked_notification",
            "member_management",
            "room_timezone",
        )
        for locale in ("ko", "en", "ja", "zh_CN"):
            for key in required_keys:
                text = i18n.t(key, locale=locale)
                self.assertTrue(text, f"Key {key!r} missing or empty for locale {locale!r}")
                self.assertNotEqual(text, key, f"Key {key!r} untranslated in locale {locale!r}")

    def test_tr_helper_resolves_all_four_languages(self):
        ko = "한국어"
        en = "English"
        ja = "日本語"
        zh = "简体中文"
        self.assertEqual(i18n.tr(ko, en, ja, zh, locale="ko"), ko)
        self.assertEqual(i18n.tr(ko, en, ja, zh, locale="en"), en)
        self.assertEqual(i18n.tr(ko, en, ja, zh, locale="ja"), ja)
        self.assertEqual(i18n.tr(ko, en, ja, zh, locale="zh_CN"), zh)

    def test_locale_normalization(self):
        self.assertEqual(i18n.normalize_locale("ja-JP"), "ja")
        self.assertEqual(i18n.normalize_locale("zh-CN"), "zh_CN")
        self.assertEqual(i18n.normalize_locale("en-US"), "en")
        self.assertEqual(i18n.normalize_locale("ko-KR"), "ko")
        self.assertEqual(i18n.normalize_locale(None), "ko")
        self.assertEqual(i18n.normalize_locale("unknown"), "ko")

    def test_interpolation_with_params(self):
        msg = i18n.t("transfer_owner_confirm", locale="ko", name="민수")
        self.assertIn("민수", msg)
        msg_en = i18n.t("transfer_owner_confirm", locale="en", name="Alice")
        self.assertIn("Alice", msg_en)


class ApiRpcTests(unittest.TestCase):
    def test_update_room_timezone_rpc(self):
        opener = FakeOpener([None])
        client = SupabaseClient(opener=opener)
        api.update_room_timezone(client, "token-123", "group-456", "Asia/Tokyo")

        req = opener.calls[0][0]
        self.assertTrue(urlparse(req.full_url).path.endswith("/rpc/update_room_timezone"))
        body = body_of(req)
        self.assertEqual(body["target_group"], "group-456")
        self.assertEqual(body["new_timezone"], "Asia/Tokyo")

    def test_update_room_public_rpc(self):
        opener = FakeOpener([None])
        client = SupabaseClient(opener=opener)
        api.update_room_public(client, "token-123", "group-456", True)

        req = opener.calls[0][0]
        self.assertTrue(urlparse(req.full_url).path.endswith("/rpc/update_room_public"))
        body = body_of(req)
        self.assertEqual(body["target_group"], "group-456")
        self.assertTrue(body["new_is_public"])

    def test_transfer_room_ownership_rpc(self):
        opener = FakeOpener([None])
        client = SupabaseClient(opener=opener)
        api.transfer_room_ownership(client, "token-123", "group-456", "user-789")

        req = opener.calls[0][0]
        self.assertTrue(urlparse(req.full_url).path.endswith("/rpc/transfer_room_ownership"))
        body = body_of(req)
        self.assertEqual(body["target_group"], "group-456")
        self.assertEqual(body["new_owner_id"], "user-789")

    def test_kick_room_member_rpc(self):
        opener = FakeOpener([None])
        client = SupabaseClient(opener=opener)
        api.kick_room_member(client, "token-123", "group-456", "user-bad")

        req = opener.calls[0][0]
        self.assertTrue(urlparse(req.full_url).path.endswith("/rpc/kick_room_member"))
        body = body_of(req)
        self.assertEqual(body["target_group"], "group-456")
        self.assertEqual(body["target_user"], "user-bad")

    def test_cleanup_inactive_members_rpc(self):
        opener = FakeOpener([3])
        client = SupabaseClient(opener=opener)
        removed_count = api.cleanup_inactive_members(client, "token-123", "group-456", days=14)

        self.assertEqual(removed_count, 3)
        req = opener.calls[0][0]
        self.assertTrue(urlparse(req.full_url).path.endswith("/rpc/cleanup_inactive_members"))
        body = body_of(req)
        self.assertEqual(body["target_group"], "group-456")
        self.assertEqual(body["days_inactive"], 14)

    def test_list_public_study_groups_rpc(self):
        fake_rooms = [
            {"id": "g-1", "name": "Room 1", "invite_code": "ABCD", "time_zone": "Asia/Seoul", "member_count": 4, "studying_count": 2}
        ]
        opener = FakeOpener([fake_rooms])
        client = SupabaseClient(opener=opener)
        rooms = api.list_public_study_groups(client, "token-123", "Asia/Seoul")

        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["name"], "Room 1")
        req = opener.calls[0][0]
        self.assertTrue(urlparse(req.full_url).path.endswith("/rpc/list_public_study_groups"))
        body = body_of(req)
        self.assertEqual(body["user_timezone"], "Asia/Seoul")

    def test_create_group_with_is_public(self):
        created_payload = [
            {"group_id": "g-new", "invite_code": "WXYZ", "time_zone": "Asia/Tokyo", "is_public": True}
        ]
        opener = FakeOpener([created_payload])
        client = SupabaseClient(opener=opener)
        res = client.create_group("token-123", "Public Room", "Asia/Tokyo", is_public=True)

        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "g-new")
        self.assertTrue(res["is_public"])
        req = opener.calls[0][0]
        body = body_of(req)
        self.assertTrue(body["room_is_public"])


class SyncKickDetectionTests(unittest.TestCase):
    def test_is_kicked_error_patterns(self):
        self.assertTrue(sync.is_kicked_error(SupabaseError("not a member of this group")))
        self.assertTrue(sync.is_kicked_error(SupabaseError("Blocked from this group")))
        self.assertTrue(sync.is_kicked_error(Exception("Kicked from room")))
        self.assertTrue(sync.is_kicked_error(RuntimeError("Member not found")))
        self.assertFalse(sync.is_kicked_error(SupabaseError("network timeout")))
        self.assertFalse(sync.is_kicked_error(None))

    def test_is_member_missing(self):
        members = [
            {"user_id": "user-1", "display_name": "Alice"},
            {"user_id": "user-2", "display_name": "Bob"},
        ]
        self.assertFalse(sync.is_member_missing(members, "user-1"))
        self.assertFalse(sync.is_member_missing(members, "user-2"))
        self.assertTrue(sync.is_member_missing(members, "user-kicked"))
        self.assertFalse(sync.is_member_missing([], "user-kicked"))
        self.assertFalse(sync.is_member_missing(None, "user-kicked"))

    def test_handle_kicked_state(self):
        controller = SimpleNamespace(
            locale="ko",
            online={
                "group": {"id": "room-1"},
                "members": [{"user_id": "other"}],
                "recovery_notice": None,
            },
            refreshed=False,
            left_calls=[],
        )
        def fake_leave(u, g):
            controller.left_calls.append((u, g))
            controller.online["group"] = None
        controller.leave_current_room_locally = fake_leave
        controller.refresh = lambda: setattr(controller, "refreshed", True)

        sync.handle_kicked_state(controller, "user-me", "room-1")

        self.assertEqual(controller.left_calls, [("user-me", "room-1")])
        self.assertIsNone(controller.online["group"])
        self.assertIn("방에서 내보내졌습니다", controller.online["recovery_notice"])
        self.assertTrue(controller.refreshed)


class MigrationFileCheckTests(unittest.TestCase):
    def test_migration_exists_and_contains_required_functions(self):
        migration_file = Path(__file__).parents[1] / "supabase" / "migrations" / "20261006_room_management.sql"
        self.assertTrue(migration_file.exists(), "Migration file does not exist")
        sql = migration_file.read_text(encoding="utf-8")
        self.assertIn("update_room_timezone", sql)
        self.assertIn("transfer_room_ownership", sql)
        self.assertIn("kick_room_member", sql)
        self.assertIn("cleanup_inactive_members", sql)
        self.assertIn("guard_study_group_calendar", sql)

    def test_update_room_public_migration_exists(self):
        migration_file = Path(__file__).parents[1] / "supabase" / "migrations" / "20261007_update_room_public.sql"
        self.assertTrue(migration_file.exists(), "Update room public migration does not exist")
        sql = migration_file.read_text(encoding="utf-8")
        self.assertIn("update_room_public", sql)
        self.assertIn("35FU", sql)


class SettingsDialogExportTests(unittest.TestCase):
    def test_settings_dialog_exports_cleanly(self):
        from study_companion import settings_dialog
        self.assertIn("SettingsDialog", settings_dialog.__all__)
        if SettingsDialog is not None:
            self.assertTrue(hasattr(SettingsDialog, "PAGE_HOME"))
            self.assertTrue(hasattr(SettingsDialog, "TAB_ROOM"))
            self.assertTrue(hasattr(SettingsDialog, "change_room_timezone"))
            self.assertTrue(hasattr(SettingsDialog, "toggle_room_public"))
            self.assertTrue(hasattr(SettingsDialog, "transfer_ownership"))
            self.assertTrue(hasattr(SettingsDialog, "kick_member"))
            self.assertTrue(hasattr(SettingsDialog, "cleanup_inactive"))


if __name__ == "__main__":
    unittest.main()
