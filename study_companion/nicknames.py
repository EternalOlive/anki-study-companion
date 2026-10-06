"""Stable anonymous display codes and nickname validation.

These codes and nicknames are display names only. They are not unique credentials,
login secrets, or room invitation codes.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


NICKNAME_VERSION = 1
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# Custom nicknames: English letters and digits only, 2 to 16 characters.
CUSTOM_NICKNAME_RE = re.compile(r"^[a-zA-Z0-9]{2,16}$")

# Canonical guest codes: 3 alphanumeric, hyphen, 3 alphanumeric.
GUEST_CODE_RE = re.compile(r"^[2-9A-HJ-NP-Z]{3}-[2-9A-HJ-NP-Z]{3}$")


def validate_display_name(name: Any) -> bool:
    """Return True if ``name`` is a valid user-chosen nickname (2-16 alphanumeric chars)."""
    if not isinstance(name, str):
        return False
    return bool(CUSTOM_NICKNAME_RE.fullmatch(name.strip()))


def is_valid_nickname_or_code(name: Any) -> bool:
    """Return True if ``name`` is a valid custom nickname or guest code."""
    if not isinstance(name, str):
        return False
    trimmed = name.strip()
    return bool(CUSTOM_NICKNAME_RE.fullmatch(trimmed) or GUEST_CODE_RE.fullmatch(trimmed))


def sanitize_display_name(raw_name: Any, fallback_user_id: str | None = None) -> str:
    """Validate and sanitize a display name received from the server, peers, or user input.

    If the name is invalid, contains forbidden characters (HTML tags, spaces, symbols, etc.),
    or is too short/long, safely fall back to the user's canonical anonymous code.
    """
    if isinstance(raw_name, str):
        trimmed = raw_name.strip()
        if CUSTOM_NICKNAME_RE.fullmatch(trimmed) or GUEST_CODE_RE.fullmatch(trimmed):
            return trimmed

    if fallback_user_id:
        try:
            return canonical_nickname(fallback_user_id)
        except Exception:
            pass
    return "Guest"


def canonical_nickname(user_id: str) -> str:
    """Return a stable ``XXX-XXX`` display code derived from an account ID."""
    normalized = str(user_id).strip()
    if not normalized:
        raise ValueError("user_id must not be empty")
    digest = hashlib.sha256(
        f"study-companion:display-code:v{NICKNAME_VERSION}:{normalized}".encode(
            "utf-8"
        )
    ).digest()
    raw = "".join(CODE_ALPHABET[value & 31] for value in digest[:6])
    return f"{raw[:3]}-{raw[3:]}"


def localize_nickname(name: str, locale: str) -> str:
    """Keep anonymous codes and legacy names identical in every locale."""
    return name


def disambiguate_nickname(name: str, user_id: str, duplicate: bool = False) -> str:
    """Add a stable suffix only when two room members share a display code."""
    if not duplicate:
        return name
    normalized = str(user_id).strip()
    if not normalized:
        raise ValueError("user_id must not be empty")
    digest = hashlib.sha256(
        f"study-companion:display-collision:{normalized}".encode("utf-8")
    ).digest()
    suffix = "".join(CODE_ALPHABET[value & 31] for value in digest[:2])
    return f"{name} · {suffix}"
