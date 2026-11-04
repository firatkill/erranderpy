from __future__ import annotations

import textwrap
from pathlib import Path

import pydantic
import pytest
from pydantic import BaseModel

from erranderpy.core.registry import AgentLoadError, load_agents
from erranderpy.core.tools import ToolContext, ToolResult, build_tool


class Thing(BaseModel):
    name: str
    count: int | None = None


def sample(ctx: ToolContext, things: list[Thing], note: str = "") -> str:
    """Do something with things."""
    return f"{len(things)} {note}".strip()


def test_schema_is_generated_from_type_hints():
    t = build_tool(sample)
    assert t.name == "sample"
    assert t.description == "Do something with things."
    schema = t.spec.parameters
    assert "$defs" not in schema and "title" not in schema
    item = schema["properties"]["things"]["items"]
    assert item["properties"]["count"] == {"type": "integer"}  # `int | None = None` became a plain optional int
    assert item["required"] == ["name"]
    assert schema["required"] == ["things"]


def test_run_validates_arguments():
    t = build_tool(sample)
    assert t.run(None, {"things": [{"name": "a"}], "note": "x"}) == ToolResult("1 x")
    with pytest.raises(pydantic.ValidationError):
        t.run(None, {"things": "not a list"})


def test_tool_needs_ctx_first_and_a_docstring():
    def no_ctx(items: list[str]) -> str:
        """Doc."""

    def no_doc(ctx: ToolContext, items: list[str]) -> str:
        pass

    with pytest.raises(TypeError, match="ctx"):
        build_tool(no_ctx)
    with pytest.raises(TypeError, match="docstring"):
        build_tool(no_doc)


def _write_agent(root: Path, folder: str, name: str | None = None, tools: str | None = None) -> None:
    d = root / folder
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        f"name: {name or folder}\ndescription: Says hello.\nexamples: [merhaba de]\nsystem_prompt: Be kind.\n",
        encoding="utf-8",
    )
    if tools is None:
        tools = textwrap.dedent(
            '''
            from erranderpy.core.tools import ToolContext, tool

            @tool
            def say_hello(ctx: ToolContext, who: str) -> str:
                """Say hello to someone."""
                return f"hello {who}"
            '''
        )
    (d / "tools.py").write_text(tools, encoding="utf-8")


def test_registry_loads_folders_and_skips_templates(tmp_path):
    _write_agent(tmp_path, "greeter")
    _write_agent(tmp_path, "_template")
    agents = load_agents(tmp_path)
    assert list(agents) == ["greeter"]
    agent = agents["greeter"]
    assert agent.examples == ["merhaba de"]
    assert agent.tools["say_hello"].run(None, {"who": "Fırat"}).text == "hello Fırat"


def test_registry_rejects_name_mismatch(tmp_path):
    _write_agent(tmp_path, "greeter", name="other")
    with pytest.raises(AgentLoadError, match="folder name"):
        load_agents(tmp_path)


def test_registry_requires_tools(tmp_path):
    _write_agent(tmp_path, "greeter", tools="x = 1\n")
    with pytest.raises(AgentLoadError, match="@tool"):
        load_agents(tmp_path)


def test_real_agents_load():
    agents = load_agents()
    assert "shopping" in agents
    assert set(agents["shopping"].tools) == {"add_items", "remove_items", "mark_bought", "list_items", "clear_list", "undo_clear"}
    assert agents["shopping"].messages_for("tr")["added"].startswith("🛒")
