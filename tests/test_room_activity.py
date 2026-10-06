"""Room-level rules mirrored by the web client: leaders, decks, presence, week."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest import TestCase

from study_companion.room_activity import (
    FRIEND_COLORS,
    MEMBER_OFFLINE_AFTER,
    PRESENCE_STUDYING_WINDOW,
    led_counts,
    member_colors,
    member_status,
    missing_week_days,
    presence_status,
    prune_week_cache,
    ranked_places,
    shareable_deck_name,
    slot_leader,
    slot_rankings,
    store_week_day,
    visible_deck_name,
    week_days,
    weekly_room_series,
)
from study_companion.tracker import STUDY_TIME_IDLE_AFTER, StudyTracker


KST = timezone(timedelta(hours=9))


def at(hour=10, minute=0, second=0, day=5):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=KST)


def member(user_id, buckets, known=True, **extra):
    return {
        "user_id": user_id,
        "activity_known": known,
        "activity_buckets": [
            {"slot": slot, "answer_count": answers, "time_ms": answers * 1000}
            for slot, answers in buckets
        ],
        **extra,
    }


class PresenceTests(TestCase):
    def test_thresholds_are_separate(self):
        self.assertEqual(STUDY_TIME_IDLE_AFTER, timedelta(minutes=1))
        self.assertEqual(PRESENCE_STUDYING_WINDOW, timedelta(minutes=2))
        self.assertEqual(MEMBER_OFFLINE_AFTER, timedelta(seconds=180))

    def test_studying_is_published_for_two_minutes_while_time_stops_at_one(self):
        tracker = StudyTracker()
        tracker.enter_review(at())
        tracker.tick(at(minute=1, second=30))
        self.assertEqual(tracker.status, "paused")
        self.assertEqual(tracker.today(at())["seconds"], 60)
        self.assertEqual(
            presence_status(tracker.status, tracker.last_input_at, at(minute=1, second=30)),
            "studying",
        )
        self.assertEqual(
            presence_status(tracker.status, tracker.last_input_at, at(minute=1, second=59)),
            "studying",
        )
        self.assertEqual(
            presence_status(tracker.status, tracker.last_input_at, at(minute=2)),
            "paused",
        )

    def test_stopped_and_unknown_input_are_unchanged(self):
        self.assertEqual(presence_status("stopped", None, at()), "stopped")
        self.assertEqual(presence_status("stopped", at(), at()), "stopped")
        self.assertEqual(presence_status("studying", None, at()), "studying")

    def test_member_goes_offline_after_180_seconds(self):
        current = datetime(2026, 10, 5, 1, 0, 0, tzinfo=timezone.utc)
        fresh = {"status": "studying", "updated_at": (current - timedelta(seconds=179)).isoformat()}
        stale = {"status": "studying", "updated_at": (current - timedelta(seconds=181)).isoformat()}
        self.assertEqual(member_status(fresh, current), "studying")
        self.assertEqual(member_status(stale, current), "offline")
        self.assertEqual(member_status({"status": "studying"}, current), "offline")
        self.assertEqual(member_status({"updated_at": "garbage"}, current), "offline")
        self.assertEqual(
            member_status({"status": "stopped", "updated_at": "2026-10-05T01:00:00Z"}, current),
            "online",
        )


class DeckVisibilityTests(TestCase):
    def test_deck_stays_while_connected_on_the_same_room_day(self):
        friend = {"current_deck_name": "English", "deck_updated_at": at(9).isoformat()}
        for status in ("studying", "paused", "online"):
            with self.subTest(status=status):
                self.assertEqual(visible_deck_name(friend, status, at(23), "Asia/Seoul"), "English")

    def test_offline_previous_day_and_missing_stamp_hide_the_deck(self):
        friend = {"current_deck_name": "English", "deck_updated_at": at(9).isoformat()}
        self.assertIsNone(visible_deck_name(friend, "offline", at(10), "Asia/Seoul"))
        # 03:59 belongs to the previous 04:00 room day even on the same date.
        early = dict(friend, deck_updated_at=at(3, 59).isoformat())
        self.assertIsNone(visible_deck_name(early, "online", at(4, 1), "Asia/Seoul"))
        self.assertEqual(visible_deck_name(early, "online", at(3, 59, 30), "Asia/Seoul"), "English")
        self.assertIsNone(visible_deck_name({"current_deck_name": "English"}, "online", at(), "Asia/Seoul"))
        self.assertIsNone(visible_deck_name({"deck_updated_at": at().isoformat()}, "online", at(), "Asia/Seoul"))

    def test_room_time_zone_decides_the_day(self):
        friend = {"current_deck_name": "Words", "deck_updated_at": "2026-10-05T03:30:00Z"}
        current = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)
        # 03:30 → 04:30 in a UTC room crosses the 04:00 boundary; Seoul does not.
        self.assertIsNone(visible_deck_name(friend, "online", current, "UTC"))
        self.assertEqual(visible_deck_name(friend, "online", current, "Asia/Seoul"), "Words")

    def test_publisher_shares_only_a_deck_opened_today(self):
        self.assertEqual(shareable_deck_name(True, "Bio", "2026-10-05", "2026-10-05"), "Bio")
        self.assertIsNone(shareable_deck_name(False, "Bio", "2026-10-05", "2026-10-05"))
        self.assertIsNone(shareable_deck_name(True, "Bio", "2026-10-04", "2026-10-05"))
        self.assertIsNone(shareable_deck_name(True, None, "2026-10-05", "2026-10-05"))

    def test_tracker_records_the_day_a_deck_was_opened(self):
        tracker = StudyTracker()
        self.assertIsNone(tracker.current_deck_day)
        tracker.set_deck("1", "Bio", at(3, 30))
        self.assertEqual(tracker.current_deck_day, "2026-10-04")
        tracker.set_deck("1", "Bio", at(4, 30))
        self.assertEqual(tracker.current_deck_day, "2026-10-05")


class SlotLeaderTests(TestCase):
    def test_leader_ties_and_led_counts(self):
        members = [
            member("me", [(10, 5), (11, 2), (12, 3)]),
            member("a", [(10, 3), (11, 4), (12, 3)]),
            member("b", [(13, 1)]),
        ]
        rankings = slot_rankings(members)
        self.assertEqual(rankings[10], [("me", 5), ("a", 3)])
        self.assertEqual(slot_leader(rankings[10]), "me")
        self.assertEqual(slot_leader(rankings[11]), "a")
        self.assertIsNone(slot_leader(rankings[12]))  # tie → neutral, nobody
        self.assertEqual(led_counts(rankings), {"me": 1, "a": 1, "b": 1})
        self.assertNotIn(0, rankings)

    def test_ranking_order_and_competition_places(self):
        members = [
            member("me", [(20, 2)]),
            member("a", [(20, 6)]),
            member("b", [(20, 2)]),
            member("c", [(20, 0)]),
        ]
        ranking = slot_rankings(members)[20]
        self.assertEqual(ranking, [("a", 6), ("me", 2), ("b", 2)])
        self.assertEqual(ranked_places(ranking), [(1, "a", 6), (2, "me", 2), (2, "b", 2)])

    def test_unknown_activity_and_bad_buckets_are_ignored(self):
        members = [
            member("a", [(5, 9)], known=False),
            {"user_id": "b", "activity_known": True,
             "activity_buckets": [{"slot": 144, "answer_count": 1}, {"slot": "x"}, None,
                                  {"slot": 7, "answer_count": 2}]},
        ]
        self.assertEqual(slot_rankings(members), {7: [("b", 2)]})
        self.assertEqual(led_counts({}), {})
        self.assertIsNone(slot_leader([]))

    def test_colors_follow_join_order_and_me_uses_accent(self):
        colors = member_colors(["a", "me", "b"], "me", "#123456")
        self.assertEqual(colors, {"a": FRIEND_COLORS[0], "me": "#123456", "b": FRIEND_COLORS[1]})
        many = member_colors([f"u{index}" for index in range(9)], None, "#000000")
        self.assertEqual(many["u7"], FRIEND_COLORS[0])


class WeeklyCacheTests(TestCase):
    def test_missing_days_store_and_prune(self):
        today = date(2026, 10, 5)
        cache = {}
        self.assertEqual(week_days(today)[0], "2026-09-29")
        self.assertEqual(week_days(today)[-1], "2026-10-05")
        self.assertEqual(len(missing_week_days(cache, "room", today)), 6)
        store_week_day(cache, "room", "2026-10-04", [
            {"user_id": "a", "answer_count": 12}, {"user_id": "b", "answer_count": None},
            {"answer_count": 4},
        ])
        self.assertEqual(cache["room"]["2026-10-04"], {"a": 12, "b": 0})
        self.assertNotIn("2026-10-04", missing_week_days(cache, "room", today))
        self.assertNotIn("2026-10-05", missing_week_days(cache, "room", today))
        store_week_day(cache, "room", "2026-09-20", [])
        store_week_day(cache, "old-room", "2026-09-01", [])
        prune_week_cache(cache, today)
        self.assertEqual(set(cache), {"room"})
        self.assertEqual(set(cache["room"]), {"2026-10-04"})
        # After the room day changes, yesterday becomes a past day to read once.
        self.assertIn("2026-10-05", missing_week_days(cache, "room", date(2026, 10, 6)))

    def test_series_uses_cache_gaps_and_live_today(self):
        today = date(2026, 10, 5)
        cache = {}
        store_week_day(cache, "room", "2026-10-03", [{"user_id": "a", "answer_count": 7}])
        store_week_day(cache, "room", "2026-10-04", [{"user_id": "b", "answer_count": 3}])
        members = [
            {"user_id": "a", "study_day": "2026-10-05", "answer_count": 11},
            {"user_id": "b", "study_day": "2026-10-04", "answer_count": 3},
        ]
        series = weekly_room_series(cache, "room", today, members)
        self.assertEqual(series["a"], [None, None, None, None, 7, 0, 11])
        # A stale member row never stands in for today.
        self.assertEqual(series["b"], [None, None, None, None, 0, 3, None])
