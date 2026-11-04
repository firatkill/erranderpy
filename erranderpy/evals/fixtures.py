"""Prototype router and agents used by the Stage 0 evals.

These are the *first drafts* of the real router and agents. Stage 0 uses them
to compare models on exactly the decisions the bot will make. In Stage 1 they
move into `agents/<name>/agent.yaml` + `tools.py`.

Important: the few-shot `examples` below must never be copied from the eval
datasets in `evals/`. If they were, a model could score well by recognizing the
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
    tools: list[ToolSpec]


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_NAMES = {"type": "array", "items": {"type": "string"}, "minItems": 1}

SHOPPING = PrototypeAgent(
    name="shopping",
    description="Manages the shopping list: adds, removes and checks off items, shows the list.",
    examples=["süt almam lazım", "listede ne var", "ekmeği aldım", "yumurtayı listeden sil"],
    system_prompt=(
        "You manage the user's shopping list. Users write in Turkish.\n"
        "For every message, call exactly one tool.\n"
        "Write item names in lowercase dictionary form, without Turkish case or plural suffixes: "
        "'peyniri' -> 'peynir', 'portakalları' -> 'portakal'.\n"
        "Set quantity only when the user gives a number of units; otherwise leave it out.\n"
        "If the user says something ran out or is finished, add it to the list."
    ),
    tools=[
        ToolSpec(
            "add_items",
            "Add one or more items to the shopping list.",
            _obj(
                {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": _obj(
                            {"name": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
                            ["name"],
                        ),
                    }
                },
                ["items"],
            ),
        ),
        ToolSpec("remove_items", "Remove items from the list without buying them.", _obj({"names": _NAMES}, ["names"])),
        ToolSpec("mark_bought", "Mark items on the list as bought.", _obj({"names": _NAMES}, ["names"])),
        ToolSpec("list_items", "Show the items still to buy.", _obj({})),
    ],
)

TASKS = PrototypeAgent(
    name="tasks",
    description=(
        "Manages daily tasks and habits: new one-off to-dos, recurring daily habits with a time target, "
        "logging time spent on a habit, completing tasks, showing what is left."
    ),
    examples=["bundan sonra her gün 20 dk meditasyon", "bugün 15 dk meditasyon yaptım", "cuma günü dişçiyi ara", "yarın neler var"],
    system_prompt=(
        "You manage the user's daily tasks and habits. Users write in Turkish.\n"
        "For every message, call exactly one tool.\n"
        "- A habit repeats every day with a target in minutes ('from now on, one hour of X a day') -> create_habit.\n"
        "- Progress is time already spent today on a habit ('I did X for 30 minutes today') -> log_progress.\n"
        "- A task is a one-off to-do -> add_task. Use when='today' or 'tomorrow'; for other days use YYYY-MM-DD.\n"
        "Always convert hours to minutes. Name habits with a short lowercase Turkish noun phrase "
        "('ingilizce', 'meditasyon', 'kitap okuma')."
    ),
    tools=[
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
        ToolSpec(
            "complete_task",
            "Mark a one-off task as done.",
            _obj({"title": {"type": "string"}}, ["title"]),
        ),
        ToolSpec(
            "list_tasks",
            "Show tasks and habit progress for a day.",
            _obj({"day": {"type": "string", "enum": ["today", "tomorrow"]}}, ["day"]),
        ),
    ],
)

AGENTS: dict[str, PrototypeAgent] = {a.name: a for a in (SHOPPING, TASKS)}

ROUTER_AGENTS = [*AGENTS, "unknown"]

ROUTE_TOOL = ToolSpec(
    "route",
    "Send each request in the user's message to one agent.",
    _obj(
        {
            "routes": {
                "type": "array",
                "minItems": 1,
                "items": _obj(
                    {
                        "agent": {"type": "string", "enum": ROUTER_AGENTS},
                        "text": {"type": "string", "description": "This request, in the user's own words."},
                    },
                    ["agent", "text"],
                ),
            }
        },
        ["routes"],
    ),
)


def router_system_prompt() -> str:
    lines = [
        "You are the router of a personal assistant. Users write short messages, usually in Turkish.",
        "A message may contain several requests. Split it and send each request to exactly one agent.",
        "",
        "Agents:",
    ]
    for agent in AGENTS.values():
        examples = "; ".join(f'"{e}"' for e in agent.examples)
        lines.append(f"- {agent.name}: {agent.description} Examples: {examples}")
    lines += [
        "- unknown: anything else: greetings, chit-chat, questions the agents can't handle, unclear messages.",
        "",
        "Always answer by calling the `route` tool exactly once. Never reply with plain text.",
    ]
    return "\n".join(lines)
