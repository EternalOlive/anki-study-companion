"""Supabase API client and room management RPC callers.

Provides methods and functions to invoke Supabase RPCs:
- update_room_timezone
- transfer_room_ownership
- kick_room_member
- cleanup_inactive_members
- poke_room_member / fetch_my_pokes (room member pokes)
"""

from __future__ import annotations

from typing import Any
from .online import PokeUnavailable, SupabaseClient, SupabaseError


def update_room_timezone(
    client: SupabaseClient, token: str, group_id: str, timezone_name: str
) -> Any:
    """Change the room time zone (room owner only)."""
    return client.update_room_timezone(token, group_id, timezone_name)


def update_room_public(
    client: SupabaseClient, token: str, group_id: str, is_public: bool
) -> Any:
    """Change the room public/private status (room owner only)."""
    return client.update_room_public(token, group_id, is_public)


def transfer_room_ownership(
    client: SupabaseClient, token: str, group_id: str, new_owner_id: str
) -> Any:
    """Transfer room ownership to another member (room owner only)."""
    return client.transfer_room_ownership(token, group_id, new_owner_id)


def kick_room_member(
    client: SupabaseClient, token: str, group_id: str, target_user: str
) -> Any:
    """Kick a member from the room, excluding the owner (room owner only)."""
    return client.kick_room_member(token, group_id, target_user)


def cleanup_inactive_members(
    client: SupabaseClient, token: str, group_id: str, days: int = 14
) -> int:
    """Remove non-owner members who have not synced for 14+ days (room owner only)."""
    return client.cleanup_inactive_members(token, group_id, days)


def list_public_study_groups(
    client: SupabaseClient, token: str, timezone_name: str = "Asia/Seoul"
) -> list[dict[str, Any]]:
    """List open public study groups with available capacity."""
    return client.list_public_study_groups(token, timezone_name)


def poke_room_member(
    client: SupabaseClient, token: str, group_id: str, target_user: str
) -> str | None:
    """Poke another member of the same room; returns the server created_at."""
    return client.poke_room_member(token, group_id, target_user)


def fetch_my_pokes(
    client: SupabaseClient, token: str, group_id: str, since: str | None = None
) -> list[dict[str, Any]]:
    """Fetch unseen pokes to the caller in the room and mark them seen."""
    return client.fetch_my_pokes(token, group_id, since)


__all__ = [
    "PokeUnavailable",
    "SupabaseClient",
    "SupabaseError",
    "update_room_timezone",
    "update_room_public",
    "transfer_room_ownership",
    "kick_room_member",
    "cleanup_inactive_members",
    "list_public_study_groups",
    "poke_room_member",
    "fetch_my_pokes",
]
