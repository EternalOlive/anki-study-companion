import re
import unittest

from study_companion.nicknames import (
    CODE_ALPHABET,
    canonical_nickname,
    disambiguate_nickname,
    is_valid_nickname_or_code,
    localize_nickname,
    sanitize_display_name,
    validate_display_name,
)


class NicknameTests(unittest.TestCase):
    def test_assignment_is_stable(self):
        first = canonical_nickname("anonymous-user-42")
        self.assertEqual(first, canonical_nickname("anonymous-user-42"))

    def test_code_has_compact_unambiguous_format(self):
        code = canonical_nickname("anonymous-user-42")
        self.assertRegex(code, r"^[2-9A-HJ-NP-Z]{3}-[2-9A-HJ-NP-Z]{3}$")
        self.assertFalse(set(code) & set("O0I1"))

    def test_alphabet_has_32_unique_characters(self):
        self.assertEqual(32, len(CODE_ALPHABET))
        self.assertEqual(32, len(set(CODE_ALPHABET)))
        self.assertFalse(set(CODE_ALPHABET) & set("O0I1"))

    def test_different_ids_normally_get_different_codes(self):
        codes = {canonical_nickname(f"user-{index}") for index in range(100)}
        self.assertEqual(100, len(codes))

    def test_code_is_locale_invariant(self):
        code = canonical_nickname("anonymous-user-42")
        self.assertEqual(code, localize_nickname(code, "ko-KR"))
        self.assertEqual(code, localize_nickname(code, "en-US"))

    def test_existing_text_is_preserved(self):
        self.assertEqual("기존이름", localize_nickname("기존이름", "en"))

    def test_empty_user_id_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_nickname("  ")

    def test_collision_suffix_is_opt_in_and_unambiguous(self):
        code = canonical_nickname("a")
        self.assertEqual(code, disambiguate_nickname(code, "a"))
        suffixed = disambiguate_nickname(code, "a", duplicate=True)
        self.assertTrue(
            re.fullmatch(
                r"[2-9A-HJ-NP-Z]{3}-[2-9A-HJ-NP-Z]{3} · [2-9A-HJ-NP-Z]{2}",
                suffixed,
            )
        )
        self.assertEqual(suffixed, disambiguate_nickname(code, "a", True))

    def test_validate_display_name(self):
        self.assertTrue(validate_display_name("Alex"))
        self.assertTrue(validate_display_name("user123"))
        self.assertTrue(validate_display_name("a1"))
        self.assertTrue(validate_display_name("16CharacterNick1"))

        # Too short (< 2)
        self.assertFalse(validate_display_name("a"))
        self.assertFalse(validate_display_name(""))
        # Too long (> 16)
        self.assertFalse(validate_display_name("17CharactersLong12"))
        # Spaces, punctuation, symbols
        self.assertFalse(validate_display_name("alex park"))
        self.assertFalse(validate_display_name("user_123"))
        self.assertFalse(validate_display_name("user-123"))
        # Non-ASCII / Korean
        self.assertFalse(validate_display_name("열공생"))
        # Injection / HTML
        self.assertFalse(validate_display_name("<script>"))
        self.assertFalse(validate_display_name("name' OR '1'='1"))
        # Non-string
        self.assertFalse(validate_display_name(None))
        self.assertFalse(validate_display_name(12345))

    def test_is_valid_nickname_or_code(self):
        self.assertTrue(is_valid_nickname_or_code("Alex"))
        self.assertTrue(is_valid_nickname_or_code("XTU-3FU"))
        self.assertFalse(is_valid_nickname_or_code("XTU-3FU1"))
        self.assertFalse(is_valid_nickname_or_code("bad!name"))

    def test_sanitize_display_name(self):
        # Valid names preserved
        self.assertEqual("Alex", sanitize_display_name("Alex", "user-1"))
        self.assertEqual("XTU-3FU", sanitize_display_name("XTU-3FU", "user-1"))

        # Invalid strings sanitized to fallback
        expected_fallback = canonical_nickname("user-1")
        self.assertEqual(expected_fallback, sanitize_display_name("<script>alert(1)</script>", "user-1"))
        self.assertEqual(expected_fallback, sanitize_display_name("홍길동", "user-1"))
        self.assertEqual(expected_fallback, sanitize_display_name("a", "user-1"))
        self.assertEqual(expected_fallback, sanitize_display_name(None, "user-1"))
        self.assertEqual(expected_fallback, sanitize_display_name("", "user-1"))

        # Fallback when no user_id given
        self.assertEqual("Guest", sanitize_display_name("bad bad bad", None))

    def test_controller_set_and_reset_display_name(self):
        import ast
        from pathlib import Path
        from types import SimpleNamespace
        path = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        controller_node = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Controller"
        )
        controller_node.body = [
            node for node in controller_node.body
            if isinstance(node, ast.FunctionDef)
            and node.name in ("set_display_name", "reset_display_name", "_access_token")
        ]
        scope = {
            "validate_display_name": validate_display_name,
            "canonical_nickname": canonical_nickname,
            "threading": __import__("threading"),
            "time": __import__("time"),
            "getattr": getattr,
        }
        exec(compile(ast.Module(body=[controller_node], type_ignores=[]), str(path), "exec"), scope)
        ControllerClass = scope["Controller"]
        ctrl = ControllerClass()
        ctrl.online = {"auth": {"user_id": "u-123", "access_token": "token-xyz"}}
        ctrl.client = SimpleNamespace(upsert_profile=lambda *a, **kw: None)
        ctrl.save = lambda: None
        ctrl.refresh = lambda: None
        ctrl.realtime = None

        self.assertTrue(ctrl.set_display_name("goyori"))
        self.assertEqual(ctrl.online["display_name"], "goyori")

        ctrl.reset_display_name()
        self.assertEqual(ctrl.online["display_name"], canonical_nickname("u-123"))


if __name__ == "__main__":
    unittest.main()

