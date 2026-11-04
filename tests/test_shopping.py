from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from erranderpy.core.db import Database
from erranderpy.core.registry import load_agents
from erranderpy.core.tools import ToolContext

USER = 42


@pytest.fixture
def shop():
    db = Database(":memory:")
    agent = load_agents(db=db)["shopping"]
    ctx = ToolContext(db=db, user_id=USER, chat_id=USER, now=datetime.now(ZoneInfo("Europe/Istanbul")), messages=agent.messages_for("tr"))

    def call(tool: str, **args) -> str:
        return agent.tools[tool].run(ctx, args).text

    return call


def test_add_and_list(shop):
    assert shop("add_items", items=[{"name": "zeytinyağı"}]) == "🛒 Ekledim: zeytinyağı. Listede 1 ürün var."
    shop("add_items", items=[{"name": "makarna", "quantity": 2}])
    listing = shop("list_items")
    assert "• zeytinyağı" in listing
    assert "• 2 × makarna" in listing


def test_duplicates_are_not_added_twice(shop):
    shop("add_items", items=[{"name": "Süt"}])
    reply = shop("add_items", items=[{"name": "süt"}])
    assert "Zaten listede: Süt" in reply
    assert "1 ürün" in reply
    reply = shop("add_items", items=[{"name": "süt", "quantity": 3}])
    assert "Güncelledim: 3 × Süt" in reply


def test_mark_bought_uses_unambiguous_partial_match(shop):
    shop("add_items", items=[{"name": "zeytinyağı"}, {"name": "ekmek"}])
    reply = shop("mark_bought", names=["zeytin"])
    assert reply.startswith("✅ Alındı: zeytinyağı.")
    listing = shop("list_items")
    assert "Bugün aldıkların: zeytinyağı" in listing
    assert "• ekmek" in listing


def test_ambiguous_partial_match_is_not_guessed(shop):
    shop("add_items", items=[{"name": "zeytinyağı"}, {"name": "ayçiçek yağı"}])
    reply = shop("mark_bought", names=["yağ"])
    assert "Listede bulamadım: yağ" in reply
    assert "2 ürün" in reply


def test_remove_and_empty_list(shop):
    shop("add_items", items=[{"name": "domates"}])
    assert "Listeden çıkardım: domates" in shop("remove_items", names=["domates"])
    assert shop("list_items") == "Listen boş 🎉"


def test_items_are_per_user(shop):
    shop("add_items", items=[{"name": "elma"}])
    db = Database(":memory:")  # a fresh db sees nothing, and another user id sees nothing either
    agent = load_agents(db=db)["shopping"]
    other = ToolContext(db=db, user_id=7, chat_id=7, now=datetime.now(ZoneInfo("UTC")), messages=agent.messages_for("en"))
    assert agent.tools["list_items"].run(other, {}).text == "Your list is empty 🎉"


def test_clear_list_and_undo(shop):
    shop("add_items", items=[{"name": "ekmek"}, {"name": "cips"}, {"name": "kola"}])
    shop("mark_bought", names=["kola"])
    assert shop("clear_list") == '🧹 Listeyi temizledim (2 ürün: ekmek, cips). Yanlışlıkla olduysa "geri al" yaz.'
    assert shop("list_items").startswith("Listen boş")
    assert shop("undo_clear") == "↩️ Geri aldım. Listede 2 ürün var."
    assert "Bugün aldıkların: kola" in shop("list_items")  # bought items are not touched
    assert shop("undo_clear") == "Geri alınacak bir temizleme yok."
    shop("clear_list")
    shop("clear_list")  # second clear on an empty list doesn't lose the undo of the first
    assert shop("undo_clear").startswith("↩️")
