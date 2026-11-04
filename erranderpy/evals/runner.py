"""Run eval cases against one provider and collect per-case results.

The evals use the *same* code the bot uses: the router prompt from
`core/router.py`, agent prompts from `core/runtime.py`, and real agents from
`agents/`. Only agents that don't exist yet come from `fixtures.py`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from erranderpy.core.llm import LLMError, LLMProvider, LLMResult, Message
from erranderpy.core.registry import AGENTS_DIR, load_agents
from erranderpy.core.router import build_router_prompt, parse_routes, route_tool
from erranderpy.core.runtime import build_agent_prompt
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


@cache
def eval_agents() -> dict[str, Any]:
    """Real agents first; prototypes only for agents that don't exist yet."""
    return {**PROTOTYPES, **load_agents(AGENTS_DIR)}


def load_cases(suite: str) -> list[dict[str, Any]]:
    data = yaml.safe_load((EVALS_DIR / f"{suite}_tr.yaml").read_text(encoding="utf-8"))
    cases = list(data["cases"])
    if suite == "tools":
        # Each agent keeps its own tool cases next to its code.
        for path in sorted(AGENTS_DIR.glob("*/evals.yaml")):
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


def run_router_case(provider: LLMProvider, case: dict[str, Any]) -> CaseResult:
    result = CaseResult("router", case["id"], case["message"], case["expect"])
    agents = list(eval_agents().values())
    names = [a.name for a in agents]
    try:
        llm = provider.complete(build_router_prompt(agents), [Message.user(case["message"])], tools=[route_tool(names)])
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
    return result


def run_tool_case(provider: LLMProvider, case: dict[str, Any]) -> CaseResult:
    result = CaseResult("tools", case["id"], case["message"], case["expect"])
    agent = eval_agents()[case["agent"]]
    system = build_agent_prompt(agent, datetime.now())  # exactly the prompt the bot uses
    try:
        llm = provider.complete(system, [Message.user(case["message"])], tools=agent.tool_specs)
    except LLMError as exc:
        result.error = exc.kind
        return result
    _record_usage(result, llm)

    if not llm.tool_calls:
        result.error = "no_tool_call"
        result.actual = llm.text[:200]
        return result
    expect = case["expect"]
    if "calls" in expect:
        # Several requests in one message: compare every call of the turn.
        actual = [{"tool": c.name, "args": c.arguments} for c in llm.tool_calls]
        result.actual = actual
        result.passed = match(expect["calls"], actual)
        return result
    call = llm.tool_calls[0]
    result.actual = {"tool": call.name, "args": call.arguments}
    if call.name != expect["tool"]:
        result.error = "wrong_tool"
        return result
    result.passed = match(expect.get("args", {}), call.arguments)
    return result


RUNNERS = {"router": run_router_case, "tools": run_tool_case}


def run_suite(provider: LLMProvider, suite: str, runs: int = 1) -> list[CaseResult]:
    cases = load_cases(suite) * runs
    runner = RUNNERS[suite]
    # Load agents once, *before* starting threads. Importing the same agent
    # modules from four threads at once lets one import overwrite another's
    # half-finished module (a race condition we actually hit).
    eval_agents()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda case: runner(provider, case), cases))
