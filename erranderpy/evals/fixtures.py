"""Prototype agents for evals: agents that are designed but not built yet.

Real agents live in `agents/<name>/` and the evals load them from there. Until
an agent exists, its prototype here lets us keep measuring the router and the
tool decisions it will need. `tasks` moves to `agents/tasks/` in Stage 2.

Important: the few-shot `examples` below must never be copied from the eval
datasets. If they were, a model could score well by recognizing the sentence
instead of understanding it (overfitting).
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


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


TASKS = PrototypeAgent(
    name="tasks",
    description=(
        "Manages daily tasks and habits: new one-off to-dos, recurring daily habits with a time target, "
        "logging time spent on a habit, completing tasks, showing what is left."
    ),
    examples=["bundan sonra her gün 20 dk meditasyon", "bugün 15 dk meditasyon yaptım", "cuma günü dişçiyi ara", "yarın neler var"],
    system_prompt=(
        "You manage the user's daily tasks and habits.\n"
        "For every request, call exactly one tool.\n"
        "- A habit repeats every day with a target in minutes ('from now on, one hour of X a day') -> create_habit.\n"
        "- Progress is time already spent today on a habit ('I did X for 30 minutes today') -> log_progress.\n"
        "- A task is a one-off to-do -> add_task. Use when='today' or 'tomorrow'; for other days use YYYY-MM-DD.\n"
        "Always convert hours to minutes. Name habits with a short lowercase Turkish noun phrase "
        "('ingilizce', 'meditasyon', 'kitap okuma')."
    ),
    tool_specs=[
        ToolSpec(
            "create_habit",
            "Start a new daily habit with a target in minutes per day.",
            _obj({"name": {"type": "string"}, "daily_minutes": {"type": "integer", "minimum": 1}}, ["name", "daily_minutes"]),
        ),
        ToolSpec(
            "log_progress",
            "Record minutes spent today on an existing habit.",
            _obj({"habit": {"type": "string"}, "minutes": {"type": "integer", "minimum": 1}}, ["habit", "minutes"]),
        ),
        ToolSpec(
            "add_task",
            "Add a one-off task.",
            _obj({"title": {"type": "string"}, "when": {"type": "string"}}, ["title", "when"]),
        ),
        ToolSpec("complete_task", "Mark a one-off task as done.", _obj({"title": {"type": "string"}}, ["title"])),
        ToolSpec(
            "list_tasks",
            "Show tasks and habit progress for a day.",
            _obj({"day": {"type": "string", "enum": ["today", "tomorrow"]}}, ["day"]),
        ),
    ],
)

PROTOTYPES: dict[str, PrototypeAgent] = {TASKS.name: TASKS}
