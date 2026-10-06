"""Room-local study-day calculations with a 04:00 boundary.

The add-on runs on Windows, where Python's stdlib ``zoneinfo`` database is
usually absent.  Anki does ship Qt's IANA time-zone database, so this module
uses ``zoneinfo`` when available and falls back to ``QTimeZone`` in Anki.
All public boundaries are returned as UTC datetimes, which keeps elapsed-time
math correct across daylight-saving changes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Iterator


DEFAULT_TIME_ZONE = "Asia/Seoul"
DAY_START_HOUR = 4
UTC = timezone.utc

_FIXED_ZONES = {
    "UTC": timezone.utc,
    "Etc/UTC": timezone.utc,
    "Asia/Seoul": timezone(timedelta(hours=9), "KST"),
}


def room_time_zone(group: dict | None) -> str:
    """Return a room's canonical IANA zone, preserving old cached rooms."""
    if isinstance(group, dict):
        value = group.get("time_zone")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_TIME_ZONE


def room_datetime(moment: datetime, time_zone: str = DEFAULT_TIME_ZONE) -> datetime:
    """Convert an aware instant to room wall time.

    The returned datetime carries the offset active at that instant.  It is
    suitable for display and calendar classification, including DST folds.
    """
    _require_aware(moment)
    zone = _stdlib_zone(time_zone)
    if zone is not None:
        return moment.astimezone(zone)
    qt_value = _qt_datetime_from_epoch(moment.timestamp(), time_zone)
    if qt_value is not None:
        return qt_value
    return moment.astimezone(_fallback_zone(time_zone))


def study_day(moment: datetime, time_zone: str = DEFAULT_TIME_ZONE) -> date:
    """Return the room date containing ``moment``; the date changes at 04:00."""
    local = room_datetime(moment, time_zone)
    return (local - timedelta(hours=DAY_START_HOUR)).date()


def day_bounds(
    day: date | str,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> tuple[datetime, datetime]:
    """Return UTC [start, end) for one room study day.

    Start and end are constructed as separate local 04:00 wall times.  Their
    UTC difference may therefore be 23 or 25 hours at a DST transition.
    """
    target = date.fromisoformat(day) if isinstance(day, str) else day
    if not isinstance(target, date):
        raise TypeError("day must be a date or ISO date string")
    return (
        _local_wall_to_utc(target, DAY_START_HOUR, 0, 0, time_zone),
        _local_wall_to_utc(target + timedelta(days=1), DAY_START_HOUR, 0, 0, time_zone),
    )


def current_day_bounds(
    moment: datetime,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> tuple[date, datetime, datetime]:
    """Return the current room study-day label and its UTC bounds."""
    target = study_day(moment, time_zone)
    start, end = day_bounds(target, time_zone)
    return target, start, end


def same_wall_time_on_day(
    day: date | str,
    moment: datetime,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> datetime:
    """Map ``moment``'s room wall clock to another room study day.

    This is used by period comparisons so a DST week compares 12:30 with
    12:30, rather than shifting the cutoff by an hour.
    """
    target = date.fromisoformat(day) if isinstance(day, str) else day
    local = room_datetime(moment, time_zone)
    wall_date = target if local.hour >= DAY_START_HOUR else target + timedelta(days=1)
    return _local_wall_to_utc(
        wall_date,
        local.hour,
        local.minute,
        local.second,
        time_zone,
        microsecond=local.microsecond,
    )


def split_interval(
    start: datetime,
    end: datetime,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> Iterator[tuple[str, float]]:
    """Yield ``(study_day, seconds)`` segments for an absolute interval."""
    _require_aware(start)
    _require_aware(end)
    cursor = start.astimezone(UTC)
    finish = end.astimezone(UTC)
    while cursor < finish:
        target = study_day(cursor, time_zone)
        _, boundary = day_bounds(target, time_zone)
        segment_end = min(finish, boundary)
        yield target.isoformat(), (segment_end - cursor).total_seconds()
        cursor = segment_end


def ten_minute_slot(moment: datetime, time_zone: str = DEFAULT_TIME_ZONE) -> int:
    """Return the room-wall 10-minute slot, where slot zero is 04:00."""
    local = room_datetime(moment, time_zone)
    minutes = (local.hour * 60 + local.minute - DAY_START_HOUR * 60) % (24 * 60)
    return minutes // 10


def quarter_hour_slot(moment: datetime, time_zone: str = DEFAULT_TIME_ZONE) -> int:
    """Return the room-wall 15-minute slot, where slot zero is 04:00."""
    local = room_datetime(moment, time_zone)
    minutes = (local.hour * 60 + local.minute - DAY_START_HOUR * 60) % (24 * 60)
    return minutes // 15


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


@lru_cache(maxsize=64)
def _stdlib_zone(name: str):
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return None
    except ImportError:
        return None


def _fallback_zone(name: str):
    try:
        return _FIXED_ZONES[name]
    except KeyError as error:
        raise ValueError(f"IANA time zone unavailable: {name}") from error


@lru_cache(maxsize=64)
def _qt_zone(name: str):
    try:
        from PyQt6.QtCore import QTimeZone

        zone = QTimeZone(name.encode("utf-8"))
        return zone if zone.isValid() else None
    except (ImportError, RuntimeError, TypeError, ValueError):
        return None


def _qt_datetime_from_epoch(timestamp: float, name: str) -> datetime | None:
    zone = _qt_zone(name)
    if zone is None:
        return None
    try:
        from PyQt6.QtCore import QDateTime

        value = QDateTime.fromMSecsSinceEpoch(round(timestamp * 1000), zone)
        qdate = value.date()
        qtime = value.time()
        offset = value.offsetFromUtc()
        return datetime(
            qdate.year(), qdate.month(), qdate.day(),
            qtime.hour(), qtime.minute(), qtime.second(), qtime.msec() * 1000,
            tzinfo=timezone(timedelta(seconds=offset)),
        )
    except (ImportError, RuntimeError, TypeError, ValueError, OverflowError):
        return None


def _local_wall_to_utc(
    target: date,
    hour: int,
    minute: int,
    second: int,
    name: str,
    *,
    microsecond: int = 0,
) -> datetime:
    zone = _stdlib_zone(name)
    if zone is not None:
        local = datetime.combine(
            target,
            time(hour, minute, second, microsecond),
            tzinfo=zone,
        )
        return local.astimezone(UTC)

    qt_zone = _qt_zone(name)
    if qt_zone is not None:
        try:
            from PyQt6.QtCore import QDate, QDateTime, QTime

            value = QDateTime(
                QDate(target.year, target.month, target.day),
                QTime(hour, minute, second, microsecond // 1000),
                qt_zone,
            )
            if value.isValid():
                return datetime.fromtimestamp(value.toMSecsSinceEpoch() / 1000, UTC)
        except (ImportError, RuntimeError, TypeError, ValueError, OverflowError):
            pass

    local = datetime.combine(
        target,
        time(hour, minute, second, microsecond),
        tzinfo=_fallback_zone(name),
    )
    return local.astimezone(UTC)
