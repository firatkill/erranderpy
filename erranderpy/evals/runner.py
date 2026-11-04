"""Run eval cases against one provider and collect per-case results."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from erranderpy.core.llm import LLMError, LLMProvider, LLMResult, Message
from erranderpy.evals.fixtures import AGENTS, ROUTE_TOOL, router_system_prompt
from erranderpy.evals.scoring import match, same_agents

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"

# A few requests in parallel keeps a 46-case run under a minute without
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


def load_cases(suite: str) -> list[dict[str, Any]]:
    path = EVALS_DIR / f"{suite}_tr.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["cases"]


def _record_usage(result: CaseResult, llm: LLMResult) -> None:
    result.latency_ms = llm.latency_ms
    result.cost_usd = llm.cost_usd
    result.tokens_in = llm.tokens_in
    result.tokens_out = llm.tokens_out


def run_router_case(provider: LLMProvider, case: dict[str, Any]) -> CaseResult:
    result = CaseResult("router", case["id"], case["message"], case["expect"])
    try:
        llm = provider.complete(router_system_prompt(), [Message.user(case["message"])], tools=[ROUTE_TOOL])
    except LLMError as exc:
        result.error = exc.kind
        return result
    _record_usage(result, llm)

    call = next((c for c in llm.tool_calls if c.name == ROUTE_TOOL.name), None)
    if call is None:
        result.error = "no_tool_call"
        result.actual = llm.text[:200]
        return result
    routes = call.arguments.get("routes") or []
    agents = [r.get("agent") for r in routes if isinstance(r, dict)]
    result.actual = agents
    result.passed = same_agents(case["expect"], agents)
    return result


def run_tool_case(provider: LLMProvider, case: dict[str, Any]) -> CaseResult:
    result = CaseResult("tools", case["id"], case["message"], case["expect"])
    agent = AGENTS[case["agent"]]
    try:
        llm = provider.complete(agent.system_prompt, [Message.user(case["message"])], tools=agent.tools)
    except LLMError as exc:
        result.error = exc.kind
        return result
    _record_usage(result, llm)

    if not llm.tool_calls:
        result.error = "no_tool_call"
        result.actual = llm.text[:200]
        return result
    call = llm.tool_calls[0]
    result.actual = {"tool": call.name, "args": call.arguments}
    if call.name != case["expect"]["tool"]:
        result.error = "wrong_tool"
        return result
    result.passed = match(case["expect"].get("args", {}), call.arguments)
    return result


RUNNERS = {"router": run_router_case, "tools": run_tool_case}


def run_suite(provider: LLMProvider, suite: str, runs: int = 1) -> list[CaseResult]:
    cases = load_cases(suite) * runs
    runner = RUNNERS[suite]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda case: runner(provider, case), cases))
