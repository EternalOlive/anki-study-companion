"""Stable anonymous display codes for local guest profiles.

These codes are display names only. They are not unique credentials, login
secrets, or room invitation codes.
"""

from __future__ import annotations

import hashlib


NICKNAME_VERSION = 1
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


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
