"""Tasks and habits.

Two tables (schema.sql): `habits` are rules ("every day, 30 min of English"),
`tasks` are things for one day. Each day, every active habit gets one task row;
`ensure_habit_tasks` creates them and is safe to call any number of times.

The model decides which tool and with which words; dates, minutes, matching
and replies are computed here.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import Field

from erranderpy.core.dates import format_day, resolve_day
from erranderpy.core.db import Database, utc_now_iso
from erranderpy.core.text import find_unique, normalize
from erranderpy.core.tools import ToolContext, tool

# --- tools ---------------------------------------------------------------------


@tool
def create_habit(
    ctx: ToolContext,
    name: Annotated[str, Field(description="Short lowercase name, e.g. 'ingilizce okuma'")],
    daily_minutes: Annotated[int | None, Field(ge=1, description="Minutes per day, only if the user said a duration")] = None,
) -> str:
    """Start a daily habit, or change the daily target of an existing one."""
    name = " ".join(name.split())
    existing = find_unique(name, [(h["name"], h) for h in _habits(ctx, active_only=False)])

    if existing is None:
        ctx.db.execute(
            "INSERT INTO habits (user_id, name, name_key, daily_minutes, created_at) VALUES (?, ?, ?, ?, ?)",
            (ctx.user_id, name, normalize(name), daily_minutes, utc_now_iso()),
        )
        ensure_habit_tasks(ctx.db, ctx.user_id, _today(ctx))
        if daily_minutes:
            return ctx.t("habit_created", name=name, minutes=daily_minutes)
        return ctx.t("habit_created_open", name=name)

    habit_id, habit_name = existing["id"], existing["name"]
    restarted = not existing["active"]
    if restarted:
        ctx.db.execute("UPDATE habits SET active = 1, ended_at = NULL WHERE id = ?", (habit_id,))
    if daily_minutes and daily_minutes != existing["daily_minutes"]:
        ctx.db.execute("UPDATE habits SET daily_minutes = ? WHERE id = ?", (daily_minutes, habit_id))
        # Today's task follows the new target too, unless it's already finished.
        ctx.db.execute(
            "UPDATE tasks SET target_minutes = ? WHERE habit_id = ? AND day = ? AND status = 'open'",
            (daily_minutes, habit_id, _today(ctx).isoformat()),
        )
        ensure_habit_tasks(ctx.db, ctx.user_id, _today(ctx))
        return ctx.t("habit_updated", name=habit_name, minutes=daily_minutes)
    ensure_habit_tasks(ctx.db, ctx.user_id, _today(ctx))
    if restarted:
        return ctx.t("habit_restarted", name=habit_name)
    if existing["daily_minutes"] is None and daily_minutes is None:
        # e.g. after "how many minutes?" the user says "keep it without a target"
        return ctx.t("habit_kept_open", name=habit_name)
    return ctx.t("habit_exists", name=habit_name)


@tool
def stop_habit(ctx: ToolContext, name: str) -> str:
    """Stop a daily habit for good (it can be restarted later)."""
    habit = _find_habit(ctx, name)
    if isinstance(habit, str):
        return habit
    ctx.db.execute("UPDATE habits SET active = 0, ended_at = ? WHERE id = ?", (utc_now_iso(), habit["id"]))
    # Drop today's task only if nothing was done on it yet.
    ctx.db.execute(
        "DELETE FROM tasks WHERE habit_id = ? AND day = ? AND done_minutes = 0 AND status = 'open'",
        (habit["id"], _today(ctx).isoformat()),
    )
    return ctx.t("habit_stopped", name=habit["name"])


@tool
def log_progress(ctx: ToolContext, habit: str, minutes: Annotated[int, Field(ge=1)]) -> str:
    """Record minutes spent today on an existing habit."""
    today = _today(ctx)
    ensure_habit_tasks(ctx.db, ctx.user_id, today)
    found = _find_habit(ctx, habit)
    if isinstance(found, str):
        return found
    task = ctx.db.one("SELECT * FROM tasks WHERE habit_id = ? AND day = ?", (found["id"], today.isoformat()))
    done = task["done_minutes"] + minutes
    target = task["target_minutes"]
    finished = target is None or done >= target
    ctx.db.execute(
        "UPDATE tasks SET done_minutes = ?, status = ?, done_at = ? WHERE id = ?",
        (done, "done" if finished else "open", utc_now_iso() if finished else None, task["id"]),
    )
    if target is None:
        return ctx.t("progress_open", name=found["name"], done=done)
    if finished:
        return ctx.t("progress_done", name=found["name"], done=done, target=target)
    return ctx.t("progress", name=found["name"], done=done, target=target, left=target - done)


@tool
def add_task(
    ctx: ToolContext,
    title: str,
    when: Annotated[
        str,
        Field(description="'today', 'tomorrow', an English weekday name for the next such day (e.g. 'thursday'), or YYYY-MM-DD"),
    ] = "today",
) -> str:
    """Add a one-off task for a day."""
    day = resolve_day(when, _today(ctx))
    if day is None:
        return ctx.t("bad_day", when=when)
    title = " ".join(title.split())
    exists = ctx.db.one(
        "SELECT id FROM tasks WHERE user_id = ? AND day = ? AND title_key = ? AND habit_id IS NULL AND status = 'open'",
        (ctx.user_id, day.isoformat(), normalize(title)),
    )
    if exists:
        return ctx.t("task_exists", title=title, day=_day_label(ctx, day))
    ctx.db.execute(
        "INSERT INTO tasks (user_id, day, title, title_key, created_at) VALUES (?, ?, ?, ?, ?)",
        (ctx.user_id, day.isoformat(), title, normalize(title), utc_now_iso()),
    )
    return ctx.t("task_added", title=title, day=_day_label(ctx, day))


@tool
def complete_task(ctx: ToolContext, title: str) -> str:
    """Mark a task (or today's part of a habit) as done."""
    today = _today(ctx)
    ensure_habit_tasks(ctx.db, ctx.user_id, today)
    # Today's open tasks plus overdue one-off tasks. A habit's past days are not
    # candidates (the overdue list hides them too): live, yesterday's unfinished
    # "ingilizce okuma" made today's one ambiguous and the reply was "not found".
    rows = ctx.db.query(
        "SELECT * FROM tasks WHERE user_id = ? AND status = 'open' "
        "AND (day = ? OR (day < ? AND habit_id IS NULL)) ORDER BY day DESC, id",
        (ctx.user_id, today.isoformat(), today.isoformat()),
    )
    task = find_unique(title, [(r["title"], r) for r in rows])
    if task is None:
        return ctx.t("task_not_found", title=title)
    done_minutes = max(task["done_minutes"], task["target_minutes"] or 0)
    ctx.db.execute(
        "UPDATE tasks SET status = 'done', done_minutes = ?, done_at = ? WHERE id = ?",
        (done_minutes, utc_now_iso(), task["id"]),
    )
    return ctx.t("task_done", title=task["title"])


@tool
def list_tasks(ctx: ToolContext, day: Literal["today", "tomorrow"] = "today") -> str:
    """Show the tasks and habit progress for today or tomorrow."""
    target = _today(ctx) + timedelta(days=1 if day == "tomorrow" else 0)
    return day_view(ctx, target, include_overdue=(day == "today"))


# --- shared with jobs.py -------------------------------------------------------


def ensure_habit_tasks(db: Database, user_id: int, day: date) -> None:
    """Give every active habit a task for `day`. Idempotent: the unique index on
    (habit_id, day) makes a second run insert nothing."""
    db.execute(
        "INSERT OR IGNORE INTO tasks (user_id, day, title, title_key, habit_id, target_minutes, created_at) "
        "SELECT user_id, ?, name, name_key, id, daily_minutes, ? FROM habits WHERE user_id = ? AND active = 1",
        (day.isoformat(), utc_now_iso(), user_id),
    )


def day_rows(ctx: ToolContext, day: date) -> list:
    ensure_habit_tasks(ctx.db, ctx.user_id, day)
    return ctx.db.query(
        "SELECT * FROM tasks WHERE user_id = ? AND day = ? ORDER BY habit_id IS NULL, id",
        (ctx.user_id, day.isoformat()),
    )


def task_line(ctx: ToolContext, row) -> str:
    mark = "✅" if row["status"] == "done" else ("⏳" if row["habit_id"] else "☐")
    if row["target_minutes"]:
        return f"{mark} {row['title']} · {row['done_minutes']}/{row['target_minutes']} {ctx.t('minutes_short')}"
    return f"{mark} {row['title']}"


def day_view(ctx: ToolContext, day: date, include_overdue: bool) -> str:
    rows = day_rows(ctx, day)
    overdue = []
    if include_overdue:
        overdue = ctx.db.query(
            "SELECT * FROM tasks WHERE user_id = ? AND day < ? AND status = 'open' AND habit_id IS NULL ORDER BY day",
            (ctx.user_id, day.isoformat()),
        )
    label = _day_label(ctx, day)
    if not rows and not overdue:
        return ctx.t("list_empty", day=label[:1].upper() + label[1:])
    lines = [ctx.t("list_header", day=label[:1].upper() + label[1:], date=_date_label(ctx, day))]
    lines += [task_line(ctx, r) for r in rows]
    if overdue:
        lines += ["", ctx.t("overdue_header")]
        lines += [f"☐ {r['title']} ({date.fromisoformat(r['day']):%d.%m})" for r in overdue]
    return "\n".join(lines)


# --- helpers -------------------------------------------------------------------


def _today(ctx: ToolContext) -> date:
    return ctx.now.date()


def _habits(ctx: ToolContext, active_only: bool = True) -> list:
    sql = "SELECT * FROM habits WHERE user_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY active DESC, id"
    return ctx.db.query(sql, (ctx.user_id,))


def _find_habit(ctx: ToolContext, name: str):
    """The matching active habit, or a ready-to-send message explaining why not."""
    habits = _habits(ctx)
    if not habits:
        return ctx.t("no_habits")
    found = find_unique(name, [(h["name"], h) for h in habits])
    if found is None:
        return ctx.t("habit_not_found", name=name, habits=", ".join(h["name"] for h in habits))
    return found


def _day_label(ctx: ToolContext, day: date) -> str:
    today = _today(ctx)
    if day == today:
        return ctx.t("day_today")
    if day == today + timedelta(days=1):
        return ctx.t("day_tomorrow")
    return _date_label(ctx, day)


def _date_label(ctx: ToolContext, day: date) -> str:
    return format_day(day, ctx.t("weekdays").split(","))
