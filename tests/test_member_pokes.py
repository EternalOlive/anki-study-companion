"""Tests for room member pokes (찌르기): client RPCs, panel action, fail-soft sync."""

import ast
import io
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlparse

import test_offline_sync as offline
from study_companion import api
from study_companion.online import PokeUnavailable, SupabaseClient, SupabaseError
from study_companion.pokes import (
    POKE_COOLDOWN_SECONDS,
    POKE_HOURLY_LIMIT,
    POKE_UNAVAILABLE_RETRY_SECONDS,
    poke_message,
)


ROOT = Path(__file__).parents[1]


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


def http_error(name, status, payload):
    return HTTPError(
        f"https://example/rest/v1/rpc/{name}", status, "error", {},
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


class PokeClientTests(unittest.TestCase):
    def test_poke_room_member_calls_authenticated_rpc(self):
        opener = FakeOpener(["2026-10-06T10:00:00+00:00"])
        created = api.poke_room_member(
            SupabaseClient(opener=opener), "token-1", "group-1", " user-2 "
        )

        self.assertEqual(created, "2026-10-06T10:00:00+00:00")
        request = opener.calls[0][0]
        self.assertTrue(urlparse(request.full_url).path.endswith("/rest/v1/rpc/poke_room_member"))
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-1")
        self.assertEqual(body_of(request), {"target_group": "group-1", "target_user": "user-2"})

    def test_poke_rate_limits_have_bilingual_429_errors(self):
        for server_message, korean, english in (
            ("poke too soon", "1분 뒤에", "Try again in a minute"),
            ("too many pokes", "너무 많이", "Too many pokes"),
        ):
            error = http_error("poke_room_member", 400, {"code": "P0001", "message": server_message})
            with self.subTest(server_message=server_message):
                with self.assertRaises(SupabaseError) as caught:
                    SupabaseClient(opener=FakeOpener([error])).poke_room_member("t", "g", "u")
                self.assertEqual(caught.exception.status, 429)
                self.assertIn(korean, str(caught.exception))
                self.assertIn(english, str(caught.exception))
                self.assertNotIsInstance(caught.exception, PokeUnavailable)

    def test_poke_target_and_self_errors_are_mapped(self):
        cases = (
            ("target is not in this group", 409),
            ("cannot poke yourself", 400),
        )
        for server_message, status in cases:
            error = http_error("poke_room_member", 400, {"message": server_message})
            with self.subTest(server_message=server_message):
                with self.assertRaises(SupabaseError) as caught:
                    SupabaseClient(opener=FakeOpener([error])).poke_room_member("t", "g", "u")
                self.assertEqual(caught.exception.status, status)
                self.assertIn(" / ", str(caught.exception))

    def test_other_poke_errors_pass_through(self):
        error = http_error("poke_room_member", 400, {"message": "not a member of this group"})
        with self.assertRaises(SupabaseError) as caught:
            SupabaseClient(opener=FakeOpener([error])).poke_room_member("t", "g", "u")
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("not a member of this group", str(caught.exception))

    def test_missing_rpc_raises_poke_unavailable(self):
        missing = [
            http_error("poke_room_member", 404, {
                "code": "PGRST202",
                "message": "Could not find the function public.poke_room_member(target_group, target_user) in the schema cache",
            }),
            # Some proxies rewrite the status; the PostgREST message still identifies it.
            http_error("poke_room_member", 400, {
                "message": "Could not find the function public.poke_room_member in the schema cache",
            }),
        ]
        for error in missing:
            with self.subTest(status=error.code):
                with self.assertRaises(PokeUnavailable) as caught:
                    SupabaseClient(opener=FakeOpener([error])).poke_room_member("t", "g", "u")
                self.assertEqual(caught.exception.status, 404)

    def test_fetch_my_pokes_returns_rows_and_defaults_since_on_server(self):
        rows = [
            {"id": 7, "from_user": "user-a", "created_at": "2026-10-06T10:00:00+00:00"},
            {"id": 8, "from_user": None, "created_at": "2026-10-06T10:00:01+00:00"},
            "garbage",
        ]
        opener = FakeOpener([rows])
        pokes = api.fetch_my_pokes(SupabaseClient(opener=opener), "token-1", "group-1")

        self.assertEqual(pokes, [{"id": 7, "from_user": "user-a", "created_at": "2026-10-06T10:00:00+00:00"}])
        request = opener.calls[0][0]
        self.assertTrue(urlparse(request.full_url).path.endswith("/rest/v1/rpc/fetch_my_pokes"))
        self.assertEqual(body_of(request), {"target_group": "group-1"})

    def test_fetch_my_pokes_sends_since_when_given_and_handles_empty(self):
        opener = FakeOpener([None, []])
        client = SupabaseClient(opener=opener)
        self.assertEqual(client.fetch_my_pokes("t", "g", since="2026-10-06T09:00:00Z"), [])
        self.assertEqual(client.fetch_my_pokes("t", "g"), [])
        self.assertEqual(
            body_of(opener.calls[0][0]),
            {"target_group": "g", "since": "2026-10-06T09:00:00Z"},
        )

    def test_fetch_my_pokes_missing_rpc_and_bad_shape(self):
        error = http_error("fetch_my_pokes", 404, {"code": "PGRST202", "message": "Could not find the function"})
        with self.assertRaises(PokeUnavailable):
            SupabaseClient(opener=FakeOpener([error])).fetch_my_pokes("t", "g")
        with self.assertRaises(SupabaseError):
            SupabaseClient(opener=FakeOpener([{"oops": True}])).fetch_my_pokes("t", "g")

    def test_api_exports(self):
        for name in ("poke_room_member", "fetch_my_pokes", "PokeUnavailable"):
            self.assertIn(name, api.__all__)


class PokeMessageTests(unittest.TestCase):
    def setUp(self):
        self.names = {"a": "K7M-2RX", "b": "T4N-8WA"}

    def message(self, pokes, locale="ko"):
        return poke_message(
            pokes,
            lambda user_id: self.names.get(user_id, user_id),
            lambda ko, en: en if locale == "en" else ko,
        )

    def test_single_poke(self):
        self.assertEqual(self.message([{"from_user": "a"}]), "K7M-2RX님이 콕 찔렀어요")
        self.assertEqual(self.message([{"from_user": "a"}], "en"), "K7M-2RX poked you")

    def test_repeated_and_multiple_senders(self):
        pokes = [{"from_user": "a"}, {"from_user": "b"}, {"from_user": "a"}]
        self.assertEqual(self.message(pokes), "K7M-2RX님(2번), T4N-8WA님이 콕 찔렀어요")
        self.assertEqual(self.message(pokes, "en"), "K7M-2RX (×2), T4N-8WA poked you")

    def test_nothing_to_show(self):
        self.assertIsNone(self.message([]))
        self.assertIsNone(self.message([{"from_user": ""}, "bad"]))


def _load_class_methods(relative_path, class_name, method_names, scope):
    """Compile selected methods of a Qt/Anki class without importing Qt/Anki."""
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    found = {
        item.name for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name in method_names
    }
    missing = set(method_names) - found
    if missing:
        raise AssertionError(f"{class_name} is missing {sorted(missing)}")
    node.body = [
        item for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name in method_names
    ]
    node.bases = []
    namespace = {"__package__": "study_companion", **scope}  # addon.py uses relative imports
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[class_name]


class PanelPokeActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Panel = _load_class_methods(
            "study_companion/panel.py", "StudyPanel",
            {"pokes_enabled", "can_poke", "poke_cooling", "poke_member"},
            {"time": time, "POKE_COOLDOWN_SECONDS": POKE_COOLDOWN_SECONDS},
        )

    def make_panel(self, *, available=True, group=True):
        poked = []
        controller = SimpleNamespace(
            online={
                "auth": {"user_id": "me"},
                "group": {"id": "room-1"} if group else None,
            },
            pokes_available=lambda: available,
            poke_member=lambda member: poked.append(member["user_id"]) or True,
        )
        panel = self.Panel()
        panel.controller = controller
        panel.poke_cooldowns = {}
        panel.member_rows = {}
        return panel, poked

    def test_action_only_for_other_members_in_a_room(self):
        panel, _ = self.make_panel()
        self.assertTrue(panel.can_poke({"user_id": "friend"}))
        self.assertFalse(panel.can_poke({"user_id": "me"}))
        self.assertFalse(panel.can_poke({"display_name": "no id"}))
        no_room, _ = self.make_panel(group=False)
        self.assertFalse(no_room.can_poke({"user_id": "friend"}))

    def test_action_hidden_when_server_lacks_pokes(self):
        panel, poked = self.make_panel(available=False)
        self.assertFalse(panel.pokes_enabled())
        self.assertFalse(panel.can_poke({"user_id": "friend"}))
        self.assertFalse(panel.poke_member({"user_id": "friend"}))
        self.assertEqual(poked, [])

    def test_poke_disables_that_friend_for_the_cooldown(self):
        panel, poked = self.make_panel()
        self.assertTrue(panel.poke_member({"user_id": "friend"}))
        self.assertTrue(panel.poke_cooling({"user_id": "friend"}))
        self.assertFalse(panel.poke_cooling({"user_id": "other"}))
        self.assertFalse(panel.poke_member({"user_id": "friend"}))
        self.assertEqual(poked, ["friend"])
        remaining = panel.poke_cooldowns["friend"] - time.monotonic()
        self.assertGreater(remaining, POKE_COOLDOWN_SECONDS - 5)
        self.assertLessEqual(remaining, POKE_COOLDOWN_SECONDS)

    def test_member_row_has_a_poke_button_wired_to_the_panel(self):
        tree = ast.parse((ROOT / "study_companion" / "panel.py").read_text(encoding="utf-8"))
        row = next(
            item for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == "MemberRow"
        )
        methods = {item.name: item for item in row.body if isinstance(item, ast.FunctionDef)}
        init_source = ast.unparse(methods["__init__"])
        self.assertIn("self.poke = QToolButton(self)", init_source)
        self.assertIn("self.panel.poke_member(self.member)", init_source)
        self.assertIn("update_poke", methods)
        self.assertIn("self.update_poke()", ast.unparse(methods["update_member"]))
        self.assertIn("can_poke", ast.unparse(methods["update_poke"]))


class ControllerPokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tooltips = []
        cls.Controller = _load_class_methods(
            "study_companion/addon.py", "Controller",
            {"pokes_available", "_disable_pokes", "poke_member", "show_pokes", "is_do_not_disturb"},
            {
                "time": time,
                "tooltip": lambda message, period=3000: cls.tooltips.append(message),
                "PokeUnavailable": PokeUnavailable,
                "POKE_UNAVAILABLE_RETRY_SECONDS": POKE_UNAVAILABLE_RETRY_SECONDS,
                "poke_message": poke_message,
            },
        )

    def setUp(self):
        self.tooltips.clear()
        controller = self.Controller()
        controller.ui_state = {"do_not_disturb": False}
        controller.online = {
            "auth": {"user_id": "me"},
            "group": {"id": "room-1"},
            "members": [{"user_id": "friend", "display_name": "K7M-2RX"}],
        }
        controller.t = lambda ko, en: ko
        controller.display_member_name = lambda member: member.get("display_name") or f"code:{member['user_id']}"
        controller.refreshed = 0
        controller.refresh_panel = lambda: setattr(controller, "refreshed", controller.refreshed + 1)
        controller.errors = []

        def run(controls, operation, success, error_prefix, *, on_error=None):
            try:
                result = operation("token")
            except SupabaseError as error:
                on_error(f"{error_prefix}\n{error}")
                return
            success(result)

        controller._run_authenticated_action = run
        self.controller = controller

    def test_poke_hourly_limit_is_sixty(self):
        self.assertEqual(POKE_HOURLY_LIMIT, 60)

    def test_poke_sends_and_confirms_with_tooltip(self):
        calls = []
        self.controller.client = SimpleNamespace(
            poke_room_member=lambda token, group, user: calls.append((group, user)) or "now"
        )
        self.assertTrue(self.controller.poke_member({"user_id": "friend", "display_name": "K7M-2RX"}))
        self.assertEqual(calls, [("room-1", "friend")])
        self.assertEqual(self.tooltips, ["K7M-2RX님을 콕 찔렀어요"])

    def test_poke_refuses_own_row_and_missing_room(self):
        self.controller.client = SimpleNamespace(poke_room_member=lambda *args: self.fail("sent"))
        self.assertFalse(self.controller.poke_member({"user_id": "me"}))
        self.controller.online["group"] = None
        self.assertFalse(self.controller.poke_member({"user_id": "friend"}))

    def test_poke_refuses_member_in_do_not_disturb(self):
        self.controller.client = SimpleNamespace(poke_room_member=lambda *args: self.fail("sent"))
        self.assertFalse(self.controller.poke_member({"user_id": "friend", "display_name": "K7M-2RX", "dnd": True}))
        self.assertEqual(self.tooltips, ["K7M-2RX 님은 방해 금지 모드 중입니다."])

    def test_missing_server_rpc_silently_disables_pokes(self):
        def missing(*args):
            raise PokeUnavailable()

        self.controller.client = SimpleNamespace(poke_room_member=missing)
        self.assertTrue(self.controller.pokes_available())
        self.controller.poke_member({"user_id": "friend"})
        self.assertFalse(self.controller.pokes_available())
        self.assertEqual(self.tooltips, [])
        self.assertEqual(self.controller.refreshed, 1)
        self.assertFalse(self.controller.poke_member({"user_id": "friend"}))

    def test_rate_limit_shows_non_blocking_tooltip(self):
        def too_soon(*args):
            raise SupabaseError("방금 찔렀어요. / You just poked them.", status=429)

        self.controller.client = SimpleNamespace(poke_room_member=too_soon)
        self.controller.poke_member({"user_id": "friend"})
        self.assertEqual(len(self.tooltips), 1)
        self.assertIn("방금 찔렀어요", self.tooltips[0])
        self.assertTrue(self.controller.pokes_available())

    def test_received_pokes_use_member_display_names(self):
        self.controller.show_pokes([
            {"from_user": "friend"}, {"from_user": "gone"},
        ])
        self.assertEqual(self.tooltips, ["K7M-2RX님, code:gone님이 콕 찔렀어요"])
        self.tooltips.clear()
        self.controller.show_pokes([])
        self.assertEqual(self.tooltips, [])

    def test_received_pokes_suppressed_in_do_not_disturb(self):
        self.controller.ui_state["do_not_disturb"] = True
        self.controller.show_pokes([{"from_user": "friend"}])
        self.assertEqual(self.tooltips, [])


class PokeFakeClient(offline.FakeClient):
    def __init__(self):
        super().__init__()
        self.poke_calls = []
        self.poke_responses = []

    def fetch_my_pokes(self, token, group_id):
        self.poke_calls.append((token, group_id))
        response = self.poke_responses.pop(0) if self.poke_responses else []
        if isinstance(response, Exception):
            raise response
        return response


class PokeSyncTests(unittest.TestCase):
    setUpClass = classmethod(offline.OfflineSyncTests.setUpClass.__func__)
    setUp_base = offline.OfflineSyncTests.setUp
    finish_background = offline.OfflineSyncTests.finish_background

    def setUp(self):
        self.setUp_base()
        self.controller.client = PokeFakeClient()
        self.shown = []
        self.controller.show_pokes = self.shown.append

    def test_new_pokes_are_shown_after_sync(self):
        self.controller.client.poke_responses = [[{"id": 1, "from_user": "friend"}]]
        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertEqual(self.controller.client.poke_calls, [("token", "room-a")])
        self.assertEqual(self.shown, [[{"id": 1, "from_user": "friend"}]])
        self.assertNotIn("last_error", self.controller.online)

    def test_no_pokes_shows_nothing(self):
        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertEqual(self.shown, [])

    def test_missing_rpc_disables_pokes_without_an_error(self):
        self.controller.client.poke_responses = [PokeUnavailable()]
        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertNotIn("last_error", self.controller.online)
        status = self.controller.record_status.snapshot(user_id="user-a", group_id="room-a")
        self.assertEqual(status["errors"], {})
        self.assertGreater(self.controller._pokes_unavailable_until, time.monotonic())
        self.assertEqual(self.controller.sync_failure_count, 0)

        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertEqual(len(self.controller.client.poke_calls), 1)

    def test_transient_poke_errors_are_ignored_and_retried(self):
        self.controller.client.poke_responses = [
            SupabaseError("offline", status=503),
            [{"id": 2, "from_user": "friend"}],
        ]
        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertNotIn("last_error", self.controller.online)
        self.assertEqual(self.shown, [])
        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertEqual(len(self.controller.client.poke_calls), 2)
        self.assertEqual(self.shown, [[{"id": 2, "from_user": "friend"}]])

    def test_no_room_means_no_poke_request(self):
        self.controller.online["group"] = None
        self.controller.sync_async(force=True)
        self.finish_background()
        self.assertEqual(self.controller.client.poke_calls, [])


class PokeMigrationFileTests(unittest.TestCase):
    def test_migration_defines_table_rpcs_and_grants(self):
        sql = (ROOT / "supabase" / "migrations" / "20261006_member_pokes.sql").read_text(encoding="utf-8")
        for needle in (
            "create table if not exists public.member_pokes",
            "on public.member_pokes (to_user, created_at desc)",
            "alter table public.member_pokes enable row level security",
            "revoke all on public.member_pokes from public, anon, authenticated",
            "create or replace function public.poke_room_member(",
            "create or replace function public.fetch_my_pokes(",
            "'poke too soon'",
            "'too many pokes'",
            "interval '7 days'",
            "grant execute on function public.poke_room_member(uuid, uuid)\n  to authenticated",
            "grant execute on function public.fetch_my_pokes(uuid, timestamptz)\n  to authenticated",
        ):
            self.assertIn(needle, sql)
        self.assertTrue((ROOT / "supabase" / "tests" / "member_pokes.sql").exists())


if __name__ == "__main__":
    unittest.main()
