"""Small, Anki-independent study timer and daily counters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


TIMEZONE = timezone(timedelta(hours=9))
IDLE_AFTER = timedelta(minutes=1)


def answers_per_minute(seconds: float, answers: int) -> float | None:
    if seconds <= 0:
        return None
    return answers * 60 / seconds


def pace_density(rate: float | None, baseline: float | None) -> str:
    """Express pace relative to the learner's current-deck rate without color."""
    if rate is None or baseline is None or baseline <= 0:
        return "□"
    ratio = rate / baseline
    if ratio < 0.75:
        return "□"
    if ratio < 0.95:
        return "▤"
    if ratio <= 1.05:
        return "▦"
    if ratio <= 1.25:
        return "▩"
    return "■"


def compare_card_pace(
    my_seconds: float,
    my_answers: int,
    friend_seconds: float,
    friend_answers: int,
) -> dict | None:
    """Compare both learners at the same amount of active study time."""
    if my_seconds <= 0 or friend_seconds <= 0:
        return None
    shared_seconds = min(my_seconds, friend_seconds)
    my_at_shared_time = my_answers * shared_seconds / my_seconds
    friend_at_shared_time = friend_answers * shared_seconds / friend_seconds
    return {
        "shared_seconds": shared_seconds,
        "card_gap": round(friend_at_shared_time - my_at_shared_time),
    }


class StudyTracker:
    def __init__(
        self,
        records=None,
        deck_records=None,
        time_goal_minutes=0,
        card_goal=0,
    ):
        self.records = records or {}
        self.deck_records = deck_records or {}
        self.time_goal_minutes = time_goal_minutes
        self.card_goal = card_goal
        self.status = "stopped"
        self.last_input_at = None
        self.counted_until = None
        self.current_deck_id = None
        self.current_deck_name = None

    def set_deck(self, deck_id: str, deck_name: str, now: datetime) -> None:
        if deck_id == self.current_deck_id:
            self.current_deck_name = deck_name
            return
        self.tick(now)
        self.current_deck_id = str(deck_id)
        self.current_deck_name = deck_name

    def enter_review(self, now: datetime) -> None:
        if self.status == "stopped":
            self.status = "studying"
            self.last_input_at = now
            self.counted_until = now

    def input(self, now: datetime) -> None:
        self.tick(now)
        if self.status == "paused":
            self.status = "studying"
            self.counted_until = now
        if self.status == "studying":
            self.last_input_at = now

    def answer(self, now: datetime) -> None:
        self.tick(now)
        if self.status != "studying":
            self.status = "studying"
            self.counted_until = now
        self.last_input_at = now
        self._record(now.date().isoformat())["answers"] += 1
        if self.current_deck_id:
            self._deck_record(now.date().isoformat())["answers"] += 1

    def pause(self, now: datetime) -> None:
        self.tick(now)
        if self.status == "studying":
            self.status = "paused"

    def leave_review(self, now: datetime) -> None:
        self.tick(now)
        self.status = "stopped"
        self.last_input_at = None
        self.counted_until = None

    def tick(self, now: datetime) -> None:
        if self.status != "studying":
            return
        deadline = self.last_input_at + IDLE_AFTER
        end = min(now, deadline)
        cursor = self.counted_until
        while cursor < end:
            next_day = datetime.combine(
                cursor.date() + timedelta(days=1), datetime.min.time(), cursor.tzinfo
            )
            segment_end = min(end, next_day)
            self._record(cursor.date().isoformat())["seconds"] += (
                segment_end - cursor
            ).total_seconds()
            if self.current_deck_id:
                self._deck_record(cursor.date().isoformat())["seconds"] += (
                    segment_end - cursor
                ).total_seconds()
            cursor = segment_end
        self.counted_until = end
        if now >= deadline:
            self.status = "paused"

    def today(self, now: datetime) -> dict:
        return self._record(now.date().isoformat()).copy()

    def today_deck(self, now: datetime) -> dict | None:
        if not self.current_deck_id:
            return None
        return self._deck_record(now.date().isoformat()).copy()

    def snapshot(self) -> dict:
        return {
            "records": self.records,
            "deck_records": self.deck_records,
            "time_goal_minutes": self.time_goal_minutes,
            "card_goal": self.card_goal,
        }

    def _record(self, day: str) -> dict:
        return self.records.setdefault(day, {"seconds": 0.0, "answers": 0})

    def _deck_record(self, day: str) -> dict:
        daily = self.deck_records.setdefault(day, {})
        record = daily.setdefault(
            self.current_deck_id,
            {
                "name": self.current_deck_name or "현재 덱",
                "seconds": 0.0,
                "answers": 0,
            },
        )
        if self.current_deck_name:
            record["name"] = self.current_deck_name
        return record
