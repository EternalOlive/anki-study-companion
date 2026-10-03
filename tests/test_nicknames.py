import re
import unittest

from study_companion.nicknames import (
    CODE_ALPHABET,
    canonical_nickname,
    disambiguate_nickname,
    localize_nickname,
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


if __name__ == "__main__":
    unittest.main()
