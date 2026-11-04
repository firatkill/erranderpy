"""Decide which agent(s) should handle a message.

The router is one LLM call with one tool, `route`, whose argument is a list of
{agent, text}. A message like "zeytinyağı al, bir de bugün 20 dk ingilizce
çalıştım" becomes two routes, one per agent. The prompt is built from each
agent's `description` and `examples`, so a new agent folder is routable
without touching this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from erranderpy.core.llm import LLMProvider, LLMResult, Message, ToolSpec
from erranderpy.core.tracing import Tracer

UNKNOWN = "unknown"


class Routable(Protocol):
    """Anything with these attributes can be routed to (real agents and eval prototypes)."""

    name: str
    description: str
    examples: list[str]


@dataclass(frozen=True)
class Route:
    agent: str
    text: str


def build_router_prompt(agents: Sequence[Routable]) -> str:
    lines = [
        "You are the router of a personal assistant. Users write short messages, usually in Turkish.",
        "A message may contain several requests. Split it and send each request to exactly one agent.",
        "Earlier messages are only context; route only the latest user message.",
        "",
        "Agents:",
    ]
    for agent in agents:
        examples = "; ".join(f'"{e}"' for e in agent.examples)
        lines.append(f"- {agent.name}: {agent.description}" + (f" Examples: {examples}" if examples else ""))
    lines += [
        f"- {UNKNOWN}: anything else: greetings, chit-chat, questions the agents can't handle, unclear messages.",
        "",
        "Always answer by calling the `route` tool exactly once. Never reply with plain text.",
    ]
    return "\n".join(lines)


def route_tool(agent_names: Sequence[str]) -> ToolSpec:
    return ToolSpec(
        "route",
        "Send each request in the user's latest message to one agent.",
        {
            "type": "object",
            "properties": {
                "routes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string", "enum": [*agent_names, UNKNOWN]},
                            "text": {"type": "string", "description": "This request, in the user's own words."},
                        },
                        "required": ["agent", "text"],
                    },
                }
            },
            "required": ["routes"],
        },
    )


def parse_routes(result: LLMResult, valid_names: Sequence[str], original_text: str) -> list[Route] | None:
    """Read the routes out of the model's answer; None if it didn't call `route`."""
    call = next((c for c in result.tool_calls if c.name == "route"), None)
    if call is None:
        return None
    # Requests for the same agent are merged into one route, in code: one agent
    # call instead of two ("2 paket makarna, bir de süt" -> one shopping call).
    # The model sometimes splits them, sometimes not; code makes it deterministic.
    texts_by_agent: dict[str, list[str]] = {}
    for item in call.arguments.get("routes") or []:
        if not isinstance(item, dict):
            continue
        agent = item.get("agent")
        agent = agent if agent in valid_names else UNKNOWN
        text = str(item.get("text") or "").strip() or original_text
        texts_by_agent.setdefault(agent, []).append(text)
    routes = [Route(agent, ", ".join(texts)) for agent, texts in texts_by_agent.items()]
    return routes or None


def route(
    llm: LLMProvider,
    agents: Sequence[Routable],
    text: str,
    history: list[Message],
    tracer: Tracer,
) -> list[Route]:
    names = [a.name for a in agents]
    with tracer.span("router.decide", input=text) as span:
        result = llm.complete(build_router_prompt(agents), [*history, Message.user(text)], tools=[route_tool(names)])
        span.record_llm(result)
        routes = parse_routes(result, names, text)
        if routes is None:
            # The model answered in plain text instead of routing (see ADR-0006).
            # Treat the message as "unknown" rather than failing.
            span.error = "no_tool_call"
            routes = [Route(UNKNOWN, text)]
        span.output = " | ".join(f"{r.agent}: {r.text}" for r in routes)
    return routes
