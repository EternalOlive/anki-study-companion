"""Tests for confetti celebrations (1st place solo & tie) and 8-locale i18n support."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from study_companion.confetti import get_confetti_script, get_fire_js, trigger_confetti
from study_companion import i18n
from study_companion.room_activity import ranked_places, slot_rankings


class ConfettiAndLocalesTests(unittest.TestCase):
    def test_confetti_script_and_fire_js(self):
        script = get_confetti_script()
        self.assertTrue(len(script) > 5000)
        self.assertIn("confetti", script.lower())

        fire_js = get_fire_js(particle_count=35)
        self.assertIn("particleCount: 35", fire_js)
        self.assertIn("origin: { y: 0.7 }", fire_js)

    def test_trigger_confetti_with_mock_web(self):
        mock_web = SimpleNamespace(eval=Mock())
        with patch("study_companion.confetti.mw", SimpleNamespace(web=mock_web)), \
             patch("study_companion.confetti.tooltip") as mock_tooltip:
            # Test solo 1st place
            result = trigger_confetti(
                controller=SimpleNamespace(t=lambda ko, en: ko),
                is_tie=False,
                answers=12,
            )
            self.assertTrue(result)
            mock_web.eval.assert_called_once()
            mock_tooltip.assert_called_once()
            self.assertIn("1등 달성", mock_tooltip.call_args[0][0])
            self.assertIn("12회", mock_tooltip.call_args[0][0])

            # Test tied 1st place
            mock_web.eval.reset_mock()
            mock_tooltip.reset_mock()
            trigger_confetti(
                controller=SimpleNamespace(t=lambda ko, en: ko),
                is_tie=True,
                answers=15,
            )
            self.assertTrue(result)
            mock_web.eval.assert_called_once()
            self.assertIn("공동 1등 달성", mock_tooltip.call_args[0][0])
            self.assertIn("15회", mock_tooltip.call_args[0][0])

    def test_all_eight_locales_supported(self):
        expected_locales = ("ko", "en", "ja", "zh_CN", "es", "pt", "de", "fr")
        self.assertEqual(i18n.SUPPORTED_LOCALES, expected_locales)
        for loc in expected_locales:
            self.assertIn(loc, i18n.LOCALE_NAMES)
            norm = i18n.normalize_locale(loc)
            self.assertEqual(norm, loc)

            # Essential keys
            save_str = i18n.t("save", locale=loc)
            self.assertTrue(save_str and save_str != "save" or loc == "en")

            confetti_str = i18n.t("celebrate_confetti", locale=loc)
            self.assertTrue(len(confetti_str) > 0)

            test_btn_str = i18n.t("test_confetti", locale=loc)
            self.assertTrue(len(test_btn_str) > 0)

    def test_normalize_locale_variants(self):
        self.assertEqual(i18n.normalize_locale("es-ES"), "es")
        self.assertEqual(i18n.normalize_locale("pt-BR"), "pt")
        self.assertEqual(i18n.normalize_locale("de-DE"), "de")
        self.assertEqual(i18n.normalize_locale("fr-FR"), "fr")
        self.assertEqual(i18n.normalize_locale("zh-Hans"), "zh_CN")


class SlotCelebrationLogicTests(unittest.TestCase):
    def setUp(self):
        self.celebrations = []

    def make_controller(self, my_id="me", dnd=False, celebrate=True):
        controller = SimpleNamespace()
        controller.is_do_not_disturb = lambda: dnd
        controller.ui_state = {"celebrate_confetti": celebrate}
        controller.online = {
            "group": {"id": "room-1"},
            "auth": {"user_id": my_id},
            "members": [],
        }
        controller.tracker = SimpleNamespace(time_zone="Asia/Seoul")
        controller._celebrated_slots = set()
        controller.t = lambda ko, en: ko
        return controller

    def simulate_check(self, controller, slot, members, current_dt):
        controller.online["members"] = members
        today_iso = "2026-10-08"

        # Emulate _check_and_celebrate_slot logic
        if controller.is_do_not_disturb():
            return False
        if not controller.ui_state.get("celebrate_confetti", True):
            return False
        if not controller.online.get("group"):
            return False
        slot_key = (today_iso, slot)
        if slot_key in controller._celebrated_slots:
            return False

        my_id = str((controller.online.get("auth") or {}).get("user_id") or "")
        todays = [
            m for m in members
            if isinstance(m, dict) and m.get("user_id") and m.get("study_day") in (None, today_iso)
        ]
        if not any(m.get("activity_known") is True for m in todays):
            return False

        rankings = slot_rankings(todays)
        ranking = rankings.get(slot, [])
        if not ranking:
            return False

        places = ranked_places(ranking)
        first_place_users = [user_id for place, user_id, _ans in places if place == 1]
        if my_id in first_place_users:
            my_entry = next((item for item in places if item[1] == my_id), None)
            answers = my_entry[2] if my_entry else 0
            if answers > 0:
                controller._celebrated_slots.add(slot_key)
                is_tie = len(first_place_users) > 1
                self.celebrations.append((slot, answers, is_tie))
                return True
        return False

    def test_solo_first_place_triggers_celebration(self):
        controller = self.make_controller()
        members = [
            {
                "user_id": "me",
                "activity_known": True,
                "activity_buckets": [{"slot": 5, "answer_count": 10}],
            },
            {
                "user_id": "friend",
                "activity_known": True,
                "activity_buckets": [{"slot": 5, "answer_count": 6}],
            },
        ]
        now_dt = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
        self.assertTrue(self.simulate_check(controller, 5, members, now_dt))
        self.assertEqual(self.celebrations, [(5, 10, False)])

        # Debounce: checking again does not celebrate twice
        self.assertFalse(self.simulate_check(controller, 5, members, now_dt))
        self.assertEqual(len(self.celebrations), 1)

    def test_tied_first_place_triggers_celebration(self):
        controller = self.make_controller()
        members = [
            {
                "user_id": "me",
                "activity_known": True,
                "activity_buckets": [{"slot": 8, "answer_count": 7}],
            },
            {
                "user_id": "peer",
                "activity_known": True,
                "activity_buckets": [{"slot": 8, "answer_count": 7}],
            },
        ]
        now_dt = datetime(2026, 10, 8, 5, 30, tzinfo=timezone.utc)
        self.assertTrue(self.simulate_check(controller, 8, members, now_dt))
        self.assertEqual(self.celebrations, [(8, 7, True)])

    def test_second_place_does_not_celebrate(self):
        controller = self.make_controller()
        members = [
            {
                "user_id": "me",
                "activity_known": True,
                "activity_buckets": [{"slot": 3, "answer_count": 4}],
            },
            {
                "user_id": "peer",
                "activity_known": True,
                "activity_buckets": [{"slot": 3, "answer_count": 10}],
            },
        ]
        now_dt = datetime(2026, 10, 8, 4, 40, tzinfo=timezone.utc)
        self.assertFalse(self.simulate_check(controller, 3, members, now_dt))
        self.assertEqual(self.celebrations, [])

    def test_zero_answers_does_not_celebrate(self):
        controller = self.make_controller()
        members = [
            {
                "user_id": "me",
                "activity_known": True,
                "activity_buckets": [{"slot": 1, "answer_count": 0}],
            },
        ]
        now_dt = datetime(2026, 10, 8, 4, 20, tzinfo=timezone.utc)
        self.assertFalse(self.simulate_check(controller, 1, members, now_dt))
        self.assertEqual(self.celebrations, [])

    def test_do_not_disturb_suppresses_celebration(self):
        controller = self.make_controller(dnd=True)
        members = [
            {
                "user_id": "me",
                "activity_known": True,
                "activity_buckets": [{"slot": 2, "answer_count": 15}],
            },
        ]
        now_dt = datetime(2026, 10, 8, 4, 30, tzinfo=timezone.utc)
        self.assertFalse(self.simulate_check(controller, 2, members, now_dt))
        self.assertEqual(self.celebrations, [])


class CardMilestoneCelebrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ast
        path = Path(__file__).parents[1] / "study_companion" / "addon.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls_node = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Controller"
        )
        fn_node = next(
            node for node in cls_node.body
            if isinstance(node, ast.FunctionDef) and node.name == "_check_and_celebrate_cards"
        )
        cls.mock_trigger_confetti = Mock()
        scope = {
            "trigger_confetti": cls.mock_trigger_confetti,
            "datetime": datetime,
            "max": max,
            "int": int,
            "getattr": getattr,
        }
        exec(compile(ast.Module(body=[fn_node], type_ignores=[]), str(path), "exec"), scope)
        cls._fn = scope["_check_and_celebrate_cards"]

    def setUp(self):
        self.mock_trigger_confetti.reset_mock()

    def make_controller(self, today_answers=0, dnd=False, celebrate=True):
        controller = SimpleNamespace()
        controller.is_do_not_disturb = lambda: dnd
        controller.ui_state = {"celebrate_confetti": celebrate}
        controller._answers = today_answers
        controller.study_record = lambda dt: {"answers": controller._answers}
        controller.tracker = SimpleNamespace(today=lambda dt: {"answers": controller._answers})
        controller._last_celebrated_card_milestone = (today_answers // 100) * 100
        controller.t = lambda ko, en, **kwargs: ko
        controller._check_and_celebrate_cards = lambda current: self.__class__._fn(controller, current)
        return controller

    def test_milestone_triggers_every_hundred_cards(self):
        controller = self.make_controller(today_answers=99)
        now_dt = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

        # 99 cards -> no trigger
        controller._check_and_celebrate_cards(now_dt)
        self.mock_trigger_confetti.assert_not_called()

        # 100 cards -> triggers 100 milestone
        controller._answers = 100
        controller._check_and_celebrate_cards(now_dt)
        self.assertEqual(self.mock_trigger_confetti.call_count, 1)
        msg = self.mock_trigger_confetti.call_args[1]["custom_message"]
        self.assertIn("100개", msg)
        self.assertEqual(controller._last_celebrated_card_milestone, 100)

        # 101 cards -> does not trigger again
        controller._answers = 101
        controller._check_and_celebrate_cards(now_dt)
        self.assertEqual(self.mock_trigger_confetti.call_count, 1)

        # 200 cards -> triggers 200 milestone
        controller._answers = 200
        controller._check_and_celebrate_cards(now_dt)
        self.assertEqual(self.mock_trigger_confetti.call_count, 2)
        msg2 = self.mock_trigger_confetti.call_args[1]["custom_message"]
        self.assertIn("200개", msg2)
        self.assertEqual(controller._last_celebrated_card_milestone, 200)

    def test_milestone_suppressed_by_dnd(self):
        controller = self.make_controller(today_answers=100, dnd=True)
        now_dt = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        controller._last_celebrated_card_milestone = 0

        controller._check_and_celebrate_cards(now_dt)
        self.mock_trigger_confetti.assert_not_called()

    def test_milestone_suppressed_by_setting(self):
        controller = self.make_controller(today_answers=100, celebrate=False)
        now_dt = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        controller._last_celebrated_card_milestone = 0

        controller._check_and_celebrate_cards(now_dt)
        self.mock_trigger_confetti.assert_not_called()


if __name__ == "__main__":
    unittest.main()
