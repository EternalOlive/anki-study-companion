"""Pure aggregation helpers for local Anki activity summaries."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


VALID_EASES = {1, 2, 3, 4}
VALID_REVIEW_TYPES = {0, 1, 2, 3}
MAX_REVIEW_TIME_MS = 3_600_000


def weekly_activity(
    rows: list[tuple[Any, Any, Any, Any, Any]],
    current: datetime,
) -> dict[str, Any]:
    """Aggregate a complete 14-day native review-log observation.

    The visible period is the latest seven calendar days including today.
    The comparison is the preceding seven days, with its last day cut off at
    the same time of day as ``current`` so an in-progress day is not compared
    with a completed one.
    """
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("current must be timezone-aware")

    today = current.date()
    visible_start = today - timedelta(days=6)
    previous_start = today - timedelta(days=13)
    previous_end = current - timedelta(days=7)
    start_at = datetime.combine(previous_start, datetime.min.time(), current.tzinfo)
    current_ms = int(current.timestamp() * 1000)
    previous_end_ms = int(previous_end.timestamp() * 1000)
    start_ms = int(start_at.timestamp() * 1000)

    events: dict[tuple[int, int], tuple[datetime, int]] = {}
    for raw in rows:
        try:
            review_id, card_id, raw_time, raw_ease, raw_type = raw
            if isinstance(review_id, bool) or isinstance(card_id, bool):
                continue
            review_id = int(review_id)
            card_id = int(card_id)
            time_ms = min(MAX_REVIEW_TIME_MS, max(0, int(raw_time)))
            ease = int(raw_ease)
            review_type = int(raw_type)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            review_id < start_ms
            or review_id > current_ms
            or ease not in VALID_EASES
            or review_type not in VALID_REVIEW_TYPES
        ):
            continue
        try:
            answered_at = datetime.fromtimestamp(review_id / 1000, current.tzinfo)
        except (ValueError, OverflowError, OSError):
            continue
        events[(review_id, card_id)] = (answered_at, time_ms)

    daily = {
        (visible_start + timedelta(days=offset)).isoformat(): {
            "day": (visible_start + timedelta(days=offset)).isoformat(),
            "answers": 0,
        }
        for offset in range(7)
    }
    daily_ms = {day: 0 for day in daily}
    previous_answers = 0
    previous_ms = 0
    for (review_id, _card_id), (answered_at, time_ms) in events.items():
        day = answered_at.date()
        if visible_start <= day <= today:
            item = daily[day.isoformat()]
            item["answers"] += 1
            daily_ms[day.isoformat()] += time_ms
        elif previous_start <= day < visible_start and review_id <= previous_end_ms:
            previous_answers += 1
            previous_ms += time_ms

    days = []
    for day, item in daily.items():
        days.append({**item, "seconds": daily_ms[day] / 1000})
    total_ms = sum(daily_ms.values())
    return {
        "as_of": current.isoformat(),
        "days": days,
        "answers": sum(item["answers"] for item in days),
        "seconds": total_ms / 1000,
        "active_days": sum(1 for item in days if item["answers"] > 0),
        "previous_answers": previous_answers,
        "previous_seconds": previous_ms / 1000,
    }
