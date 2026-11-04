"""Scheduled jobs of the tasks agent. Plain code, no LLM calls.

tools.py is always loaded before jobs.py, so its helpers can be imported
from `erranderpy_agents.tasks.tools`.
"""

from __future__ import annotations

from erranderpy.core.jobs import daily
from erranderpy.core.tools import ToolContext
from erranderpy_agents.tasks.tools import _date_label, day_rows, ensure_habit_tasks, task_line


@daily(at="00:01")
def create_daily_habit_tasks(ctx: ToolContext) -> None:
    """Put every active habit on the new day's list.

    Tools call ensure_habit_tasks too, so the list is right even if the bot was
    asleep at midnight. Running both is safe because the insert is idempotent.
    """
    ensure_habit_tasks(ctx.db, ctx.user_id, ctx.now.date())


@daily(at="21:00", env="SUMMARY_TIME")
def evening_summary(ctx: ToolContext) -> str | None:
    """Send the day's progress. Nothing to report = no message."""
    today = ctx.now.date()
    rows = day_rows(ctx, today)
    if not rows:
        return None
    done = sum(r["status"] == "done" for r in rows)
    lines = [ctx.t("summary_header", date=_date_label(ctx, today))]
    lines += [task_line(ctx, r) for r in rows]
    lines += ["", ctx.t("summary_total", done=done, total=len(rows))]
    lines.append(ctx.t("summary_all_done") if done == len(rows) else ctx.t("summary_tomorrow"))
    return "\n".join(lines)
