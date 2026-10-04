import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ConfigSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"ConfigReadError", "_atomic_write_bytes", "_atomic_write_text", "_load_config"}
        nodes = [
            node for node in tree.body
            if (isinstance(node, ast.ClassDef) or isinstance(node, ast.FunctionDef))
            and node.name in names
        ]
        scope = {"Path": Path, "json": json, "os": os}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
        cls.ConfigReadError = scope["ConfigReadError"]
        cls.load_config = staticmethod(scope["_load_config"])
        cls.atomic_write_text = staticmethod(scope["_atomic_write_text"])

    def test_missing_file_is_the_only_empty_first_run(self):
        with tempfile.TemporaryDirectory() as directory:
            data, error = self.load_config(Path(directory) / "missing.json")
        self.assertEqual(data, {})
        self.assertIsNone(error)

    def test_corrupt_json_is_preserved_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            raw = b'{"auth": broken'
            path.write_bytes(raw)
            data, error = self.load_config(path)
            self.assertEqual(data, {})
            self.assertIsInstance(error, self.ConfigReadError)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(
                path.with_name(path.name + ".corrupt-backup").read_bytes(), raw
            )

    def test_non_object_json_is_not_accepted_as_a_new_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("[]", encoding="utf-8")
            data, error = self.load_config(path)
            self.assertEqual(data, {})
            self.assertIsInstance(error, self.ConfigReadError)
            self.assertEqual(path.read_text(encoding="utf-8"), "[]")

    def test_invalid_nested_state_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"online": []}', encoding="utf-8")
            data, error = self.load_config(path)
            self.assertEqual(data, {})
            self.assertIsInstance(error, self.ConfigReadError)

    def test_permission_error_is_not_treated_as_first_run(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("busy")):
            data, error = self.load_config(Path("state.json"))
        self.assertEqual(data, {})
        self.assertIsInstance(error, self.ConfigReadError)

    def test_failed_atomic_replace_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = b'{"version": 1}'
            path.write_bytes(original)
            with patch.object(os, "replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    self.atomic_write_text(path, '{"version": 2}')
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
