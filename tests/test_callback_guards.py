"""Exercise controller callbacks without requiring a running Anki process."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
import time
from concurrent.futures import Future
from study_companion.online import SupabaseError


class CallbackGuardTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        controller = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Controller")
        names = {"_run_online_action", "_run_authenticated_action"}
        controller.body = [n for n in controller.body if isinstance(n, ast.FunctionDef) and n.name in names]
        callbacks = []
        jobs = []
        namespace = {"mw": SimpleNamespace(taskman=SimpleNamespace(
            run_in_background=lambda task, done: (
                jobs.append(task), callbacks.append(done)
            )
        ))}
        namespace["SupabaseError"] = SupabaseError
        namespace["threading"] = threading
        namespace["time"] = time
        exec(compile(ast.Module(body=[controller], type_ignores=[]), str(source), "exec"), namespace)
        self.control = namespace["Controller"]()
        self.control.closed = False
        self.control.identity_generation = 1
        self.control.online = {"auth": {"user_id": "old"}}
        self.control.client = SimpleNamespace()
        self.control.save = lambda: None
        self.callbacks = callbacks
        self.jobs = jobs

    def start(self, authenticated=False):
        method = self.control._run_authenticated_action if authenticated else self.control._run_online_action
        method([], lambda *args: None, lambda value: self.fail("Stale success"), "error")

    def test_closed_online_callback_is_ignored(self):
        self.start()
        self.control.closed = True
        self.callbacks[0](None)

    def test_replaced_login_callback_is_ignored(self):
        self.start()
        self.control.identity_generation += 1
        self.callbacks[0](None)

    def test_other_account_authenticated_callback_is_ignored(self):
        self.start(authenticated=True)
        self.control.online["auth"]["user_id"] = "new"
        self.callbacks[0](None)
        self.assertEqual("new", self.control.online["auth"]["user_id"])

    def test_closed_authenticated_callback_is_ignored(self):
        self.start(authenticated=True)
        self.control.closed = True
        self.callbacks[0](None)

    def test_online_error_can_be_shown_inline(self):
        errors = []
        self.control._run_online_action([], lambda: None, lambda _: None,
                                        "Could not sign in", on_error=errors.append)
        future = Future()
        future.set_exception(SupabaseError("Connection failed"))
        self.callbacks[0](future)
        self.assertEqual(["Could not sign in\nConnection failed"], errors)

    def test_authenticated_error_can_be_shown_inline(self):
        errors = []
        self.control._run_authenticated_action([], lambda _: None, lambda _: None,
                                               "Could not join", on_error=errors.append)
        future = Future()
        future.set_exception(SupabaseError("Unknown room", status=404))
        self.callbacks[0](future)
        self.assertEqual(["Could not join\nUnknown room"], errors)

    def test_refreshed_session_is_saved_when_operation_fails(self):
        self.control.online["auth"] = {
            "user_id": "old", "access_token": "old-token",
            "refresh_token": "refresh", "expires_at": 0,
        }
        self.control.client.refresh = lambda _token: {
            "access_token": "new-token", "refresh_token": "new-refresh",
            "expires_at": int(time.time()) + 3600,
        }
        saves = []
        self.control.save = lambda: saves.append(True)
        errors = []
        self.control._run_authenticated_action(
            [],
            lambda _token: (_ for _ in ()).throw(SupabaseError("server error", status=500)),
            lambda _value: self.fail("failed operation must not succeed"),
            "Could not update", on_error=errors.append,
        )
        future = Future()
        future.set_result(self.jobs[-1]())
        self.callbacks[-1](future)
        self.assertEqual(self.control.online["auth"]["access_token"], "new-token")
        self.assertEqual(saves, [True])
        self.assertEqual(errors, ["Could not update\nserver error"])

    def test_concurrent_actions_share_one_refresh_result(self):
        self.control.online["auth"] = {
            "user_id": "old", "access_token": "old-token",
            "refresh_token": "refresh", "expires_at": 0,
        }
        calls = []
        self.control.client.refresh = lambda token: (
            calls.append(token) or {
                "access_token": "new-token", "refresh_token": "new-refresh",
                "expires_at": int(time.time()) + 3600,
            }
        )
        for _ in range(2):
            self.control._run_authenticated_action(
                [], lambda token: token, lambda _value: None, "error"
            )
        results = []
        for task in self.jobs[-2:]:
            results.append(task())
        self.assertEqual(calls, ["refresh"])
        for done, result in zip(self.callbacks[-2:], results):
            future = Future()
            future.set_result(result)
            done(future)
        self.assertEqual(self.control.online["auth"]["access_token"], "new-token")

    def test_late_callback_cannot_overwrite_a_newer_session(self):
        self.control.online["auth"] = {
            "user_id": "old", "access_token": "old-token",
            "refresh_token": "refresh", "expires_at": 0,
        }
        self.control.client.refresh = lambda _token: {
            "access_token": "refreshed-token", "refresh_token": "rotated",
            "expires_at": int(time.time()) + 3600,
        }
        successes = []
        self.control._run_authenticated_action(
            [], lambda token: token, successes.append, "error"
        )
        result = self.jobs[-1]()
        self.control.online["auth"] = {
            "user_id": "old", "access_token": "newer-login"
        }
        future = Future()
        future.set_result(result)
        self.callbacks[-1](future)
        self.assertEqual(self.control.online["auth"]["access_token"], "newer-login")
        self.assertEqual(successes, [])
