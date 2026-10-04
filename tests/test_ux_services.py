import unittest

from study_companion.ux_services import (
    ANSWER_GOAL_MAX,
    TIME_GOAL_MAX_MINUTES,
    build_invite_message,
    normalize_invite_code,
    validate_goal,
    validate_invite_code,
)


class UxServicesTests(unittest.TestCase):
    def test_invite_code_is_normalized_and_validated(self):
        self.assertEqual(normalize_invite_code(" a b3d \n"), "AB3D")
        self.assertEqual(validate_invite_code(" a b3d "), "AB3D")
        for value in ("AB1D", "AB0D", "ABOD", "AID3", "ABC", "ABCDE"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_invite_code(value)

    def test_invite_message_contains_only_current_room_and_optional_verified_url(self):
        group = {
            "name": "goyori",
            "invite_code": "AB3D",
            "owner_id": "private-user",
            "access_token": "private-token",
        }
        korean = build_invite_message(group, "ko")
        self.assertEqual(
            korean,
            "Anki 스터디방 · goyori\n초대 코드: AB3D\n"
            "스터디 패널 → 코드로 참여 → AB3D 입력",
        )
        self.assertNotIn("private-user", korean)
        self.assertNotIn("private-token", korean)
        self.assertNotIn("<", korean)

        english = build_invite_message(group, "en", "https://example.test/setup")
        self.assertIn("Anki study room · goyori", english)
        self.assertIn("Setup: https://example.test/setup", english)

    def test_goal_bounds_match_existing_settings(self):
        self.assertEqual(validate_goal(0, maximum=TIME_GOAL_MAX_MINUTES), 0)
        self.assertEqual(validate_goal("60", maximum=TIME_GOAL_MAX_MINUTES), 60)
        self.assertEqual(validate_goal(ANSWER_GOAL_MAX, maximum=ANSWER_GOAL_MAX), 10000)
        for value in (-1, TIME_GOAL_MAX_MINUTES + 1, True, "x", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_goal(value, maximum=TIME_GOAL_MAX_MINUTES)


if __name__ == "__main__":
    unittest.main()
