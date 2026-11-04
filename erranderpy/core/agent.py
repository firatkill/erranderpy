"""What an agent is, once loaded: the same shape whether it was written as code
(agents/<name>/tools.py) or declared in YAML and built by the platform (kind: records)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from erranderpy.core.jobs import Job
from erranderpy.core.llm import ToolSpec
from erranderpy.core.tools import Tool

DEFAULT_LANGUAGE = "en"


@dataclass
class Agent:
    name: str
    description: str
    examples: list[str]
    system_prompt: str
    tools: dict[str, Tool]
    messages: dict[str, dict[str, str]] = field(default_factory=dict)  # language -> key -> template
    jobs: list[Job] = field(default_factory=list)
    path: Path | None = None
    kind: Literal["code", "records"] = "code"
    title: str = ""  # human name for lists, e.g. "📅 Takvim"
    created_by: str | None = None  # "builder" for agents the bot made itself
    route_last: bool = False  # listed last in the router prompt (the builder: "if nothing else fits")
    reminder_hint: str = ""  # one line for the capability list, e.g. "reminds 60 min before 'when'"
    context: list[str] = field(default_factory=list)  # extra prompt context, e.g. ["agents"]: list the other agents

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.tools.values()]

    def messages_for(self, language: str) -> dict[str, str]:
        """Templates in `language`, falling back to English key by key."""
        return {**self.messages.get(DEFAULT_LANGUAGE, {}), **self.messages.get(language, {})}
