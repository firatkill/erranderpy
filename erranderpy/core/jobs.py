"""Scheduled agent jobs: code that runs at a time of day, not in reply to a message.

An agent's optional `jobs.py` marks functions with `@daily`:

    @daily(at="21:00", env="SUMMARY_TIME")
    def evening_summary(ctx: ToolContext) -> str | None:
        ...  # returned text is sent to the owner; None sends nothing

The gateway schedules every job (Telegram's JobQueue, built on APScheduler).
Jobs don't call the LLM: they are plain code, so they are cheap and predictable.
See ADR-0010 in AGENTS.md.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time
from typing import Any

JOB_ATTR = "__erranderpy_job__"
DISABLED = {"off", "none", "false", "0", "no"}


@dataclass(frozen=True)
class Job:
    name: str
    fn: Callable[..., Any]
    at: str  # default time of day, "HH:MM", in the user's TIMEZONE
    env: str | None = None  # env var that can override `at` (or turn the job off)
    agent: str = ""  # filled in by the registry

    def time_of_day(self, env: Mapping[str, str] | None = None) -> time | None:
        """When to run today, or None if the user turned the job off."""
        env = os.environ if env is None else env
        value = (env.get(self.env, "") if self.env else "").strip() or self.at
        if value.lower() in DISABLED:
            return None
        return parse_hhmm(value)


def daily(at: str, env: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    parse_hhmm(at)  # fail at import time on a typo, not at midnight

    def mark(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, JOB_ATTR, Job(name=fn.__name__, fn=fn, at=at, env=env))
        return fn

    return mark


def get_job(obj: Any) -> Job | None:
    return getattr(obj, JOB_ATTR, None)


def parse_hhmm(value: str) -> time:
    try:
        hours, minutes = (int(part) for part in value.strip().split(":"))
        return time(hours, minutes)
    except ValueError as exc:
        raise ValueError(f"expected a time like 21:00, got {value!r}") from exc
