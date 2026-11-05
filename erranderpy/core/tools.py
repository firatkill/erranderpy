"""Turn plain Python functions into tools an LLM can call.

An agent's `tools.py` marks functions with `@tool`:

    @tool
    def add_items(ctx: ToolContext, items: list[Item]) -> str:
        '''Add one or more items to the shopping list.'''

From the type hints and the docstring we build the JSON schema the model sees
(via Pydantic), and when the model calls the tool we validate its arguments
against the same types before running the function. The schema and the
validation can never drift apart, because they come from one source.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

import pydantic

from erranderpy.core.db import Database
from erranderpy.core.llm import ToolSpec
from erranderpy.core.tracing import Tracer


@dataclass
class ToolContext:
    """Everything a tool may need, passed in instead of imported (easy to test)."""

    db: Database
    user_id: int
    chat_id: int
    now: datetime  # timezone-aware, in the user's TIMEZONE
    messages: dict[str, str] = field(default_factory=dict)  # this agent's reply templates, one language
    tracer: Tracer | None = None
    request_text: str = ""  # the request routed to this agent
    # The recent chat as plain text (user and bot). Lets a tool check that a value
    # really came from the conversation, e.g. a clock time the model may invent.
    conversation: str = ""
    # Tools ask the pipeline for things beyond a reply by setting effects,
    # e.g. {"reload_agents": True, "replay": "11 kasım 14:00 avm"} (see Assistant).
    effects: dict[str, Any] = field(default_factory=dict)
    # Platform information a few tools need (the agent builder): agents, folders.
    extras: dict[str, Any] = field(default_factory=dict)

    def t(self, key: str, **values: Any) -> str:
        """Fill in one of the agent's reply templates (agents/<name>/messages.yaml)."""
        template = self.messages.get(key)
        if template is None:
            return f"[{key}]"  # a missing template is visible, not a crash
        return template.format(**values)

    def today_start_utc(self) -> str:
        """Midnight of the user's 'today', as a UTC ISO string comparable with stored timestamps."""
        midnight = datetime.combine(self.now.date(), time.min, tzinfo=self.now.tzinfo)
        return midnight.astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class ToolResult:
    """`text` goes to the model as the tool result.

    If `final` is True, `text` is also the reply to the user and the agent loop
    stops without asking the model to rephrase it: one LLM call saved per
    message. See docs/decisions/0008-kod-yazar-onay-erken-cikis.md.
    """

    text: str
    final: bool = True


class ToolError(Exception):
    """Raise with a user-facing message when a tool can't do what was asked."""


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    args_model: type[pydantic.BaseModel]
    spec: ToolSpec

    def run(self, ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        # Raises pydantic.ValidationError if the model sent wrong arguments.
        args = self.args_model.model_validate(arguments)
        result = self.fn(ctx, **{name: getattr(args, name) for name in self.args_model.model_fields})
        return result if isinstance(result, ToolResult) else ToolResult(str(result))


TOOL_ATTR = "__erranderpy_tool__"


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: register `fn` as a tool. The function itself is left unchanged."""
    setattr(fn, TOOL_ATTR, build_tool(fn))
    return fn


def get_tool(obj: Any) -> Tool | None:
    return getattr(obj, TOOL_ATTR, None)


def build_tool(fn: Callable[..., Any]) -> Tool:
    hints = typing.get_type_hints(fn, include_extras=True)
    params = list(inspect.signature(fn).parameters.values())
    if not params or params[0].name != "ctx":
        raise TypeError(f"tool {fn.__name__}: the first parameter must be `ctx: ToolContext`")

    fields: dict[str, Any] = {}
    for p in params[1:]:
        if p.name not in hints:
            raise TypeError(f"tool {fn.__name__}: parameter '{p.name}' needs a type hint")
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[p.name] = (hints[p.name], default)

    args_model = pydantic.create_model(f"{fn.__name__}_args", **fields)
    description = (inspect.getdoc(fn) or "").strip()
    if not description:
        raise TypeError(f"tool {fn.__name__}: add a docstring, the model uses it to pick the tool")
    schema = clean_schema(args_model.model_json_schema())
    return Tool(fn.__name__, description, fn, args_model, ToolSpec(fn.__name__, description, schema))


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make Pydantic's JSON schema friendlier to every provider.

    - inline `$ref`s to `$defs` (not every provider resolves references)
    - drop auto-generated `title`s (noise in the prompt)
    - turn `X | None = None` (anyOf [X, null]) into a plain optional X
    """
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].split("/")[-1]]
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title":
                continue
            if key == "properties":  # a map of field name -> schema; keep the names
                out[key] = {name: walk(sub) for name, sub in value.items()}
            else:
                out[key] = walk(value)
        any_of = out.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
            real = next(s for s in any_of if s != {"type": "null"})
            out.pop("anyOf")
            if out.get("default", 0) is None:
                out.pop("default")
            out = {**real, **out}
        return out

    return walk(schema)
