"""Notes and ideas. (Created from agents/_template with `make new-agent NAME=notes`.)

The model decides which tool, the note's text and a few tags. Code does the
rest: storing, pulling #hashtags out of the text, Turkish-aware search
("mercimekli" finds "mercimek"), and writing every reply.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field

from erranderpy.core.db import utc_now_iso
from erranderpy.core.text import normalize, phrases_match
from erranderpy.core.tools import ToolContext, tool

TABLE = "notes_entries"  # a constant, never user input: safe in the SQL strings below
SHOW = 10  # notes per listing
HASHTAG = re.compile(r"#(\w+)")


@tool
def add_note(
    ctx: ToolContext,
    text: Annotated[str, Field(description="the note itself, in the user's words, without 'not al:'")],
    tags: Annotated[list[str], Field(max_length=3, description="0-3 short lowercase topic words, no '#'")] = [],
) -> str:
    """Save a new note or idea."""
    text = " ".join(text.split())
    all_tags = _clean_tags([*tags, *HASHTAG.findall(text)])
    now = utc_now_iso()
    cur = ctx.db.execute(
        f"INSERT INTO {TABLE} (user_id, text, tags, created_at) VALUES (?, ?, ?, ?)",
        (ctx.user_id, text, " ".join(all_tags), now),
    )
    return ctx.t("added", id=cur.lastrowid, text=text, tags=_tags_suffix(all_tags))


@tool
def find_notes(
    ctx: ToolContext,
    tag: Annotated[str | None, Field(description="a topic, e.g. 'kitap'")] = None,
    query: Annotated[str | None, Field(description="words that appear in the note")] = None,
) -> str:
    """Show notes: by tag, by words in them, or the latest ones."""
    rows = _rows(ctx)
    about = " · ".join(p for p in (f"#{_clean_tags([tag])[0]}" if tag and _clean_tags([tag]) else "", query or "") if p)
    if tag:
        wanted = normalize(tag).lstrip("#")
        rows = [r for r in rows if any(phrases_match(wanted, t) for t in r["tags"].split())]
    if query:
        rows = [r for r in rows if phrases_match(query, f"{r['text']} {r['tags']}")]
    if not rows:
        return ctx.t("none_found", about=about) if about else ctx.t("empty")
    newest = rows[::-1]
    lines = [ctx.t("header", about=f" · {about}" if about else "", count=len(rows))]
    lines += [f"#{r['id']} · {r['text']}{_tags_suffix(r['tags'].split())}" for r in newest[:SHOW]]
    if len(rows) > SHOW:
        lines.append(ctx.t("header_more", more=len(rows) - SHOW))
    return "\n".join(lines)


@tool
def append_to_note(
    ctx: ToolContext,
    note: Annotated[str, Field(description="which note: its #number or words from it")],
    text: Annotated[str, Field(description="what to add, in the user's words")],
) -> str:
    """Add text to an existing note."""
    row, error = _find(ctx, note)
    if row is None:
        return error
    addition = " ".join(text.split())
    new_text = f"{row['text']} {addition}" if row["text"].endswith((".", "!", "?")) else f"{row['text']}; {addition}"
    tags = _clean_tags([*row["tags"].split(), *HASHTAG.findall(addition)])
    ctx.db.execute(
        f"UPDATE {TABLE} SET text = ?, tags = ?, updated_at = ? WHERE id = ?", (new_text, " ".join(tags), utc_now_iso(), row["id"])
    )
    return ctx.t("appended", id=row["id"], text=new_text)


@tool
def delete_note(ctx: ToolContext, note: Annotated[str, Field(description="its #number or words from it")]) -> str:
    """Delete one note."""
    row, error = _find(ctx, note)
    if row is None:
        return error
    ctx.db.execute(f"DELETE FROM {TABLE} WHERE id = ?", (row["id"],))
    return ctx.t("deleted", text=row["text"])


# --- helpers ------------------------------------------------------------------------


def _rows(ctx: ToolContext) -> list:
    return ctx.db.query(f"SELECT * FROM {TABLE} WHERE user_id = ? ORDER BY id", (ctx.user_id,))


def _find(ctx: ToolContext, entry: str):
    """(row, None) or (None, reply). Never guesses between two notes."""
    rows = _rows(ctx)
    key = entry.strip().lstrip("#")
    if key.isdigit():
        row = next((r for r in rows if r["id"] == int(key)), None)
        return (row, None) if row else (None, ctx.t("not_found", entry=entry))
    hits = [r for r in rows if phrases_match(entry, f"{r['text']} {r['tags']}")]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, ctx.t("not_found", entry=entry)
    return None, ctx.t("ambiguous", entry=entry, ids=", ".join(f"#{r['id']}" for r in hits[:5]))


def _clean_tags(tags: list[str]) -> list[str]:
    """Lowercase, no '#', no duplicates, order kept: ['Kitap', '#kitap', 'fikir'] -> ['kitap', 'fikir']."""
    out: list[str] = []
    for tag in tags:
        clean = normalize(str(tag)).lstrip("#").replace(" ", "_")
        if clean and clean not in out:
            out.append(clean)
    return out


def _tags_suffix(tags: list[str]) -> str:
    return " · " + " ".join(f"#{t}" for t in tags) if tags else ""
