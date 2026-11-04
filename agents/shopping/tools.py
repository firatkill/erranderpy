"""Shopping list tools.

The model only decides *which* tool and *which* items; everything else (matching
names, counting, writing the reply) happens here in plain code.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from erranderpy.core.db import utc_now_iso
from erranderpy.core.text import find_unique, normalize
from erranderpy.core.tools import ToolContext, tool


class Item(BaseModel):
    name: str = Field(description="Item name in lowercase dictionary form, e.g. 'zeytinyağı'")
    quantity: int | None = Field(default=None, ge=1, description="Number of units, only if the user gave one")


@tool
def add_items(ctx: ToolContext, items: list[Item]) -> str:
    """Add one or more items to the shopping list."""
    added, updated, already = [], [], []
    for item in items:
        name = " ".join(item.name.split())
        key = normalize(name)
        if not key:
            continue
        row = _find_pending(ctx, key)
        if row is None:
            ctx.db.execute(
                "INSERT INTO shopping_items (user_id, name, name_key, quantity, created_at) VALUES (?, ?, ?, ?, ?)",
                (ctx.user_id, name, key, item.quantity, utc_now_iso()),
            )
            added.append(_label(name, item.quantity))
        elif item.quantity and item.quantity != row["quantity"]:
            ctx.db.execute("UPDATE shopping_items SET quantity = ? WHERE id = ?", (item.quantity, row["id"]))
            updated.append(_label(row["name"], item.quantity))
        else:
            already.append(row["name"])

    parts = []
    if added:
        parts.append(ctx.t("added", items=", ".join(added)))
    if updated:
        parts.append(ctx.t("updated", items=", ".join(updated)))
    if already:
        parts.append(ctx.t("already", items=", ".join(already)))
    parts.append(ctx.t("count", count=_pending_count(ctx)))
    return " ".join(parts)


@tool
def remove_items(ctx: ToolContext, names: list[str]) -> str:
    """Remove items from the list without buying them (changed your mind, already have it)."""
    return _close(ctx, names, status="removed", done_key="removed")


@tool
def mark_bought(ctx: ToolContext, names: list[str]) -> str:
    """Mark items on the list as bought."""
    return _close(ctx, names, status="bought", done_key="bought")


@tool
def list_items(ctx: ToolContext) -> str:
    """Show the items still to buy and what was bought today."""
    pending = ctx.db.query(
        "SELECT name, quantity FROM shopping_items WHERE user_id = ? AND status = 'pending' ORDER BY id",
        (ctx.user_id,),
    )
    bought = ctx.db.query(
        "SELECT name FROM shopping_items WHERE user_id = ? AND status = 'bought' AND done_at >= ? ORDER BY done_at",
        (ctx.user_id, ctx.today_start_utc()),
    )
    lines = []
    if pending:
        lines.append(ctx.t("list_header"))
        lines += [f"• {_label(r['name'], r['quantity'])}" for r in pending]
    else:
        lines.append(ctx.t("list_empty"))
    if bought:
        lines.append("")
        lines.append(ctx.t("bought_today", items=", ".join(r["name"] for r in bought)))
    return "\n".join(lines)


# --- helpers -------------------------------------------------------------------


def _close(ctx: ToolContext, names: list[str], status: str, done_key: str) -> str:
    done, missing = [], []
    for name in names:
        row = _match_pending(ctx, normalize(name))
        if row is None:
            missing.append(name)
            continue
        ctx.db.execute(
            "UPDATE shopping_items SET status = ?, done_at = ? WHERE id = ?", (status, utc_now_iso(), row["id"])
        )
        done.append(row["name"])
    parts = []
    if done:
        parts.append(ctx.t(done_key, items=", ".join(done)))
    if missing:
        parts.append(ctx.t("not_found", items=", ".join(missing)))
    parts.append(ctx.t("count", count=_pending_count(ctx)))
    return " ".join(parts)


def _find_pending(ctx: ToolContext, key: str):
    return ctx.db.one(
        "SELECT id, name, quantity FROM shopping_items WHERE user_id = ? AND status = 'pending' AND name_key = ?",
        (ctx.user_id, key),
    )


def _match_pending(ctx: ToolContext, key: str):
    """Exact name first; otherwise a loose match ("zeytin" -> "zeytinyağı"), but
    only if it is unambiguous: "yağ" must not silently close "zeytinyağı" when
    "ayçiçek yağı" is also on the list."""
    rows = ctx.db.query(
        "SELECT id, name, name_key FROM shopping_items WHERE user_id = ? AND status = 'pending'", (ctx.user_id,)
    )
    return find_unique(key, [(r["name"], r) for r in rows])


def _pending_count(ctx: ToolContext) -> int:
    row = ctx.db.one(
        "SELECT COUNT(*) AS n FROM shopping_items WHERE user_id = ? AND status = 'pending'", (ctx.user_id,)
    )
    return row["n"]


def _label(name: str, quantity: int | None) -> str:
    return f"{quantity} × {name}" if quantity else name
