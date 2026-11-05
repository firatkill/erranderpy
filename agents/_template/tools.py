"""{{Name}} tools. (Created from agents/_template; see agents/README.md.)

The model only decides *which* tool and *which* arguments. Everything else
(storing, matching, counting, writing the reply) is plain code here.
"""

from __future__ import annotations

from erranderpy.core.db import utc_now_iso
from erranderpy.core.text import find_unique
from erranderpy.core.tools import ToolContext, tool

TABLE = "{{name}}_entries"  # created by schema.sql


@tool
def add_entry(ctx: ToolContext, text: str) -> str:
    """Save a new entry. `text` is what to keep, in the user's words."""
    text = " ".join(text.split())
    cur = ctx.db.execute(
        f"INSERT INTO {TABLE} (user_id, text, created_at) VALUES (?, ?, ?)", (ctx.user_id, text, utc_now_iso())
    )
    return ctx.t("added", id=cur.lastrowid, text=text)


@tool
def list_entries(ctx: ToolContext) -> str:
    """Show the saved entries to the user."""
    rows = ctx.db.query(f"SELECT id, text FROM {TABLE} WHERE user_id = ? ORDER BY id", (ctx.user_id,))
    if not rows:
        return ctx.t("empty")
    return "\n".join([ctx.t("header"), *(f"#{r['id']} · {r['text']}" for r in rows)])


@tool
def delete_entry(ctx: ToolContext, entry: str) -> str:
    """Delete one entry, by its #number or words from it."""
    rows = ctx.db.query(f"SELECT id, text FROM {TABLE} WHERE user_id = ?", (ctx.user_id,))
    key = entry.strip().lstrip("#")
    row = next((r for r in rows if str(r["id"]) == key), None) or find_unique(entry, [(r["text"], r) for r in rows])
    if row is None:
        return ctx.t("not_found", entry=entry)
    ctx.db.execute(f"DELETE FROM {TABLE} WHERE id = ?", (row["id"],))
    return ctx.t("deleted", text=row["text"])
