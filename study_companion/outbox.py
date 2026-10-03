"""Persistent, account-scoped queue for idempotent study snapshots."""

from __future__ import annotations

from typing import Any


class SyncOutbox:
    """Keep exact device snapshots until the server acknowledges their revision.

    Entries are deliberately scoped by account, room, device, and study day.
    This prevents a later login or room change from inheriting study that was
    recorded for another identity.  Newer snapshots replace older snapshots
    for the same route because device counters are cumulative and revisions
    are monotonic.
    """

    def __init__(self, state: dict[str, Any]):
        self.state = state
        self.state.setdefault("entries", {})
        self.state.setdefault("routes", {})
        self.state.setdefault("route_sequence", 0)
        self.state.setdefault("legacy_owners", {})

    def bind_device(self, device_id: str) -> None:
        previous = self.state.get("installation_id")
        if previous and previous != device_id:
            # A copied Anki profile must not replay another installation's
            # queued device stream.
            self.state.clear()
            self.state.update(
                {"entries": {}, "routes": {}, "route_sequence": 0,
                 "legacy_owners": {}}
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
        self.state["entries"][key] = dict(payload)

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
