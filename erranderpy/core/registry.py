"""Find and load agents from the `agents/` folder: one folder = one agent.

    agents/<name>/
      agent.yaml     name, description, examples, system_prompt   (required)
      tools.py       functions marked with @tool                  (required)
      schema.sql     CREATE TABLE IF NOT EXISTS ...               (optional)
      messages.yaml  reply templates per language                 (optional)
      evals.yaml     eval cases for this agent's tools            (optional)

Folders starting with "_" or "." are skipped (e.g. agents/_template).
Adding an agent never requires editing this file or any other core file.
See docs/decisions/0009-agent-eklenti-sozlesmesi.md.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from erranderpy.core.db import Database
from erranderpy.core.llm import ToolSpec
from erranderpy.core.tools import Tool, get_tool

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
RESERVED_NAMES = {"unknown"}  # the router uses this for "no agent fits"
DEFAULT_LANGUAGE = "en"


class AgentLoadError(Exception):
    """An agent folder is broken; the message says which file and what to fix."""


@dataclass
class Agent:
    name: str
    description: str
    examples: list[str]
    system_prompt: str
    tools: dict[str, Tool]
    messages: dict[str, dict[str, str]] = field(default_factory=dict)  # language -> key -> template
    path: Path | None = None

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    def messages_for(self, language: str) -> dict[str, str]:
        """Templates in `language`, falling back to English key by key."""
        return {**self.messages.get(DEFAULT_LANGUAGE, {}), **self.messages.get(language, {})}


def load_agents(agents_dir: Path = AGENTS_DIR, db: Database | None = None) -> dict[str, Agent]:
    """Load every agent folder. With `db`, also create each agent's tables."""
    agents: dict[str, Agent] = {}
    if not agents_dir.is_dir():
        return agents
    for folder in sorted(agents_dir.iterdir()):
        if not folder.is_dir() or folder.name.startswith(("_", ".")):
            continue
        agent = load_agent(folder)
        agents[agent.name] = agent
        schema = folder / "schema.sql"
        if db is not None and schema.exists():
            db.apply_schema(schema.read_text(encoding="utf-8"))
    return agents


def load_agent(folder: Path) -> Agent:
    config = _read_yaml(folder / "agent.yaml", required=True)
    name = config.get("name")
    if name != folder.name:
        raise AgentLoadError(f"{folder}/agent.yaml: 'name' must be '{folder.name}' (the folder name), got {name!r}")
    if name in RESERVED_NAMES:
        raise AgentLoadError(f"{folder}: the name '{name}' is reserved")
    for key in ("description", "system_prompt"):
        if not str(config.get(key) or "").strip():
            raise AgentLoadError(f"{folder}/agent.yaml: '{key}' is required")

    tools = _load_tools(folder, name)
    messages = _read_yaml(folder / "messages.yaml", required=False)
    return Agent(
        name=name,
        description=str(config["description"]).strip(),
        examples=[str(e) for e in config.get("examples") or []],
        system_prompt=str(config["system_prompt"]).strip(),
        tools=tools,
        messages={lang: {k: str(v) for k, v in (table or {}).items()} for lang, table in messages.items()},
        path=folder,
    )


def _load_tools(folder: Path, agent_name: str) -> dict[str, Tool]:
    path = folder / "tools.py"
    if not path.exists():
        raise AgentLoadError(f"{folder}: tools.py is missing")
    module_name = f"erranderpy_agents.{agent_name}.tools"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AgentLoadError(f"{path}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # needed so Pydantic can resolve the module's types
    spec.loader.exec_module(module)

    tools: dict[str, Tool] = {}
    for value in vars(module).values():
        found = get_tool(value)
        if found is not None and getattr(value, "__module__", None) == module_name:
            tools[found.name] = found
    if not tools:
        raise AgentLoadError(f"{path}: no functions marked with @tool")
    return tools


def _read_yaml(path: Path, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise AgentLoadError(f"{path} is missing")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AgentLoadError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentLoadError(f"{path}: expected a mapping at the top level")
    return data
