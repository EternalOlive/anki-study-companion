"""UI-independent validation and text for study-room actions."""

from __future__ import annotations

from typing import Any


INVITE_CODE_ALPHABET = frozenset("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
TIME_GOAL_MAX_MINUTES = 1440
ANSWER_GOAL_MAX = 10000


def normalize_invite_code(value: Any) -> str:
    return "".join(str(value or "").split()).upper()


def validate_invite_code(value: Any) -> str:
    code = normalize_invite_code(value)
    if len(code) != 4 or any(character not in INVITE_CODE_ALPHABET for character in code):
        raise ValueError("invalid invite code")
    return code


def normalize_room_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("room name is required")
    return name


def validate_goal(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("goal must be an integer")
    if isinstance(value, int):
        goal = value
    elif isinstance(value, str) and value.strip().isdigit():
        goal = int(value.strip())
    else:
        raise ValueError("goal must be an integer")
    if goal < 0 or goal > maximum:
        raise ValueError(f"goal must be between 0 and {maximum}")
    return goal


def build_invite_message(
    group: dict[str, Any], locale: str = "ko", setup_url: str | None = None
) -> str:
    """Return a safe, ready-to-share invite using current room data only."""
    name = normalize_room_name(group.get("name"))
    code = validate_invite_code(group.get("invite_code"))
    setup = str(setup_url or "").strip()
    if locale == "en":
        lines = [
            f"Anki study room · {name}",
            f"Invite code: {code}",
            f"Study panel → Join with a code → enter {code}",
        ]
        if setup:
            lines.append(f"Setup: {setup}")
    else:
        lines = [
            f"Anki 스터디방 · {name}",
            f"초대 코드: {code}",
            f"스터디 패널 → 코드로 참여 → {code} 입력",
        ]
        if setup:
            lines.append(f"설치·참여 안내: {setup}")
    return "\n".join(lines)
