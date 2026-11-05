"""Run eval cases against one provider and collect per-case results.

The evals use the *same* code the bot uses: the router prompt from
`core/router.py`, agent prompts from `core/runtime.py`, and real agents from
`agents/`. Only agents that don't exist yet come from `fixtures.py`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

import pydantic

from erranderpy.core.db import Database
from erranderpy.core.llm import LLMError, LLMProvider, LLMResult, Message, ToolCall
from erranderpy.core.registry import AGENTS_DIR, load_agents
from erranderpy.core.router import build_router_prompt, parse_routes, route_tool
from erranderpy.core.runtime import build_agent_prompt, requests_note
from erranderpy.core.tools import ToolContext
from erranderpy.evals.fixtures import PROTOTYPES
from erranderpy.evals.scoring import match, same_agents

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"

# A few requests in parallel keeps a full run under a minute without
# hitting the providers' rate limits.
WORKERS = 4


@dataclass
class CaseResult:
    suite: str
    case_id: str
    message: str
    expected: Any
    actual: Any = None
    passed: bool = False
    error: str | None = None  # an LLMError kind, or "no_tool_call" / "wrong_tool"
    latency_ms: float | None = None
    cost_usd: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0


FIXTURE_AGENTS_DIR = EVALS_DIR / "agents"
EVAL_TZ = ZoneInfo("Europe/Istanbul")


@cache
def routing_agents() -> dict[str, Any]:
    """What the router sees: the built-in agents (plus prototypes of planned ones)."""
    return {**PROTOTYPES, **load_agents(AGENTS_DIR)}


@cache
def eval_agents() -> dict[str, Any]:
    """Agents tool cases can target: built-in ones, plus example agents the bot
    could create for itself (evals/agents/, e.g. a calendar), so declarative
    agents are measured too."""
    return {**routing_agents(), **load_agents(FIXTURE_AGENTS_DIR)}


def _history(case: dict[str, Any]) -> list[Message]:
    """Optional earlier turns: history: [[user, "..."], [assistant, "..."]]."""
    return [Message(role=role, text=text) for role, text in case.get("history") or []]


def load_cases(suite: str) -> list[dict[str, Any]]:
    data = yaml.safe_load((EVALS_DIR / f"{suite}_tr.yaml").read_text(encoding="utf-8"))
    cases = list(data["cases"])
    if suite == "tools":
        # Each agent keeps its own tool cases next to its code.
        for path in sorted([*AGENTS_DIR.glob("*/evals.yaml"), *FIXTURE_AGENTS_DIR.glob("*/evals.yaml")]):
            if path.parent.name.startswith(("_", ".")):
                continue
            agent_cases = yaml.safe_load(path.read_text(encoding="utf-8")).get("cases") or []
            cases += [{"agent": path.parent.name, **case} for case in agent_cases]
    return cases


def _record_usage(result: CaseResult, llm: LLMResult) -> None:
    result.latency_ms = llm.latency_ms
    result.cost_usd = llm.cost_usd
    result.tokens_in = llm.tokens_in
    result.tokens_out = llm.tokens_out


def run_router_case(provider: LLMProvider, case: dict[str, Any], agents: list[Any] | None = None) -> CaseResult:
    """`agents`: route among these instead of the agents on disk (the on-call
    canary passes the agents the running bot actually has)."""
    result = CaseResult("router", case["id"], case["message"], case["expect"])
    agents = agents if agents is not None else list(routing_agents().values())
    names = [a.name for a in agents]
    messages = [*_history(case), Message.user(case["message"])]
    try:
        llm = provider.complete(build_router_prompt(agents), messages, tools=[route_tool(names)])
    except LLMError as exc:
        result.error = exc.kind
        return result
    _record_usage(result, llm)

    routes = parse_routes(llm, names, case["message"])
    if routes is None:
        result.error = "no_tool_call"
        result.actual = llm.text[:200]
        return result
    result.actual = [r.agent for r in routes]
    result.passed = same_agents(case["expect"], result.actual)
    if "requests" in case:
        # How many separate requests the router found (the runtime's multi-request
        # check runs only from 2). Several requests for one agent share one route.
        found = sum(max(len(r.requests), 1) for r in routes)
        result.actual = {"agents": result.actual, "requests": found}
        result.passed = result.passed and found == case["requests"]
    return result


def run_tool_case(provider: LLMProvider, case: dict[str, Any]) -> CaseResult:
    result = CaseResult("tools", case["id"], case["message"], case["expect"])
    agent = eval_agents()[case["agent"]]
    # Exactly the prompt the bot uses; the builder also sees the agents that exist.
    system = build_agent_prompt(agent, datetime.now(), list(routing_agents().values()))
    system += requests_note(case.get("requests") or [])  # as the router would split it
    system += _live_context(agent, case.get("entries") or [])
    messages = [*_history(case), Message.user(case["message"])]
    try:
        llm = provider.complete(system, messages, tools=agent.tool_specs)
    except LLMError as exc:
        result.error = exc.kind
        return result
    _record_usage(result, llm)

    result.passed, result.actual, result.error = check_tool_expectation(case["expect"], _with_defaults(agent, llm))
    return result


def _live_context(agent: Any, entries: list[dict[str, Any]]) -> str:
    """entries: [...] in a case = records that already exist. They are saved with the
    agent's real add tool (add_entry, or a code agent's own add_*) into a throwaway
    database, and the prompt gets exactly the context the bot would build from them."""
    if getattr(agent, "live_context", None) is None:
        return ""
    db = Database(":memory:")
    if agent.path is not None and (agent.path / "schema.sql").exists():
        db.apply_schema((agent.path / "schema.sql").read_text(encoding="utf-8"))
    ctx = ToolContext(db, 1, 1, datetime.now(EVAL_TZ), agent.messages_for("tr"))
    add = agent.tools.get("add_entry") or next(t for name, t in agent.tools.items() if name.startswith("add_"))
    for entry in entries:
        add.run(ctx, entry)
    ctx.extras.clear()  # the case's turn starts after these were saved
    return agent.live_context(ctx)


def _with_defaults(agent: Any, llm: LLMResult) -> LLMResult:
    """Compare what the tool actually receives: the model's arguments with the
    tool's defaults filled in. `list_tasks()` and `list_tasks(day="today")` are the
    same call; an eval must not fail one of them."""
    tools = getattr(agent, "tools", None)
    if not tools:
        return llm  # prototypes have no code, so no defaults to fill in
    calls = []
    for call in llm.tool_calls:
        tool = tools.get(call.name)
        args = call.arguments
        if tool is not None:
            try:
                args = tool.args_model.model_validate(args).model_dump(mode="json")
            except pydantic.ValidationError:
                pass  # invalid arguments are compared as sent (and will fail)
        calls.append(ToolCall(call.id, call.name, args))
    return replace(llm, message=replace(llm.message, tool_calls=calls))


def check_tool_expectation(expect: dict[str, Any], llm: LLMResult) -> tuple[bool, Any, str | None]:
    """Return (passed, what the model did, error). Expectation forms:
    {tool, args}            the first tool call
    {calls: [...]}          every tool call of the turn (order doesn't matter)
    {no_tool: true}         a question back, no action
    {any: [expect, ...]}    any one of several acceptable answers
    """
    if "any" in expect:
        outcomes = [check_tool_expectation(e, llm) for e in expect["any"]]
        passed = next((o for o in outcomes if o[0]), None)
        return passed or (False, outcomes[0][1], outcomes[0][2])
    if expect.get("no_tool"):
        actual = {"text": llm.text[:200], "tools": [c.name for c in llm.tool_calls]}
        return (not llm.tool_calls and bool(llm.text.strip())), actual, None
    if not llm.tool_calls:
        return False, llm.text[:200], "no_tool_call"
    if "calls" in expect:
        actual = [{"tool": c.name, "args": c.arguments} for c in llm.tool_calls]
        return match(expect["calls"], actual), actual, None
    call = llm.tool_calls[0]
    actual = {"tool": call.name, "args": call.arguments}
    if call.name != expect["tool"]:
        return False, actual, "wrong_tool"
    return match(expect.get("args", {}), call.arguments), actual, None


RUNNERS = {"router": run_router_case, "tools": run_tool_case}


def run_suite(provider: LLMProvider, suite: str, runs: int = 1) -> list[CaseResult]:
    cases = load_cases(suite) * runs
    runner = RUNNERS[suite]
    # Load agents once, *before* starting threads. Importing the same agent
    # modules from four threads at once lets one import overwrite another's
    # half-finished module (a race condition we actually hit).
    eval_agents()  # also loads routing_agents()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda case: runner(provider, case), cases))
