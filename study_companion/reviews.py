"""Durable, stdlib-only tracking for synced Anki review history.

The collector deliberately treats removal as an explicit operation.  A normal
observation can be incomplete (for example before a sync finishes), so missing
rows never erase reviews unless ``allow_removals`` is true. Versioned explicit
undo/redo observations override earlier changes; normal stale snapshots do not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .activity import normalize_review_time_ms
from .study_day import DEFAULT_TIME_ZONE, study_day


MAX_BATCH_ITEMS = 500
VALID_EASES = {1, 2, 3, 4}
VALID_REVIEW_TYPES = {0, 1, 2, 3}


class ReviewHistory:
    """Collect deduplicated review rows and track per-room delivery."""

    def __init__(
        self,
        state: dict[str, Any],
        now_ms: Callable[[], int] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.state = state
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._clock = clock or time.time
        if not isinstance(self.state.get("collections"), dict):
            self.state["collections"] = {}
        if not isinstance(self.state.get("routes"), dict):
            self.state["routes"] = {}
        self.state["version"] = 1
        self._normalize_cached_review_times()

    def migrate_study_days(
        self,
        time_zone: str = DEFAULT_TIME_ZONE,
        *,
        scheme: str = "room-04-v1",
    ) -> bool:
        """Re-key cached review events for the room's 04:00 study day.

        Delivery acknowledgements refer to the old day keys, so they are
        invalidated and the same stable review identities are offered again.
        Review events and tombstones themselves are preserved.  The operation
        is marker-guarded and therefore safe to call on every startup.
        """
        marker = f"{scheme}|{time_zone}"
        if self.state.get("study_day_scheme") == marker:
            return False

        for source, source_state in self.state.get("collections", {}).items():
            if not isinstance(source_state, dict):
                continue
            old_days = source_state.get("days", {})
            if not isinstance(old_days, dict):
                continue
            new_days: dict[str, dict[str, Any]] = {}
            destinations_by_old_day: dict[str, set[str]] = {}
            for old_day, old_day_state in old_days.items():
                if not isinstance(old_day_state, dict):
                    continue
                for bucket in ("events", "removed"):
                    values = old_day_state.get(bucket, {})
                    if not isinstance(values, dict):
                        continue
                    for event_key, event in values.items():
                        if not isinstance(event, dict):
                            continue
                        try:
                            instant = datetime.fromtimestamp(
                                int(event["id"]) / 1000,
                                timezone.utc,
                            )
                        except (KeyError, TypeError, ValueError, OverflowError, OSError):
                            continue
                        target_day = study_day(instant, time_zone).isoformat()
                        destinations_by_old_day.setdefault(str(old_day), set()).add(
                            target_day
                        )
                        target = new_days.setdefault(
                            target_day,
                            {
                                "observed": True,
                                "events": {},
                                "removed": {},
                                "last_observed_keys": [],
                            },
                        )
                        other_bucket = "removed" if bucket == "events" else "events"
                        existing = target[bucket].get(event_key)
                        opposing = target[other_bucket].get(event_key)
                        incoming_version = _integer(event.get("changed_at"))
                        newest_version = max(
                            _integer(existing.get("changed_at"))
                            if isinstance(existing, dict) else -1,
                            _integer(opposing.get("changed_at"))
                            if isinstance(opposing, dict) else -1,
                        )
                        if incoming_version >= newest_version:
                            target[other_bucket].pop(event_key, None)
                            target[bucket][event_key] = dict(event)
            for target in new_days.values():
                target["last_observed_keys"] = sorted(target["events"])
            source_state["days"] = new_days
            # Keep every account/room delivery scope and its original sharing
            # boundary.  Day acknowledgements are reset because their keys
            # described the old calendar, forcing stable review IDs to be
            # resent while also preserving old offline pending work.
            for groups in self.state.get("routes", {}).values():
                if not isinstance(groups, dict):
                    continue
                for route in groups.values():
                    if not isinstance(route, dict):
                        continue
                    route_source = route.get("sources", {}).get(str(source))
                    if not isinstance(route_source, dict):
                        continue
                    previous_since = route_source.get("since_day")
                    previously_routed = {
                        str(day) for day in route_source.get("days", {})
                    }
                    eligible_old_days = previously_routed.union(
                        str(day)
                        for day in old_days
                        if previous_since is None or str(day) >= str(previous_since)
                    )
                    eligible_destinations: set[str] = set()
                    for old_day in eligible_old_days:
                        eligible_destinations.update(
                            destinations_by_old_day.get(old_day, set())
                        )
                    route_source["days"] = {
                        day: {"reviews": {}, "removed": {}, "activated": False}
                        for day in eligible_destinations
                    }

        self.state["study_day_scheme"] = marker
        return True

    def observe(
        self,
        collection_key: str,
        day: str,
        rows: list[tuple[Any, Any, Any, Any, Any]],
        allow_removals: bool = False,
    ) -> None:
        """Observe a complete or partial set of review rows for one day.

        Rows contain ``(review_id, card_id, time_ms, ease, review_type)``.
        Only answered learning/review/relearning/filtered rows are counted.
        Missing rows are tombstoned only for an explicit removal observation.
        """
        source = str(collection_key)
        target_day = str(day)
        self.state["active_collection"] = source
        day_state = self._day_state(source, target_day)
        events = day_state["events"]
        removed = day_state["removed"]
        previous_observed_keys = set(day_state.get("last_observed_keys", []))
        now_keys: set[str] = set()

        for raw in rows:
            try:
                review_id, card_id, raw_time, raw_ease, raw_type = raw
                review_id = _identifier(review_id)
                card_id = _identifier(card_id)
            except (TypeError, ValueError):
                continue

            event_key = _event_key(review_id, card_id)
            try:
                ease = int(raw_ease)
                review_type = int(raw_type)
                time_ms = normalize_review_time_ms(raw_time)
            except (TypeError, ValueError, OverflowError):
                continue
            if ease not in VALID_EASES or review_type not in VALID_REVIEW_TYPES:
                continue
            now_keys.add(event_key)
            if event_key in removed:
                if not allow_removals or event_key in previous_observed_keys:
                    continue
                previous_version = _integer(removed[event_key].get("changed_at"))
                removed.pop(event_key)
                changed_at = self._next_version(previous_version)
            else:
                changed_at = _integer(events.get(event_key, {}).get("changed_at"))
            events[event_key] = {
                "id": review_id,
                "card_id": card_id,
                "time_ms": time_ms,
                "changed_at": changed_at,
            }

        if allow_removals:
            removable = previous_observed_keys.intersection(events).difference(now_keys)
            for event_key in removable:
                event = events.pop(event_key)
                removed[event_key] = {
                    "id": event["id"],
                    "card_id": event["card_id"],
                    "changed_at": self._next_version(
                        _integer(event.get("changed_at"))
                    ),
                }
        day_state["last_observed_keys"] = sorted(now_keys)
        day_state["observed"] = True

    def today(self, day: str) -> dict[str, float | int] | None:
        """Return active collection totals, or ``None`` if unknown."""
        target_day = str(day)
        observed = False
        total_ms = 0
        answers = 0
        active_collection = self.state.get("active_collection")
        source_state = self.state["collections"].get(active_collection, {})
        day_state = source_state.get("days", {}).get(target_day)
        if isinstance(day_state, dict) and day_state.get("observed"):
            observed = True
            for event in day_state.get("events", {}).values():
                if isinstance(event, dict):
                    total_ms += _cached_review_time_ms(event.get("time_ms"))
                    answers += 1
        if not observed:
            return None
        return {"seconds": total_ms / 1000, "answers": answers}

    def pending(
        self,
        user_id: str,
        group_id: str,
        since_day: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return unsent review changes for one account/room route.

        Batches contain no more than 500 review additions and removals in
        total.  An observed empty day emits one activation batch until it is
        acknowledged, allowing the server to distinguish zero from unknown.
        """
        route = self._route_state(str(user_id), str(group_id))
        batches: list[dict[str, Any]] = []
        for source in sorted(self.state["collections"]):
            source_state = self.state["collections"].get(source)
            if not isinstance(source_state, dict):
                continue
            existing_source = route["sources"].get(source)
            route_source = route["sources"].setdefault(source, {"days": {}})
            route_days = route_source.setdefault("days", {})
            if since_day is not None and "since_day" not in route_source:
                if isinstance(existing_source, dict):
                    # A route written by an older add-on may contain delivery
                    # state but no sharing boundary. Preserve ambiguous local
                    # history rather than silently skipping it during upgrade.
                    observed_days = source_state.get("days", {})
                    route_source["since_day"] = min(
                        (str(day) for day in observed_days),
                        default=str(since_day),
                    )
                else:
                    route_source["since_day"] = str(since_day)
            route_since_day = route_source.get("since_day")
            for day in sorted(source_state.get("days", {})):
                # A newly joined room only needs the recent sharing window.
                # An older day already attempted for this route is still
                # retried so an extended offline period never loses data.
                if route_since_day is not None and day < str(route_since_day) and day not in route_days:
                    continue
                day_state = source_state["days"].get(day)
                if not isinstance(day_state, dict) or not day_state.get("observed"):
                    continue
                ack = route_days.setdefault(
                    day,
                    {"reviews": {}, "removed": {}, "activated": False},
                )
                ack_reviews = ack.setdefault("reviews", {})
                ack_removed = ack.setdefault("removed", {})

                reviews = []
                for event_key in sorted(day_state.get("events", {})):
                    event = day_state["events"][event_key]
                    sent_value = {
                        "time_ms": _cached_review_time_ms(event.get("time_ms")),
                        "changed_at": max(0, _integer(event.get("changed_at"))),
                    }
                    if ack_reviews.get(event_key) == sent_value:
                        continue
                    reviews.append(
                        {
                            "id": str(event["id"]),
                            "card_id": str(event["card_id"]),
                            **sent_value,
                        }
                    )

                removals = []
                for event_key in sorted(day_state.get("removed", {})):
                    event = day_state["removed"][event_key]
                    changed_at = max(0, _integer(event.get("changed_at")))
                    if ack_removed.get(event_key) == changed_at:
                        continue
                    removals.append(
                        {
                            "id": str(event["id"]),
                            "card_id": str(event["card_id"]),
                            "changed_at": changed_at,
                        }
                    )

                batches.extend(_chunks(source, day, reviews, removals))
                if not reviews and not removals and not ack.get("activated"):
                    batches.append(_batch(source, day, [], []))
        self._update_pending_since(route, bool(batches))
        return batches

    def pending_summary(
        self,
        user_id: str,
        group_id: str,
        since_day: str | None = None,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Describe pending review batches for one account and room.

        The first detected pending time is stored with the delivery route, so
        restarting Anki does not reset the displayed delay. A late
        acknowledgement only clears it when no newer review change remains.
        Times returned here are Unix seconds, matching ``SyncOutbox``.
        """
        batches = self.pending(user_id, group_id, since_day=since_day)
        route = self._route_state(str(user_id), str(group_id))
        pending_since = route.get("pending_since") if batches else None
        try:
            oldest = float(pending_since)
        except (TypeError, ValueError, OverflowError):
            oldest = None
        current = float(self._clock()) if now is None else float(now)
        return {
            "count": len(batches),
            "oldest_queued_at": oldest,
            "oldest_age_seconds": (
                max(0.0, current - oldest) if oldest is not None else None
            ),
        }

    def mark_route_days(
        self,
        user_id: str,
        group_id: str,
        source: str,
        days: list[str],
        *,
        since_day: str | None = None,
    ) -> None:
        """Protect locally observed room days until delivery is acknowledged."""
        source_state = self.state["collections"].get(str(source), {})
        observed_days = source_state.get("days", {})
        route = self._route_state(str(user_id), str(group_id))
        source_key = str(source)
        existing_source = route["sources"].get(source_key)
        route_source = route["sources"].setdefault(source_key, {"days": {}})
        if since_day is not None and "since_day" not in route_source:
            route_source["since_day"] = (
                min((str(day) for day in observed_days), default=str(since_day))
                if isinstance(existing_source, dict)
                else str(since_day)
            )
        route_days = route_source.setdefault("days", {})
        for day in days:
            target_day = str(day)
            day_state = observed_days.get(target_day)
            if not isinstance(day_state, dict) or not day_state.get("observed"):
                continue
            route_days.setdefault(
                target_day,
                {"reviews": {}, "removed": {}, "activated": False},
            )

    def compact(self, before_day: str, *, discard_unrouted: bool = False) -> int:
        """Discard old local cache days that have no unfinished delivery.

        Native Anki revlog remains the source of truth.  A day is retained if
        any existing room route has an unacknowledged activation, review, or
        removal for it.  Routes created later do not make historical days
        pending retroactively.
        """
        cutoff = str(before_day)
        removed_days = 0
        collections = self.state.get("collections", {})
        routes = self.state.get("routes", {})
        for source, source_state in list(collections.items()):
            if not isinstance(source_state, dict):
                continue
            days = source_state.get("days", {})
            if not isinstance(days, dict):
                continue
            for day, day_state in list(days.items()):
                if day >= cutoff or not isinstance(day_state, dict):
                    continue
                acknowledgements = []
                route_can_require_delivery = False
                for groups in routes.values():
                    if not isinstance(groups, dict):
                        continue
                    for route in groups.values():
                        if not isinstance(route, dict):
                            continue
                        route_source = route.get("sources", {}).get(source, {})
                        if not isinstance(route_source, dict):
                            continue
                        if not route_source:
                            route_can_require_delivery = True
                            continue
                        route_day = route_source.get("days", {}).get(day)
                        if isinstance(route_day, dict):
                            acknowledgements.append(route_day)
                            continue
                        route_since_day = route_source.get("since_day")
                        if route_since_day is None or day >= str(route_since_day):
                            route_can_require_delivery = True
                if route_can_require_delivery:
                    continue
                if not acknowledgements and not discard_unrouted and not routes:
                    continue
                if any(
                    not _fully_acknowledged(day_state, acknowledgement)
                    for acknowledgement in acknowledgements
                ):
                    continue
                days.pop(day, None)
                removed_days += 1
                for groups in routes.values():
                    if not isinstance(groups, dict):
                        continue
                    for route in groups.values():
                        if not isinstance(route, dict):
                            continue
                        route_days = (
                            route.get("sources", {})
                            .get(source, {})
                            .get("days", {})
                        )
                        if isinstance(route_days, dict):
                            route_days.pop(day, None)
            if not days:
                collections.pop(source, None)
        return removed_days

    def acknowledge(
        self,
        user_id: str,
        group_id: str,
        batch: dict[str, Any],
    ) -> None:
        """Acknowledge exactly the values captured in ``batch``.

        If observations changed while a request was in flight, the old values
        are recorded as acknowledged and the newer values remain pending.
        """
        source = str(batch.get("source_collection", ""))
        day = str(batch.get("target_day", ""))
        if not source or not day:
            return
        route = self._route_state(str(user_id), str(group_id))
        source_state = route["sources"].setdefault(source, {"days": {}})
        ack = source_state.setdefault("days", {}).setdefault(
            day,
            {"reviews": {}, "removed": {}, "activated": False},
        )
        ack_reviews = ack.setdefault("reviews", {})
        ack_removed = ack.setdefault("removed", {})

        for event in batch.get("reviews", []):
            try:
                review_id = _identifier(event["id"])
                card_id = _identifier(event["card_id"])
                time_ms = normalize_review_time_ms(event["time_ms"])
                changed_at = max(0, int(event["changed_at"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            event_key = _event_key(review_id, card_id)
            ack_reviews[event_key] = {
                "time_ms": time_ms,
                "changed_at": changed_at,
            }
            ack_removed.pop(event_key, None)

        for event in batch.get("removed_ids", []):
            try:
                review_id = _identifier(event["id"])
                card_id = _identifier(event["card_id"])
                changed_at = max(0, int(event["changed_at"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            event_key = _event_key(review_id, card_id)
            ack_removed[event_key] = changed_at
            ack_reviews.pop(event_key, None)
        ack["activated"] = True
        # Re-evaluate after applying this exact acknowledgement. If a newer
        # observation arrived while the request was in flight, ``pending``
        # remains non-empty and preserves the original waiting time.
        self.pending(str(user_id), str(group_id))

    def invalidate_route(self, user_id: str, group_id: str) -> None:
        """Forget delivery acknowledgements after a room membership is reset."""
        groups = self.state["routes"].get(str(user_id))
        if not isinstance(groups, dict):
            return
        groups.pop(str(group_id), None)
        if not groups:
            self.state["routes"].pop(str(user_id), None)

    def _day_state(self, source: str, day: str) -> dict[str, Any]:
        source_state = self.state["collections"].setdefault(source, {"days": {}})
        days = source_state.setdefault("days", {})
        day_state = days.setdefault(
            day,
            {
                "observed": False,
                "events": {},
                "removed": {},
                "last_observed_keys": [],
            },
        )
        day_state.setdefault("events", {})
        day_state.setdefault("removed", {})
        day_state.setdefault("last_observed_keys", [])
        return day_state

    def _route_state(self, user_id: str, group_id: str) -> dict[str, Any]:
        users = self.state["routes"]
        groups = users.setdefault(user_id, {})
        return groups.setdefault(group_id, {"sources": {}})

    def _update_pending_since(self, route: dict[str, Any], has_pending: bool) -> None:
        if has_pending:
            if route.get("pending_since") is None:
                route["pending_since"] = max(0.0, float(self._clock()))
        else:
            route.pop("pending_since", None)

    def _next_version(self, previous: int) -> int:
        return max(_integer(self._now_ms()), previous + 1, 1)

    def _normalize_cached_review_times(self) -> None:
        """Heal legacy event and acknowledgement values in-place."""
        for source_state in self.state["collections"].values():
            if not isinstance(source_state, dict):
                continue
            days = source_state.get("days", {})
            if not isinstance(days, dict):
                continue
            for day_state in days.values():
                if not isinstance(day_state, dict):
                    continue
                events = day_state.get("events", {})
                if not isinstance(events, dict):
                    continue
                for event in events.values():
                    if isinstance(event, dict):
                        event["time_ms"] = _cached_review_time_ms(
                            event.get("time_ms")
                        )

        for groups in self.state["routes"].values():
            if not isinstance(groups, dict):
                continue
            for route in groups.values():
                if not isinstance(route, dict):
                    continue
                sources = route.get("sources", {})
                if not isinstance(sources, dict):
                    continue
                for route_source in sources.values():
                    if not isinstance(route_source, dict):
                        continue
                    days = route_source.get("days", {})
                    if not isinstance(days, dict):
                        continue
                    for acknowledgement in days.values():
                        if not isinstance(acknowledgement, dict):
                            continue
                        reviews = acknowledgement.get("reviews", {})
                        if not isinstance(reviews, dict):
                            continue
                        for sent_value in reviews.values():
                            if isinstance(sent_value, dict):
                                sent_value["time_ms"] = _cached_review_time_ms(
                                    sent_value.get("time_ms")
                                )


def _chunks(
    source: str,
    day: str,
    reviews: list[dict[str, Any]],
    removals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    batches = []
    review_index = 0
    removal_index = 0
    while review_index < len(reviews) or removal_index < len(removals):
        selected_reviews = reviews[review_index : review_index + MAX_BATCH_ITEMS]
        review_index += len(selected_reviews)
        capacity = MAX_BATCH_ITEMS - len(selected_reviews)
        selected_removals = removals[removal_index : removal_index + capacity]
        removal_index += len(selected_removals)
        batches.append(_batch(source, day, selected_reviews, selected_removals))
    return batches


def _batch(
    source: str,
    day: str,
    reviews: list[dict[str, Any]],
    removals: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source_collection": source,
        "target_day": day,
        "reviews": reviews,
        "removed_ids": removals,
    }


def _identifier(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean is not an identifier")
    return str(int(value))


def _event_key(review_id: str, card_id: str) -> str:
    return f"{review_id}:{card_id}"


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _cached_review_time_ms(value: Any) -> int:
    return normalize_review_time_ms(_integer(value))


def _fully_acknowledged(
    day_state: dict[str, Any], acknowledgement: dict[str, Any]
) -> bool:
    if not acknowledgement.get("activated"):
        return False
    acknowledged_reviews = acknowledgement.get("reviews", {})
    acknowledged_removals = acknowledgement.get("removed", {})
    for event_key, event in day_state.get("events", {}).items():
        expected = {
            "time_ms": _cached_review_time_ms(event.get("time_ms")),
            "changed_at": max(0, _integer(event.get("changed_at"))),
        }
        if acknowledged_reviews.get(event_key) != expected:
            return False
    for event_key, event in day_state.get("removed", {}).items():
        if acknowledged_removals.get(event_key) != max(
            0, _integer(event.get("changed_at"))
        ):
            return False
    return True
