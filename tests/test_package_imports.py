import ast
from pathlib import Path
from unittest import TestCase


PACKAGE = Path(__file__).resolve().parents[1] / "study_companion"


class PackageImportTests(TestCase):
    def test_addon_modules_never_import_their_own_package_by_name(self):
        # AnkiWeb installs the add-on under its numeric ID (addons21/124974592),
        # so `study_companion.x` is not importable inside Anki. Use `.x` instead.
        offenders = []
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                for name in names:
                    if name == "study_companion" or name.startswith("study_companion."):
                        offenders.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual(offenders, [])
