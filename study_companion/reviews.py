"""Durable, stdlib-only tracking for synced Anki review history.

The collector deliberately treats removal as an explicit operation.  A normal
observation can be incomplete (for example before a sync finishes), so missing
rows never erase reviews unless ``allow_removals`` is true. Versioned explicit
undo/redo observations override earlier changes; normal stale snapshots do not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


MAX_BATCH_ITEMS = 500
VALID_EASES = {1, 2, 3, 4}
VALID_REVIEW_TYPES = {0, 1, 2, 3}


class ReviewHistory:
    """Collect deduplicated review rows and track per-room delivery."""

    def __init__(
        self,
        state: dict[str, Any],
        now_ms: Callable[[], int] | None = None,
    ):
        self.state = state
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        if not isinstance(self.state.get("collections"), dict):
            self.state["collections"] = {}
        if not isinstance(self.state.get("routes"), dict):
            self.state["routes"] = {}
        self.state["version"] = 1

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
                time_ms = max(0, int(raw_time))
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
                    total_ms += max(0, _integer(event.get("time_ms")))
                    answers += 1
        if not observed:
            return None
        return {"seconds": total_ms / 1000, "answers": answers}

    def pending(self, user_id: str, group_id: str) -> list[dict[str, Any]]:
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
            route_source = route["sources"].setdefault(source, {"days": {}})
            route_days = route_source.setdefault("days", {})
            for day in sorted(source_state.get("days", {})):
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
                        "time_ms": max(0, _integer(event.get("time_ms"))),
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
        return batches

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
                time_ms = max(0, int(event["time_ms"]))
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

    def _next_version(self, previous: int) -> int:
        return max(_integer(self._now_ms()), previous + 1, 1)


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
