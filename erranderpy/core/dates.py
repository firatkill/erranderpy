"""Turn the model's date words into real dates. Date math is code, never the LLM (ADR-0003).

The model is asked for simple tokens: 'today', 'tomorrow', an English weekday
name, or ISO 'YYYY-MM-DD' (and 'HH:MM' for times). Turkish day words are
accepted too, because models sometimes answer in the user's language anyway.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from erranderpy.core.text import normalize

WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "pazartesi": 0, "salı": 1, "sali": 1, "çarşamba": 2, "carsamba": 2, "perşembe": 3, "persembe": 3,
    "cuma": 4, "cumartesi": 5, "pazar": 6,
}  # fmt: skip

_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def resolve_day(when: str, today: date) -> date | None:
    """'today' / 'tomorrow' / weekday (the *next* such day, never today) / YYYY-MM-DD."""
    word = normalize(when)
    if word in ("", "today", "bugün", "bugun"):
        return today
    if word in ("tomorrow", "yarın", "yarin"):
        return today + timedelta(days=1)
    if word in WEEKDAY_NAMES:
        ahead = (WEEKDAY_NAMES[word] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    try:
        return date.fromisoformat(word)
    except ValueError:
        return None


def parse_time(text: str) -> time | None:
    match = _TIME.match(text.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return time(hours, minutes)


def parse_datetime(text: str, today: date) -> datetime | None:
    """'2026-11-11 14:00', '2026-11-11T14:00', 'tomorrow 09:30', 'thursday 14:00' -> naive local datetime."""
    value = text.strip().replace("T", " ")
    day_part, _, time_part = value.rpartition(" ")
    if not day_part:  # a single token: a date without a time is not a date-time
        return None
    day = resolve_day(day_part, today)
    at = parse_time(time_part)
    if day is None or at is None:
        return None
    return datetime.combine(day, at)


def format_day(day: date, weekdays: list[str]) -> str:
    name = weekdays[day.weekday()] if len(weekdays) == 7 else ""
    return f"{day:%d.%m} {name}".strip()


def format_datetime(moment: datetime, weekdays: list[str]) -> str:
    return f"{format_day(moment.date(), weekdays)} {moment:%H:%M}"
