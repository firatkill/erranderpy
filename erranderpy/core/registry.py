"""Find and load agents: one folder = one agent.

Two kinds of agent folders:

    agents/<name>/                      kind: code (written by people)
      agent.yaml     name (= folder name), description, examples, system_prompt
      tools.py       functions marked with @tool
      jobs.py        functions marked with @daily              (optional)
      schema.sql     CREATE TABLE IF NOT EXISTS ...            (optional)
      messages.yaml  reply templates per language              (optional)
      evals.yaml     eval cases for this agent's tools         (optional)

    data/agents/<name>/                 kind: records (usually written by the bot itself)
      agent.yaml     a RecordsSpec: fields, reminders...; tools are generated (core/records.py)

Agents are loaded from several folders: the built-in `agents/` (in the repo) and
the user's `data/agents/` (git-ignored, private). Folders starting with "_" or
"." are skipped (templates, deleted agents). Adding an agent never requires
editing core code. See ADR-0009 and ADR-0012.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pydantic
import yaml

from erranderpy.core.agent import DEFAULT_LANGUAGE, Agent
from erranderpy.core.db import Database
from erranderpy.core.jobs import Job, get_job
from erranderpy.core.records import RecordsSpec, build_records_agent
from erranderpy.core.tools import Tool, get_tool

__all__ = ["AGENTS_DIR", "Agent", "AgentLoadError", "DEFAULT_LANGUAGE", "load_agent", "load_agents"]

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
RESERVED_NAMES = {"unknown"}  # the router uses this for "no agent fits"


class AgentLoadError(Exception):
    """An agent folder is broken; the message says which file and what to fix."""


def load_agents(dirs: Path | Iterable[Path] = AGENTS_DIR, db: Database | None = None) -> dict[str, Agent]:
    """Load every agent folder in `dirs`. With `db`, also create code agents' tables.

    Agents marked `routing: last` (the builder) come last, so the router lists
    them after every concrete agent.
    """
    folders = [dirs] if isinstance(dirs, Path) else list(dirs)
    agents: dict[str, Agent] = {}
    for base in folders:
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir() or folder.name.startswith(("_", ".")):
                continue
            agent = load_agent(folder)
            if agent.name in agents:
                raise AgentLoadError(f"{folder}: an agent named '{agent.name}' already exists in {agents[agent.name].path}")
            agents[agent.name] = agent
            schema = folder / "schema.sql"
            if db is not None and schema.exists():
                db.apply_schema(schema.read_text(encoding="utf-8"))
    return dict(sorted(agents.items(), key=lambda item: item[1].route_last))


def load_agent(folder: Path) -> Agent:
    config = _read_yaml(folder / "agent.yaml", required=True)
    name = config.get("name")
    if name != folder.name:
        raise AgentLoadError(f"{folder}/agent.yaml: 'name' must be '{folder.name}' (the folder name), got {name!r}")
    if name in RESERVED_NAMES:
        raise AgentLoadError(f"{folder}: the name '{name}' is reserved")

    if config.get("kind") == "records":
        try:
            spec = RecordsSpec.model_validate(config)
        except pydantic.ValidationError as exc:
            raise AgentLoadError(f"{folder}/agent.yaml: {exc}") from exc
        return build_records_agent(spec, folder)

    for key in ("description", "system_prompt"):
        if not str(config.get(key) or "").strip():
            raise AgentLoadError(f"{folder}/agent.yaml: '{key}' is required")
    tools = _load_tools(folder, name)
    jobs = _load_jobs(folder, name)
    messages = _read_yaml(folder / "messages.yaml", required=False)
    return Agent(
        name=name,
        description=str(config["description"]).strip(),
        examples=[str(e) for e in config.get("examples") or []],
        system_prompt=str(config["system_prompt"]).strip(),
        tools=tools,
        messages={lang: {k: str(v) for k, v in (table or {}).items()} for lang, table in messages.items()},
        jobs=jobs,
        path=folder,
        title=str(config.get("title") or name),
        route_last=config.get("routing") == "last",
        context=[str(c) for c in config.get("context") or []],
    )


def _import(folder: Path, agent_name: str, filename: str):
    path = folder / filename
    module_name = f"erranderpy_agents.{agent_name}.{path.stem}"
    # Register empty parent packages so an agent's jobs.py can write
    # `from erranderpy_agents.<name>.tools import ...`.
    for parent in ("erranderpy_agents", f"erranderpy_agents.{agent_name}"):
        if parent not in sys.modules:
            package = types.ModuleType(parent)
            package.__path__ = []
            sys.modules[parent] = package
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AgentLoadError(f"{path}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # needed so Pydantic can resolve the module's types
    spec.loader.exec_module(module)
    return module, module_name


def _load_tools(folder: Path, agent_name: str) -> dict[str, Tool]:
    path = folder / "tools.py"
    if not path.exists():
        raise AgentLoadError(f"{folder}: tools.py is missing")
    module, module_name = _import(folder, agent_name, "tools.py")
    tools: dict[str, Tool] = {}
    for value in vars(module).values():
        found = get_tool(value)
        if found is not None and getattr(value, "__module__", None) == module_name:
            tools[found.name] = found
    if not tools:
        raise AgentLoadError(f"{path}: no functions marked with @tool")
    return tools


def _load_jobs(folder: Path, agent_name: str) -> list[Job]:
    if not (folder / "jobs.py").exists():
        return []
    module, module_name = _import(folder, agent_name, "jobs.py")
    jobs = []
    for value in vars(module).values():
        found = get_job(value)
        if found is not None and getattr(value, "__module__", None) == module_name:
            jobs.append(Job(found.name, found.fn, found.at, found.env, agent=agent_name))
    return jobs


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
