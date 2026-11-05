"""Notes and ideas. (Created from agents/_template with `make new-agent NAME=notes`.)

The model decides which tool, the note's title, its text and a few tags. Code
does the rest: storing, pulling #hashtags out of the text, Turkish-aware search
("mercimekli" finds "mercimek"), and writing every reply.

Every note has a short title. Notes can be long (a pasted diction exercise), so
listings, the prompt's live context and the replies show titles only; the full
text goes out only when the user asks for one note (show_note). That keeps both
the chat and the tokens small (live, 2026-11-05).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field

from erranderpy.core.db import Database, utc_now_iso
from erranderpy.core.text import normalize, phrases_match
from erranderpy.core.tools import ToolContext, tool

TABLE = "notes_entries"  # a constant, never user input: safe in the SQL strings below
SHOW = 10  # notes per listing
CONTEXT_NOTES = 30  # notes (titles only) shown to the model on every turn
TITLE_MAX = 60
HASHTAG = re.compile(r"#(\w+)")
ADDED_KEY = "notes_added"  # ids saved during this turn, in ctx.extras


@tool
def add_note(
    ctx: ToolContext,
    title: Annotated[
        str, Field(description="a short title, 2-5 words, in the user's language; their own heading if they gave one")
    ],
    text: Annotated[str, Field(description="the note itself, in the user's words, without 'not al:'; keep line breaks")],
    tags: Annotated[list[str], Field(max_length=3, description="0-3 short lowercase topic words, no '#'")] = [],
) -> str:
    """Save a new note or idea."""
    text = _clean_text(text)
    title = _clean_title(title, text)
    text = _drop_heading(text, title)
    all_tags = _clean_tags([*tags, *HASHTAG.findall(text)])
    cur = ctx.db.execute(
        f"INSERT INTO {TABLE} (user_id, title, text, tags, created_at) VALUES (?, ?, ?, ?, ?)",
        (ctx.user_id, title, text, " ".join(all_tags), utc_now_iso()),
    )
    ctx.extras.setdefault(ADDED_KEY, set()).add(cur.lastrowid)
    return ctx.t("added", id=cur.lastrowid, title=title, tags=_tags_suffix(all_tags))


@tool
def find_notes(
    ctx: ToolContext,
    tag: Annotated[str | None, Field(description="a topic, e.g. 'kitap'")] = None,
    query: Annotated[str | None, Field(description="words in the note's title or text")] = None,
) -> str:
    """List notes by title: by tag, by words in them, or the latest ones."""
    rows = _rows(ctx)
    about = " · ".join(p for p in (f"#{_clean_tags([tag])[0]}" if tag and _clean_tags([tag]) else "", query or "") if p)
    if tag:
        wanted = normalize(tag).lstrip("#")
        rows = [r for r in rows if any(phrases_match(wanted, t) for t in r["tags"].split())]
    if query:
        rows = [r for r in rows if phrases_match(query, _haystack(r))]
    if not rows:
        return ctx.t("none_found", about=about) if about else ctx.t("empty")
    newest = rows[::-1]
    lines = [ctx.t("header", about=f" · {about}" if about else "", count=len(rows))]
    lines += [_line(r) for r in newest[:SHOW]]
    if len(rows) > SHOW:
        lines.append(ctx.t("header_more", more=len(rows) - SHOW))
    lines.append(ctx.t("show_hint"))
    return "\n".join(lines)


@tool
def show_note(ctx: ToolContext, note: Annotated[str, Field(description="which note: its #number or words from it")]) -> str:
    """Show one note in full: its whole text."""
    row, error = _find(ctx, note)
    if row is None:
        return error
    return f"{_line(row)}\n\n{row['text']}"


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
    addition = _clean_text(text)
    old = row["text"]
    if "\n" in old or "\n" in addition:
        new_text = f"{old}\n{addition}"
    elif old.endswith((".", "!", "?")):
        new_text = f"{old} {addition}"
    else:
        new_text = f"{old}; {addition}"
    tags = _clean_tags([*row["tags"].split(), *HASHTAG.findall(addition)])
    ctx.db.execute(
        f"UPDATE {TABLE} SET text = ?, tags = ?, updated_at = ? WHERE id = ?", (new_text, " ".join(tags), utc_now_iso(), row["id"])
    )
    return ctx.t("appended", id=row["id"], title=row["title"], text=addition)


@tool
def delete_notes(
    ctx: ToolContext,
    notes: Annotated[list[str], Field(description="the notes to delete: #numbers or words from them")] = [],
    all_notes: Annotated[bool, Field(description="true only when the user asks to delete every note")] = False,
) -> str:
    """Delete one or more notes, or all of them."""
    if all_notes:
        # Notes saved earlier in this same turn survive: "notları temizle, sonra şunu
        # ekle" must not lose the new note if the model calls add before delete.
        keep = ctx.extras.get(ADDED_KEY, set())
        rows = [r for r in _rows(ctx) if r["id"] not in keep]
        if not rows:
            return ctx.t("empty")
        ctx.db.execute(
            f"DELETE FROM {TABLE} WHERE id IN ({', '.join('?' * len(rows))})", tuple(r["id"] for r in rows)
        )
        return ctx.t("deleted_all", count=len(rows))
    if not notes:
        return ctx.t("which")
    deleted, problems = [], []
    for note in notes:
        row, error = _find(ctx, note)
        if row is None:
            problems.append(error)
            continue
        ctx.db.execute(f"DELETE FROM {TABLE} WHERE id = ?", (row["id"],))
        deleted.append(row["title"])
    lines = [ctx.t("deleted", titles=", ".join(deleted))] if deleted else []
    return "\n".join([*lines, *problems])


# --- hooks the registry picks up (ADR-0022 in AGENTS.md) -----------------------------


def live_context(ctx: ToolContext) -> str:
    """Titles of the existing notes, with #numbers, for the prompt on every turn.

    Titles only, never the text: cheap even with long notes. Live (2026-11-05):
    without this the model listed the notes first ("notları temizle, sonra şunu
    ekle"), the listing ended the turn and the new note was never saved.
    """
    rows = _rows(ctx)[::-1]
    if not rows:
        return "\n\nThe user has no notes yet."
    lines = [_line(r) for r in rows[:CONTEXT_NOTES]]
    if len(rows) > CONTEXT_NOTES:
        lines.append(f"... and {len(rows) - CONTEXT_NOTES} older notes (find them with find_notes and a query)")
    return (
        "\n\nThe user's notes, newest first (titles only; pass a #number to show, extend or delete one):\n"
        + "\n".join(lines)
    )


def migrate(db: Database) -> None:
    """Notes saved before titles existed get a `title` column and a title from their first line."""
    columns = {r["name"] for r in db.query(f"PRAGMA table_info({TABLE})")}
    if "title" not in columns:
        db.execute(f"ALTER TABLE {TABLE} ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    for row in db.query(f"SELECT id, text FROM {TABLE} WHERE title = ''"):
        title = _clean_title("", row["text"])
        text = row["text"]
        if text.startswith(f"{title}:") and text[len(title) + 1 :].strip():
            text = text[len(title) + 1 :].strip()  # the heading is now the title
        db.execute(f"UPDATE {TABLE} SET title = ?, text = ? WHERE id = ?", (title, text, row["id"]))


# --- helpers ------------------------------------------------------------------------


def _rows(ctx: ToolContext) -> list:
    return ctx.db.query(f"SELECT * FROM {TABLE} WHERE user_id = ? ORDER BY id", (ctx.user_id,))


def _line(row) -> str:
    return f"#{row['id']} · {row['title']}{_tags_suffix(row['tags'].split())}"


def _haystack(row) -> str:
    return f"{row['title']} {row['text']} {row['tags']}"


def _find(ctx: ToolContext, entry: str):
    """(row, None) or (None, reply). Never guesses between two notes.

    A title match wins over a text match: "diksiyon notunu sil" means the note
    titled "Diksiyon çalışması", not every note that mentions diction."""
    rows = _rows(ctx)
    key = entry.strip().lstrip("#")
    if key.isdigit():
        row = next((r for r in rows if r["id"] == int(key)), None)
        return (row, None) if row else (None, ctx.t("not_found", entry=entry))
    hits = [r for r in rows if phrases_match(entry, r["title"])] or [r for r in rows if phrases_match(entry, _haystack(r))]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, ctx.t("not_found", entry=entry)
    return None, ctx.t("ambiguous", entry=entry, ids=", ".join(f"#{r['id']}" for r in hits[:5]))


def _clean_text(text: str) -> str:
    """Keep the user's line breaks (a poem, a list), drop trailing spaces and runs of blank lines."""
    lines = [" ".join(line.split()) for line in text.strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean_title(title: str, text: str) -> str:
    """One line, no trailing ':'. Without a title from the model, the text's first line."""
    title = " ".join(title.split()).rstrip(":").strip()
    if not title:
        first = (text.splitlines() or [""])[0]
        # "Diksiyon çalışması: Lar Ler ..." (a note saved before line breaks were kept):
        # a short heading before the first ':' is the title.
        head, colon, _ = first.partition(":")
        first = head if colon and 0 < len(head.split()) <= 5 else first
        title = first.rstrip(":").strip() or "Not"
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def _drop_heading(text: str, title: str) -> str:
    """"Diksiyon çalışması:\\nLar..." titled "Diksiyon çalışması": the first line is the title, not the text."""
    first, _, rest = text.partition("\n")
    if rest.strip() and normalize(first.rstrip(":")) == normalize(title):
        return rest.strip()
    return text


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
