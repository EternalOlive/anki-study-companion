"""Persistent, account-scoped queue for idempotent study snapshots."""

from __future__ import annotations

import time
from typing import Any


class SyncOutbox:
    """Keep exact device snapshots until the server acknowledges their revision.

    Entries are deliberately scoped by account, room, device, and study day.
    This prevents a later login or room change from inheriting study that was
    recorded for another identity.  Newer snapshots replace older snapshots
    for the same route because device counters are cumulative and revisions
    are monotonic.
    """

    def __init__(self, state: dict[str, Any], *, clock=time.time):
        self.state = state
        self.clock = clock
        self.state.setdefault("entries", {})
        self.state.setdefault("routes", {})
        self.state.setdefault("route_sequence", 0)
        self.state.setdefault("legacy_owners", {})
        self.state.setdefault("recovery_events", [])

    def bind_device(self, device_id: str) -> None:
        previous = self.state.get("installation_id")
        if previous and previous != device_id:
            # A copied Anki profile must not replay another installation's
            # queued device stream.
            self.state.clear()
            self.state.update(
                {"entries": {}, "routes": {}, "route_sequence": 0,
                 "legacy_owners": {}, "recovery_events": []}
            )
        self.state["installation_id"] = device_id

    @staticmethod
    def key(user_id: str, group_id: str, device_id: str, study_day: str) -> str:
        return "|".join((user_id, group_id, device_id, study_day))

    def enqueue(self, payload: dict[str, Any]) -> None:
        required = ("user_id", "group_id", "device_id", "study_day", "revision")
        if any(not payload.get(field) for field in required):
            raise ValueError("동기화 대기 항목의 식별 정보가 비어 있습니다")
        key = self.key(*(str(payload[field]) for field in required[:4]))
        existing = self.state["entries"].get(key)
        if existing and int(existing.get("revision", 0)) > int(payload["revision"]):
            return
        queued_at = (
            existing.get("_queued_at")
            if isinstance(existing, dict) and existing.get("_queued_at") is not None
            else self.clock()
        )
        self.state["entries"][key] = {**payload, "_queued_at": float(queued_at)}

    def pending(
        self, *, user_id: str, group_id: str, device_id: str
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.state["entries"].values()
            if row.get("user_id") == user_id
            and row.get("group_id") == group_id
            and row.get("device_id") == device_id
        ]
        # Send the current/newest day first. A stale historical failure must
        # not prevent today's presence and progress from reaching the room.
        return sorted(rows, key=lambda row: (row["study_day"], row["revision"]), reverse=True)

    def pending_summary(
        self, *, user_id: str, group_id: str, device_id: str, now: float | None = None
    ) -> dict[str, Any]:
        rows = self.pending(user_id=user_id, group_id=group_id, device_id=device_id)
        timestamps = [
            float(row["_queued_at"])
            for row in rows
            if row.get("_queued_at") is not None
        ]
        oldest = min(timestamps) if timestamps else None
        current = float(self.clock() if now is None else now)
        return {
            "count": len(rows),
            "oldest_queued_at": oldest,
            "oldest_age_seconds": max(0.0, current - oldest) if oldest is not None else None,
        }

    def acknowledge(self, payload: dict[str, Any], revision: int) -> bool:
        key = self.key(
            str(payload["user_id"]),
            str(payload["group_id"]),
            str(payload["device_id"]),
            str(payload["study_day"]),
        )
        current = self.state["entries"].get(key)
        if current and int(current.get("revision", 0)) == int(revision):
            del self.state["entries"][key]
            return True
        return False

    def resolve_server_conflict(
        self, payload: dict[str, Any], stored: dict[str, Any]
    ) -> dict[str, Any]:
        """Retire one stale retry only with matching authoritative evidence.

        The server response must identify the exact account/room/device/day and
        prove that its revision or cumulative counters differ.  A newer queued
        snapshot is never removed.  Invalid or incomplete evidence raises and
        leaves the queue untouched so the UI can offer an actionable recovery
        error instead of silently resetting data.
        """
        identity = {
            "user_id": "user_id",
            "group_id": "group_id",
            "device_id": "device_id",
            "study_day": "study_day",
        }
        for payload_field, stored_field in identity.items():
            if not payload.get(payload_field) or not stored.get(stored_field):
                raise ValueError("server recovery identity is incomplete")
            if str(payload[payload_field]) != str(stored[stored_field]):
                raise ValueError("server recovery identity does not match the queued snapshot")

        def nonnegative_integer(value: Any, field: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"invalid server recovery {field}")
            if value < 0:
                raise ValueError(f"invalid server recovery {field}")
            return value

        queued_revision = nonnegative_integer(payload.get("revision"), "queued revision")
        server_revision = nonnegative_integer(stored.get("revision"), "revision")
        queued_seconds = nonnegative_integer(
            payload.get("active_seconds"), "queued active_seconds"
        )
        server_seconds = nonnegative_integer(
            stored.get("active_seconds"), "active_seconds"
        )
        queued_answers = nonnegative_integer(
            payload.get("answer_count"), "queued answer_count"
        )
        server_answers = nonnegative_integer(
            stored.get("answer_count"), "answer_count"
        )
        if server_revision < queued_revision:
            raise ValueError("server recovery revision is older than the queued snapshot")
        if (
            server_revision == queued_revision
            and server_seconds == queued_seconds
            and server_answers == queued_answers
        ):
            raise ValueError("server response acknowledges the queued snapshot; no recovery needed")

        key = self.key(
            str(payload["user_id"]),
            str(payload["group_id"]),
            str(payload["device_id"]),
            str(payload["study_day"]),
        )
        current = self.state["entries"].get(key)
        removed = bool(
            current
            and int(current.get("revision", -1)) == queued_revision
        )
        if removed:
            del self.state["entries"][key]
        event = {
            "user_id": str(payload["user_id"]),
            "group_id": str(payload["group_id"]),
            "device_id": str(payload["device_id"]),
            "study_day": str(payload["study_day"]),
            "queued_revision": queued_revision,
            "server_revision": server_revision,
            "queued_active_seconds": queued_seconds,
            "server_active_seconds": server_seconds,
            "queued_answer_count": queued_answers,
            "server_answer_count": server_answers,
            "removed": removed,
        }
        events = self.state["recovery_events"]
        events.append(event)
        del events[:-20]
        return dict(event)

    def discard_room(self, user_id: str, group_id: str) -> int:
        doomed = [
            key
            for key, row in self.state["entries"].items()
            if row.get("user_id") == user_id and row.get("group_id") == group_id
        ]
        for key in doomed:
            del self.state["entries"][key]
        for device_id, route in list(self.state["routes"].items()):
            if route.get("user_id") == user_id and route.get("group_id") == group_id:
                del self.state["routes"][device_id]
        return len(doomed)

    def route(self, device_id: str) -> dict[str, str] | None:
        route = self.state["routes"].get(device_id)
        return dict(route) if route else None

    def claim_legacy_ledger(
        self, *, user_id: str, group_id: str, device_id: str
    ) -> bool:
        """Assign an upgraded account-only ledger to exactly one room.

        The assignment survives leaving that room. Otherwise deleting the
        active route and joining a different room could reuse all legacy
        cumulative counters in the new room.
        """
        key = f"{device_id}|{user_id}"
        owner = self.state["legacy_owners"].get(key)
        if owner is None:
            self.state["legacy_owners"][key] = group_id
            return True
        return owner == group_id

    def route_epoch(self, device_id: str) -> int | None:
        route = self.state["routes"].get(device_id)
        return int(route["epoch"]) if route and route.get("epoch") is not None else None

    def bind_route(
        self, *, user_id: str, group_id: str, device_id: str, study_day: str,
        ledger_id: str | None = None,
    ) -> None:
        previous = self.state["routes"].get(device_id)
        same_membership = (
            previous
            and previous.get("user_id") == user_id
            and previous.get("group_id") == group_id
        )
        if same_membership and previous.get("epoch") is not None:
            epoch = int(previous["epoch"])
        else:
            self.state["route_sequence"] = int(self.state["route_sequence"]) + 1
            epoch = int(self.state["route_sequence"])
        self.state["routes"][device_id] = {
            "user_id": user_id,
            "group_id": group_id,
            "study_day": study_day,
            "ledger_id": ledger_id or user_id,
            "epoch": epoch,
        }
