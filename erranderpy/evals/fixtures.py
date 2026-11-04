"""Prototype agents for evals: agents that are designed but not built yet.

Real agents live in `agents/<name>/` and the evals load them from there. While
an agent is only planned, a prototype here (description, examples, prompt and
tool schemas, no code) lets us measure the router and the tool decisions it
will need *before* writing it. That is how `tasks` was designed in Stage 0–1;
it became a real agent in Stage 2, so the list is empty for now.

Important: the few-shot `examples` of a prototype must never be copied from the
eval datasets. If they were, a model could score well by recognizing the
sentence instead of understanding it (overfitting).
"""

from __future__ import annotations

from dataclasses import dataclass

from erranderpy.core.llm import ToolSpec


@dataclass(frozen=True)
class PrototypeAgent:
    name: str
    description: str
    examples: list[str]
    system_prompt: str
    tool_specs: list[ToolSpec]


PROTOTYPES: dict[str, PrototypeAgent] = {}
