"""Verify activity filtering without starting Anki or Qt."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock


class InputWatcherTests(TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "InputWatcher")
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "eventFilter"]
        self.types = SimpleNamespace(KeyPress=1, MouseButtonPress=2, Wheel=3,
                                     MouseMove=4, ApplicationDeactivate=5)
        self.web = SimpleNamespace(parent=lambda: None)
        self.window = SimpleNamespace(state="review", web=self.web,
                                      reviewer=None, isActiveWindow=lambda: True)
        ns = {"QObject": object, "mw": self.window,
              "QEvent": SimpleNamespace(Type=self.types)}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), ns)
        self.watcher = ns["InputWatcher"]()
        self.watcher.controller = Mock()

    def event(self, kind, target=None, repeat=False):
        self.watcher.eventFilter(target or self.web,
            SimpleNamespace(type=lambda: kind, isAutoRepeat=lambda: repeat))

    def test_mouse_movement_and_auto_repeat_do_not_extend_timer(self):
        self.event(self.types.MouseMove)
        self.event(self.types.KeyPress, repeat=True)
        self.watcher.controller.input.assert_not_called()

    def test_sidebar_input_does_not_extend_timer(self):
        self.event(self.types.MouseButtonPress, SimpleNamespace(parent=lambda: None, name="sidebar"))
        self.watcher.controller.input.assert_not_called()

    def test_review_child_click_resumes_timer(self):
        self.event(self.types.MouseButtonPress, SimpleNamespace(parent=lambda: self.web))
        self.watcher.controller.input.assert_called_once()

    def test_background_input_does_not_resume(self):
        self.window.isActiveWindow = lambda: False
        self.event(self.types.KeyPress)
        self.watcher.controller.input.assert_not_called()

    def test_deactivation_pauses_timer(self):
        self.event(self.types.ApplicationDeactivate)
        self.watcher.controller.pause.assert_called_once()
