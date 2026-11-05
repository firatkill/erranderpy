"""Tests for the notes agent (created by `make new-agent NAME=notes`). Free: no LLM calls."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from erranderpy.core.db import Database
from erranderpy.core.registry import load_agents
from erranderpy.core.tools import ToolContext
from erranderpy.gateway.telegram import split_message

DICTION = "Diksiyon çalışması:\nLar  \nLer\n\n\n\n1: Kırk kırık küp."


@pytest.fixture
def notes():
    db = Database(":memory:")
    agent = load_agents(db=db)["notes"]
    ctx = ToolContext(db=db, user_id=1, chat_id=1, now=datetime.now(ZoneInfo("Europe/Istanbul")), messages=agent.messages_for("tr"))
    return agent, ctx


@pytest.fixture
def call(notes):
    agent, ctx = notes
    return lambda tool, **args: agent.tools[tool].run(ctx, args).text


def test_add_keeps_lines_and_replies_with_title_only(call):
    reply = call("add_note", title="Diksiyon çalışması", text=DICTION, tags=["Diksiyon"])
    assert reply == "📝 Not aldım (#1): Diksiyon çalışması · #diksiyon"
    full = call("show_note", note="#1")
    # The heading became the title and is not repeated; line breaks survive, blank runs shrink.
    assert full == "#1 · Diksiyon çalışması · #diksiyon\n\nLar\nLer\n\n1: Kırk kırık küp."


def test_hashtags_and_missing_title(call):
    assert call("add_note", title="", text="geçmişe mektup yazan adam #roman", tags=["Kitap", "#kitap"]) == (
        "📝 Not aldım (#1): geçmişe mektup yazan adam #roman · #kitap #roman"
    )


def test_listing_shows_titles_not_text(call):
    call("add_note", title="Mercimek çorbası", text="önce soğan, sonra mercimek", tags=["tarif"])
    call("add_note", title="Mektup romanı", text="geçmişe mektup yazan adam", tags=["kitap"])
    assert "#2 · Mektup romanı · #kitap" in call("find_notes", tag="kitaplar")  # suffix-aware
    found = call("find_notes", query="mercimekli")  # searches the text too
    assert "#1 · Mercimek çorbası" in found and "önce soğan" not in found
    latest = call("find_notes")
    assert latest.index("#2") < latest.index("#1")  # newest first
    assert call("find_notes", tag="iş") == "#iş için not bulamadım."


def test_append_and_delete_never_guess(call):
    call("add_note", title="Bütçe", text="toplantıda bütçe")
    call("add_note", title="İzinler", text="toplantıda izinler")
    assert call("append_to_note", note="#1", text="ve maaşlar") == "✏️ #1 · Bütçe notuna ekledim: ve maaşlar"
    assert "toplantıda bütçe; ve maaşlar" in call("show_note", note="bütçe")
    assert "birden çok notla eşleşiyor" in call("delete_notes", notes=["toplantı"])
    assert call("delete_notes", notes=["izinler", "#9"]) == '🗑️ Sildim: İzinler\n"#9" diye bir not bulamadım.'


def test_delete_all_keeps_notes_from_this_turn(notes, call):
    agent, ctx = notes
    call("add_note", title="Eski", text="eski not")
    ctx.extras.clear()  # a new turn
    call("add_note", title="Diksiyon çalışması", text=DICTION)  # the model called add before delete
    assert call("delete_notes", all_notes=True) == "🗑️ Tüm notları sildim (1)."
    assert "#2 · Diksiyon çalışması" in call("find_notes")


def test_live_context_has_titles_only(notes, call):
    agent, ctx = notes
    assert "no notes yet" in agent.live_context(ctx)
    call("add_note", title="Diksiyon çalışması", text=DICTION)
    context = agent.live_context(ctx)
    assert "#1 · Diksiyon çalışması" in context and "Kırk" not in context


def test_migrate_adds_titles_to_old_notes():
    db = Database(":memory:")
    db.apply_schema(
        "CREATE TABLE notes_entries (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT NOT NULL,"
        " tags TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT)"
    )
    db.execute("INSERT INTO notes_entries (user_id, text, created_at) VALUES (1, 'gibi dizisinin 4. bölümünü izledim', 'x')")
    load_agents(db=db)
    load_agents(db=db)  # safe to run again
    db.execute("INSERT INTO notes_entries (user_id, text, created_at, title) VALUES (1, 'Diksiyon çalışması: Lar Ler', 'x', '')")
    load_agents(db=db)
    rows = db.query("SELECT title, text FROM notes_entries ORDER BY id")
    assert rows[0]["title"] == "gibi dizisinin 4. bölümünü izledim"
    assert (rows[1]["title"], rows[1]["text"]) == ("Diksiyon çalışması", "Lar Ler")


def test_listing_is_capped(call):
    for i in range(12):
        call("add_note", title=f"not {i}", text=f"not {i}")
    listing = call("find_notes")
    assert listing.startswith("📝 Notlar (12):") and "… ve 2 not daha" in listing


def test_long_replies_are_split_at_line_breaks():
    text = "\n".join(f"satır {i}" for i in range(1000))
    parts = split_message(text, limit=500)
    assert all(len(p) <= 500 for p in parts) and "\n".join(parts) == text
    assert split_message("kısa") == ["kısa"]
