"""Pure room-level rules shared by the panel, the controller, and the web client.

Nothing here touches Qt or the network so the same rules can be unit tested
and mirrored exactly by the separate web client.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .study_day import study_day


# Published presence: a learner stays ``studying`` for this long after the
# last review input.  Study-time accounting keeps its own shorter idle limit
# (tracker.STUDY_TIME_IDLE_AFTER); the two thresholds are intentionally apart.
PRESENCE_STUDYING_WINDOW = timedelta(minutes=2)
# A member without any server update for this long is shown as offline.
MEMBER_OFFLINE_AFTER = timedelta(seconds=180)

SLOTS_PER_DAY = 144
WEEK_DAYS = 7
FRIEND_COLORS = (
    "#34C759", "#FF9500", "#AF52DE", "#FF2D55", "#30B0C7", "#A2845E", "#5856D6",
)
TIE_COLOR = "#8E8E93"


def presence_status(status: str, last_input_at: datetime | None, current: datetime) -> str:
    """Return the status to publish for this PC.

    ``stopped`` (outside review) is unchanged.  Inside review the learner is
    ``studying`` while the last input is within PRESENCE_STUDYING_WINDOW even
    after the study timer itself has paused.
    """
    if status not in ("studying", "paused"):
        return status
    if last_input_at is None:
        return status
    elapsed = current - last_input_at
    if timedelta(0) <= elapsed < PRESENCE_STUDYING_WINDOW:
        return "studying"
    return "paused"


def _parse_stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def member_status(member: dict, current: datetime) -> str:
    """Viewer-side status: offline after MEMBER_OFFLINE_AFTER without updates."""
    updated = _parse_stamp(member.get("updated_at"))
    if updated is None:
        return "offline"
    if current.astimezone(timezone.utc) - updated.astimezone(timezone.utc) > MEMBER_OFFLINE_AFTER:
        return "offline"
    status = member.get("status", "stopped")
    return status if status in ("studying", "paused", "online") else "online"


def visible_deck_name(member: dict, status: str, current: datetime, time_zone: str) -> str | None:
    """A friend's deck stays visible until it changes, but never from another day."""
    name = member.get("current_deck_name")
    if status == "offline" or not name:
        return None
    stamp = _parse_stamp(member.get("deck_updated_at"))
    if stamp is None:
        return None
    if study_day(stamp, time_zone) != study_day(current, time_zone):
        return None
    return str(name)


def shareable_deck_name(
    share_enabled: bool, deck_name: str | None, deck_day: str | None, today: str
) -> str | None:
    """Publisher rule: share the last opened deck only if it was opened today."""
    if not share_enabled or not deck_name or deck_day != today:
        return None
    return deck_name


def member_colors(member_ids: Iterable[str], my_id: str | None, my_color: str) -> dict[str, str]:
    """Stable colors by room join order; friends cycle through FRIEND_COLORS."""
    colors: dict[str, str] = {}
    index = 0
    for user_id in member_ids:
        if user_id in colors:
            continue
        if my_id is not None and user_id == my_id:
            colors[user_id] = my_color
            continue
        colors[user_id] = FRIEND_COLORS[index % len(FRIEND_COLORS)]
        index += 1
    return colors


def slot_rankings(members: Iterable[dict]) -> dict[int, list[tuple[str, int]]]:
    """Per 10-minute slot, members with answers there, most answers first.

    Equal counts keep join order.  Members whose activity is not known send
    no buckets and therefore never appear.
    """
    order: list[str] = []
    counts: dict[int, dict[str, int]] = {}
    for member in members:
        user_id = str(member.get("user_id") or "")
        if not user_id or user_id in order:
            continue
        order.append(user_id)
        if member.get("activity_known") is not True:
            continue
        for bucket in member.get("activity_buckets") or []:
            try:
                slot = int(bucket.get("slot"))
                answers = int(bucket.get("answer_count") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if 0 <= slot < SLOTS_PER_DAY and answers > 0:
                slot_counts = counts.setdefault(slot, {})
                slot_counts[user_id] = slot_counts.get(user_id, 0) + answers
    position = {user_id: index for index, user_id in enumerate(order)}
    return {
        slot: sorted(values.items(), key=lambda item: (-item[1], position[item[0]]))
        for slot, values in counts.items()
    }


def slot_leader(ranking: list[tuple[str, int]]) -> str | None:
    """The single member with the most answers, or None for a tie or no data."""
    if not ranking:
        return None
    if len(ranking) > 1 and ranking[1][1] == ranking[0][1]:
        return None
    return ranking[0][0]


def ranked_places(ranking: list[tuple[str, int]]) -> list[tuple[int, str, int]]:
    """Competition ranking (1, 1, 3): equal counts share a place."""
    places = []
    previous = None
    place = 0
    for index, (user_id, answers) in enumerate(ranking):
        if answers != previous:
            place = index + 1
            previous = answers
        places.append((place, user_id, answers))
    return places


def led_counts(rankings: dict[int, list[tuple[str, int]]]) -> dict[str, int]:
    """Number of slots each member led alone today; ties count for nobody."""
    result: dict[str, int] = {}
    for ranking in rankings.values():
        leader = slot_leader(ranking)
        if leader is not None:
            result[leader] = result.get(leader, 0) + 1
    return result


def week_days(today: date) -> list[str]:
    """The last WEEK_DAYS room days, oldest first, ending with today."""
    return [(today - timedelta(days=offset)).isoformat() for offset in range(WEEK_DAYS - 1, -1, -1)]


def missing_week_days(cache: dict, group_id: str, today: date) -> list[str]:
    """Past days of the visible week not yet cached for this room."""
    stored = (cache.get(str(group_id)) or {}) if isinstance(cache, dict) else {}
    return [day for day in week_days(today)[:-1] if day not in stored]


def store_week_day(cache: dict, group_id: str, day: str, rows: Iterable[dict]) -> None:
    """Cache one finished day's answer counts; finished days no longer change."""
    answers: dict[str, int] = {}
    for row in rows or []:
        try:
            user_id = str(row["user_id"])
            answers[user_id] = max(0, int(row.get("answer_count") or 0))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    cache.setdefault(str(group_id), {})[str(day)] = answers


def prune_week_cache(cache: dict, today: date) -> None:
    """Drop days that left the visible week; they are never shown again."""
    keep = set(week_days(today)[:-1])
    for group_id in list(cache):
        days = cache[group_id]
        if not isinstance(days, dict):
            del cache[group_id]
            continue
        for day in list(days):
            if day not in keep:
                del days[day]
        if not days:
            del cache[group_id]


def weekly_room_series(
    cache: dict, group_id: str, today: date, members: Iterable[dict] | None
) -> dict[str, list[int | None]]:
    """Seven answer counts per room member; None marks an unknown day.

    Past days come from the per-room cache, today from the current member
    list.  A member missing from a fetched day studied 0 that day.
    """
    member_list = [member for member in (members or []) if member.get("user_id")]
    stored = (cache.get(str(group_id)) or {}) if isinstance(cache, dict) else {}
    days = week_days(today)
    series: dict[str, list[int | None]] = {}
    for member in member_list:
        user_id = str(member["user_id"])
        values: list[int | None] = []
        for day in days[:-1]:
            day_values = stored.get(day)
            values.append(None if day_values is None else int(day_values.get(user_id, 0)))
        if member.get("study_day") in (None, days[-1]) and member.get("answer_count") is not None:
            values.append(max(0, int(member.get("answer_count") or 0)))
        else:
            values.append(None)
        series[user_id] = values
    return series


def sort_room_members(members: Iterable[dict]) -> list[dict]:
    """Sort room members in order of most study: answers desc, active_seconds desc, display_name."""
    return sorted(
        members or [],
        key=lambda m: (
            -max(0, int(m.get("answer_count") or 0)),
            -max(0, int(m.get("active_seconds") or 0)),
            str(m.get("display_name") or m.get("user_id") or ""),
        ),
    )

