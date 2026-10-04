"""Persistent success markers and transient errors for record synchronization."""

from __future__ import annotations

import time
from typing import Any, Callable


LOCAL_READ = "local_read"
LOCAL_SAVE = "local_save"
UPLOAD = "upload"
MEMBERS = "members"
STAGES = frozenset((LOCAL_READ, LOCAL_SAVE, UPLOAD, MEMBERS))
ISSUE_PRIORITY = (LOCAL_SAVE, LOCAL_READ, UPLOAD, MEMBERS)


def merge_pending_summaries(*summaries: dict[str, Any] | None) -> dict[str, Any]:
    """Combine independent durable queues into one status summary."""
    present = [summary for summary in summaries if isinstance(summary, dict)]
    timestamps = [
        float(summary["oldest_queued_at"])
        for summary in present
        if summary.get("oldest_queued_at") is not None
    ]
    ages = [
        max(0.0, float(summary["oldest_age_seconds"]))
        for summary in present
        if summary.get("oldest_age_seconds") is not None
    ]
    return {
        "count": sum(max(0, int(summary.get("count", 0) or 0)) for summary in present),
        "oldest_queued_at": min(timestamps) if timestamps else None,
        "oldest_age_seconds": max(ages) if ages else None,
    }


class RecordStatusLedger:
    def __init__(
        self,
        state: dict[str, Any],
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.state = state
        self.state.setdefault("success", {})
        self.errors: dict[str, str] = {}
        self.clock = clock

    @staticmethod
    def _scope(stage: str, user_id: str | None, group_id: str | None) -> str:
        if stage not in STAGES:
            raise ValueError("unknown record status stage")
        if stage in (LOCAL_READ, LOCAL_SAVE):
            return "local"
        if not user_id or not group_id:
            raise ValueError("account and room are required for remote status")
        return f"{user_id}|{group_id}"

    def _key(self, stage: str, user_id: str | None, group_id: str | None) -> str:
        return f"{self._scope(stage, user_id, group_id)}|{stage}"

    def mark_success(
        self,
        stage: str,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        at: float | None = None,
    ) -> float:
        value = float(self.clock() if at is None else at)
        key = self._key(stage, user_id, group_id)
        self.state["success"][key] = value
        self.errors.pop(key, None)
        return value

    def success_at(
        self, stage: str, *, user_id: str | None = None, group_id: str | None = None
    ) -> float | None:
        value = self.state["success"].get(self._key(stage, user_id, group_id))
        return float(value) if value is not None else None

    def restore_success(
        self,
        stage: str,
        value: float | None,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        key = self._key(stage, user_id, group_id)
        if value is None:
            self.state["success"].pop(key, None)
        else:
            self.state["success"][key] = float(value)

    def set_error(
        self,
        stage: str,
        message: Any,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        self.errors[self._key(stage, user_id, group_id)] = str(message or stage)

    def clear_error(
        self,
        stage: str,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        self.errors.pop(self._key(stage, user_id, group_id), None)

    def snapshot(
        self,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        pending: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "local_read_at": self.success_at(LOCAL_READ),
            "local_save_at": self.success_at(LOCAL_SAVE),
            "upload_at": None,
            "members_at": None,
            "pending_count": int((pending or {}).get("count", 0) or 0),
            "oldest_pending_age_seconds": (pending or {}).get("oldest_age_seconds"),
            "errors": {},
            "primary_issue": None,
        }
        if user_id and group_id:
            result["upload_at"] = self.success_at(
                UPLOAD, user_id=user_id, group_id=group_id
            )
            result["members_at"] = self.success_at(
                MEMBERS, user_id=user_id, group_id=group_id
            )
        for stage in ISSUE_PRIORITY:
            try:
                key = self._key(stage, user_id, group_id)
            except ValueError:
                continue
            if key in self.errors:
                result["errors"][stage] = self.errors[key]
                if result["primary_issue"] is None:
                    result["primary_issue"] = stage
        age = result["oldest_pending_age_seconds"]
        if result["primary_issue"] is None and age is not None and float(age) >= 60:
            result["primary_issue"] = "pending"
        return result
