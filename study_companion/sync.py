"""Sync loop helpers, state reset, and kicked member detection."""

from __future__ import annotations

from typing import Any
from . import i18n

KICKED_ERROR_PATTERNS = (
    "not a member of this group",
    "blocked from this group",
    "kicked from room",
    "kicked from group",
    "not a member",
    "member not found",
)


def is_kicked_error(error: Any) -> bool:
    """Check if an exception indicates the user is no longer a member or was kicked."""
    if error is None:
        return False
    msg = str(error).strip().casefold()
    for pattern in KICKED_ERROR_PATTERNS:
        if pattern in msg:
            return True
    return False


def is_member_missing(members: list[dict[str, Any]] | None, user_id: str) -> bool:
    """Detect if the current user is absent from a loaded room member list."""
    if not members or not user_id:
        return False
    target = str(user_id).strip()
    for row in members:
        if not isinstance(row, dict):
            continue
        member_id = str(row.get("user_id") or row.get("id") or "").strip()
        if member_id == target:
            return False
    # User is not found in the non-empty member roster
    return True


def handle_kicked_state(
    controller: Any,
    user_id: str,
    group_id: str,
    *,
    custom_notice: str | None = None,
) -> None:
    """Reset room state locally and set user-visible notification upon being kicked."""
    locale = getattr(controller, "locale", i18n.DEFAULT_LOCALE)
    notice = custom_notice or i18n.t("kicked_notification", locale)

    if hasattr(controller, "online") and isinstance(controller.online, dict):
        controller.online["recovery_notice"] = notice
        controller.online["last_error"] = notice

    if hasattr(controller, "leave_current_room_locally"):
        controller.leave_current_room_locally(user_id, group_id)
    elif hasattr(controller, "online") and isinstance(controller.online, dict):
        controller.online["group"] = None
        controller.online.pop("members", None)
        controller.online.pop("room_week_stats", None)

    if hasattr(controller, "refresh"):
        controller.refresh()
