"""The agent loop: let the model call tools until the request is done.

    model ──tool call──▶ our code runs the tool ──result──▶ model ──▶ ...

Two rules keep it fast and safe:
- Early exit: when every tool in a turn returns a *final* result, that text is
  the reply and we stop. No extra LLM call just to rephrase "added olive oil"
  (ADR-0008).
- At most MAX_STEPS model calls per request, so a confused model can't loop forever.
"""

from __future__ import annotations

from datetime import datetime

import pydantic

from erranderpy.core.llm import LLMProvider, Message
from erranderpy.core.agent import Agent
from erranderpy.core.tools import ToolContext, ToolError, ToolResult

MAX_STEPS = 5

COMMON_INSTRUCTIONS = """\
Rules for every agent:
- Reply in the same language as the user.
- Act only through your tools. Never say you did something without calling a tool.
- If the request is unclear, ask one short question instead of guessing, and call no tool in that reply.
- If none of your tools can do what the user asked, say so plainly. Never call a different tool instead.
- Earlier messages are only context; act on the latest user message."""


class TooManySteps(Exception):
    pass


def build_agent_prompt(agent, now: datetime | None = None, others: list | None = None) -> str:
    """The agent's own prompt + rules for every agent + today's date.

    The date goes last: it changes daily, and providers cache the *start* of a
    prompt, so the stable part stays cacheable. The model uses it only to pick
    words like "tomorrow" or "thursday"; the actual date math is done in code.
    """
    prompt = f"{agent.system_prompt}\n\n{COMMON_INSTRUCTIONS}"
    if "agents" in getattr(agent, "context", []) and others is not None:
        listing = "\n".join(f"- {a.name} ({a.title or a.name}): {a.description}" for a in others if a.name != agent.name)
        prompt += f"\n\nAgents that already exist:\n{listing or '- none'}"
    if now is not None:
        prompt += f"\n\nToday is {now:%A}, {now:%Y-%m-%d}."
    return prompt


def run_agent(llm: LLMProvider, agent: Agent, text: str, ctx: ToolContext, history: list[Message]) -> str:
    assert ctx.tracer is not None
    messages = [*history, Message.user(text)]
    system = build_agent_prompt(agent, ctx.now, list(ctx.extras.get("agents", {}).values()))

    for step in range(MAX_STEPS):
        with ctx.tracer.span("agent.llm", agent=agent.name, input=text if step == 0 else f"step {step + 1}") as span:
            result = llm.complete(system, messages, tools=agent.tool_specs)
            span.record_llm(result)
            span.output = {"text": result.text, "tool_calls": [{"name": c.name, "args": c.arguments} for c in result.tool_calls]}

        if not result.tool_calls:
            return result.text.strip()  # a question back to the user, or a plain answer

        messages.append(result.message)
        outputs = [_run_tool_call(agent, call.name, call.arguments, ctx) for call in result.tool_calls]
        for call, output in zip(result.tool_calls, outputs, strict=True):
            messages.append(Message.tool_result(call.id, output.text))

        if all(o.final for o in outputs):
            return "\n".join(o.text for o in outputs)  # early exit

    raise TooManySteps(f"agent {agent.name} did not finish in {MAX_STEPS} steps")


def _run_tool_call(agent: Agent, name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
    assert ctx.tracer is not None
    with ctx.tracer.span("tool.call", agent=agent.name, tool=name, input=arguments) as span:
        tool = agent.tools.get(name)
        if tool is None:
            # Tell the model what went wrong; it gets another step to fix it.
            span.error = "unknown_tool"
            output = ToolResult(f"There is no tool named {name!r}. Available: {', '.join(agent.tools)}.", final=False)
        else:
            try:
                output = tool.run(ctx, arguments)
            except pydantic.ValidationError as exc:
                span.error = "invalid_arguments"
                output = ToolResult(f"Invalid arguments for {name}: {exc.errors(include_url=False)}", final=False)
            except ToolError as exc:
                span.error = "tool_error"
                output = ToolResult(str(exc), final=True)  # already a user-facing message
        span.output = output.text
        return output
