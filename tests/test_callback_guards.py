"""Exercise controller callbacks without requiring a running Anki process."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
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
        namespace = {"mw": SimpleNamespace(taskman=SimpleNamespace(
            run_in_background=lambda task, done: callbacks.append(done)))}
        namespace["SupabaseError"] = SupabaseError
        exec(compile(ast.Module(body=[controller], type_ignores=[]), str(source), "exec"), namespace)
        self.control = namespace["Controller"]()
        self.control.closed = False
        self.control.identity_generation = 1
        self.control.online = {"auth": {"user_id": "old"}}
        self.callbacks = callbacks

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
