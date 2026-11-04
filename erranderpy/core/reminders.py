"""One-off reminders: "send this text at that moment".

Reminders are rows in the `reminders` table, not in-memory timers, so they
survive restarts. The gateway checks for due reminders every 30 seconds
(`take_due`). If the bot was off when one was due, it is still sent late
(marked as such) within LATE_WINDOW; older ones are marked missed and dropped,
because "your 9:00 meeting" arriving at 18:00 only confuses.
See docs/decisions/0014-kalici-hatirlaticilar.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from erranderpy.core.db import Database, utc_now_iso

LATE_WINDOW = timedelta(hours=6)


@dataclass(frozen=True)
class DueReminder:
    id: int
    user_id: int
    agent: str
    text: str
    late: bool


def schedule(db: Database, user_id: int, agent: str, due_at: datetime, text: str, record_id: int | None = None) -> int:
    """`due_at` must be timezone-aware; it is stored in UTC."""
    if due_at.tzinfo is None:
        raise ValueError("due_at must be timezone-aware")
    cur = db.execute(
        "INSERT INTO reminders (user_id, agent, record_id, due_at, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, agent, record_id, due_at.astimezone(UTC).isoformat(timespec="seconds"), text, utc_now_iso()),
    )
    return cur.lastrowid


def cancel_for_record(db: Database, agent: str, record_id: int) -> None:
    db.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE agent = ? AND record_id = ? AND status = 'pending'",
        (agent, record_id),
    )


def take_due(db: Database, now: datetime | None = None) -> list[DueReminder]:
    """Claim every pending reminder that is due. Each one is returned once only."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    now_iso = now.isoformat(timespec="seconds")
    oldest_ok = (now - LATE_WINDOW).isoformat(timespec="seconds")
    with db.lock:  # claim and mark in one go, so two ticks can't send the same reminder
        db.execute(
            "UPDATE reminders SET status = 'missed' WHERE status = 'pending' AND due_at < ?", (oldest_ok,)
        )
        rows = db.query(
            "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at", (now_iso,)
        )
        due = []
        for row in rows:
            late = datetime.fromisoformat(row["due_at"]) < now - timedelta(minutes=5)
            db.execute(
                "UPDATE reminders SET status = ?, sent_at = ? WHERE id = ?",
                ("late" if late else "sent", now_iso, row["id"]),
            )
            due.append(DueReminder(row["id"], row["user_id"], row["agent"], row["text"], late))
    return due
