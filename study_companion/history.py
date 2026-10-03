"""Pure helpers for same-deck comparisons against completed local days."""

from __future__ import annotations

from datetime import date, datetime, timedelta


TODAY_MIN_SECONDS = 5 * 60
TODAY_MIN_ANSWERS = 10
HISTORY_MIN_SECONDS = 10 * 60
HISTORY_MIN_ANSWERS = 20
BEST_WINDOW_DAYS = 30


def get_comparison(
    deck_records: dict,
    deck_id: str,
    now: datetime | date,
    mode: str = "yesterday",
) -> dict:
    """Compare today's same-deck average with one completed local day.

    ``mode`` is ``yesterday`` (the exact calendar day), ``previous`` (the most
    recent earlier recorded day), or ``best`` (the fastest valid day in the
    last 30 completed calendar days).  Missing or undersized samples remain in
    the result but never produce an extrapolated percentage.
    """
    if mode not in {"yesterday", "previous", "best"}:
        raise ValueError(f"unsupported comparison mode: {mode}")

    today_date = now.date() if isinstance(now, datetime) else now
    deck_key = str(deck_id)
    today = _record_for(deck_records, deck_key, today_date)
    reference = _select_reference(deck_records, deck_key, today_date, mode)

    today_valid = _is_valid(today, TODAY_MIN_SECONDS, TODAY_MIN_ANSWERS)
    reference_valid = _is_valid(
        reference, HISTORY_MIN_SECONDS, HISTORY_MIN_ANSWERS
    )

    if reference is None:
        reason = "reference_missing"
    elif not today_valid:
        reason = "today_below_threshold"
    elif not reference_valid:
        reason = "reference_below_threshold"
    else:
        reason = "ok"

    percent_change = None
    if reason == "ok":
        percent_change = (today["rate"] / reference["rate"] - 1) * 100

    return {
        "mode": mode,
        "deck_id": deck_key,
        "today": _with_validity(today, today_valid),
        "reference": _with_validity(reference, reference_valid),
        "comparable": reason == "ok",
        "percent_change": percent_change,
        "reason": reason,
    }


def _select_reference(
    deck_records: dict, deck_id: str, today: date, mode: str
) -> dict | None:
    if mode == "yesterday":
        return _record_for(deck_records, deck_id, today - timedelta(days=1))

    candidates = []
    earliest = today - timedelta(days=BEST_WINDOW_DAYS)
    for day_text in deck_records:
        try:
            day = date.fromisoformat(day_text)
        except (TypeError, ValueError):
            continue
        if day >= today:
            continue
        if mode == "best" and day < earliest:
            continue
        record = _record_for(deck_records, deck_id, day)
        if record is not None:
            candidates.append(record)

    if mode == "previous":
        return max(candidates, key=lambda item: item["date"], default=None)

    valid = [
        item
        for item in candidates
        if _is_valid(item, HISTORY_MIN_SECONDS, HISTORY_MIN_ANSWERS)
    ]
    return max(valid, key=lambda item: (item["rate"], item["date"]), default=None)


def _record_for(deck_records: dict, deck_id: str, day: date) -> dict | None:
    raw = deck_records.get(day.isoformat(), {}).get(deck_id)
    if not isinstance(raw, dict):
        return None
    seconds = max(0.0, _number(raw.get("seconds")))
    answers = max(0, int(_number(raw.get("answers"))))
    rate = answers * 60 / seconds if seconds > 0 else None
    return {
        "date": day.isoformat(),
        "name": raw.get("name"),
        "seconds": seconds,
        "answers": answers,
        "rate": rate,
    }


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_valid(record: dict | None, min_seconds: int, min_answers: int) -> bool:
    return bool(
        record
        and record["seconds"] >= min_seconds
        and record["answers"] >= min_answers
        and record["rate"] is not None
        and record["rate"] > 0
    )


def _with_validity(record: dict | None, valid: bool) -> dict | None:
    if record is None:
        return None
    return {**record, "valid": valid}
