"""Tests for the notes agent (created by `make new-agent NAME=notes`). Free: no LLM calls."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from erranderpy.core.db import Database
from erranderpy.core.registry import load_agents
from erranderpy.core.tools import ToolContext


@pytest.fixture
def call():
    db = Database(":memory:")
    agent = load_agents(db=db)["notes"]
    ctx = ToolContext(db=db, user_id=1, chat_id=1, now=datetime.now(ZoneInfo("Europe/Istanbul")), messages=agent.messages_for("tr"))
    return lambda tool, **args: agent.tools[tool].run(ctx, args).text


def test_add_with_tags_and_hashtags(call):
    reply = call("add_note", text="geçmişe mektup yazan adam #roman", tags=["Kitap", "#kitap", "fikir"])
    assert reply == "📝 Not aldım (#1): geçmişe mektup yazan adam #roman · #kitap #fikir #roman"


def test_find_by_tag_by_words_and_latest(call):
    call("add_note", text="mercimek çorbası: önce soğan", tags=["tarif"])
    call("add_note", text="geçmişe mektup yazan adam", tags=["kitap"])
    assert "#2 · geçmişe mektup" in call("find_notes", tag="kitaplar")  # suffix-aware
    assert "#1 · mercimek" in call("find_notes", query="mercimekli")
    latest = call("find_notes")
    assert latest.index("#2") < latest.index("#1")  # newest first
    assert call("find_notes", tag="iş") == "#iş için not bulamadım."


def test_append_and_delete_never_guess(call):
    call("add_note", text="toplantıda bütçe")
    call("add_note", text="toplantıda izinler")
    assert call("append_to_note", note="#1", text="ve maaşlar") == "✏️ #1 notuna ekledim: toplantıda bütçe; ve maaşlar"
    assert "birden çok notla eşleşiyor" in call("delete_note", note="toplantı")
    assert call("delete_note", note="izinler") == "🗑️ Sildim: toplantıda izinler"
    assert call("delete_note", note="#9") == '"#9" diye bir not bulamadım.'


def test_listing_is_capped(call):
    for i in range(12):
        call("add_note", text=f"not {i}")
    listing = call("find_notes")
    assert listing.startswith("📝 Notlar (12):") and "… ve 2 not daha" in listing
