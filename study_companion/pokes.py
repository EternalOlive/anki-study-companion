"""Pure helpers for room member pokes (찌르기).

Kept free of Qt/Anki imports so the message and fail-soft rules are testable.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# Matches the server rule: the same sender -> receiver pair in one room at most
# once per 60 seconds. The panel disables that friend's button for this long.
POKE_COOLDOWN_SECONDS = 60
POKE_HOURLY_LIMIT = 60
# When the server has no poke RPCs yet, stop calling them for this long. The
# add-on retries later so a migration applied while Anki runs is picked up.
POKE_UNAVAILABLE_RETRY_SECONDS = 30 * 60


def poke_message(
    pokes: Iterable[dict[str, Any]],
    name_for: Callable[[str], str],
    translate: Callable[[str, str], str],
) -> str | None:
    """Build one tooltip line for newly received pokes, or ``None``.

    Senders keep the order of their first poke; repeated pokes from the same
    person are counted instead of listed twice.
    """
    counts: dict[str, int] = {}
    for poke in pokes:
        if not isinstance(poke, dict):
            continue
        sender = str(poke.get("from_user") or "").strip()
        if not sender:
            continue
        counts[sender] = counts.get(sender, 0) + 1
    if not counts:
        return None
    parts_ko = []
    parts_en = []
    for sender, count in counts.items():
        name = name_for(sender)
        parts_ko.append(f"{name}님" + (f"({count}번)" if count > 1 else ""))
        parts_en.append(name + (f" (×{count})" if count > 1 else ""))
    return translate(
        ", ".join(parts_ko) + "이 콕 찔렀어요",
        ", ".join(parts_en) + " poked you",
    )
