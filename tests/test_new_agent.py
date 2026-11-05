"""`make new-agent`: the template must produce an agent that loads and works untouched."""

import pytest

from erranderpy.core.db import Database
from erranderpy.core.registry import load_agents
from erranderpy.core.tools import ToolContext
from erranderpy.new_agent import create


def test_template_makes_a_working_agent(tmp_path):
    agents, tests = tmp_path / "agents", tmp_path / "tests"
    agents.mkdir()
    tests.mkdir()
    written = create("recipes_x", agents, tests)
    assert (tests / "test_agent_recipes_x.py") in written
    assert "{{" not in "".join(p.read_text(encoding="utf-8") for p in written)

    db = Database(":memory:")
    agent = load_agents(agents, db)["recipes_x"]
    assert set(agent.tools) == {"add_entry", "list_entries", "delete_entry"}
    from datetime import datetime

    ctx = ToolContext(db, 1, 1, datetime.now(), agent.messages_for("tr"))
    assert agent.tools["add_entry"].run(ctx, {"text": "menemen"}).text == "✅ Kaydettim (#1): menemen"


@pytest.mark.parametrize("name", ["Bad", "x", "unknown", "_hidden", "1abc", "has-dash"])
def test_bad_names_are_refused(tmp_path, name):
    with pytest.raises(ValueError):
        create(name, tmp_path, tmp_path)


def test_existing_agent_is_not_overwritten(tmp_path):
    create("notes_x", tmp_path, tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        create("notes_x", tmp_path, tmp_path)
