"""Online eval ("canary"): run the router eval against the agents the bot is running.

Some faults raise no error at all. Chaos test, 2026-11-05: with a broken
description in shopping/agent.yaml the router quietly sent "zeytinyağı almam
lazım" to the tasks agent, which added it as a task. Every span succeeded; no
detector in signals.py could see it. The eval set knows where those messages
belong, so running it regularly turns a silent regression into a signal.

Runs before the daily report (~40 router calls, well under a cent).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from erranderpy.core.llm import LLMProvider
from erranderpy.evals.runner import WORKERS, load_cases, run_router_case
from erranderpy.oncall.signals import WARNING, Signal

# The model is not deterministic: one miss in ~40 cases is noise, not a regression.
MIN_FAILURES = 2


def router_canary(llm: LLMProvider, agents: dict[str, Any]) -> Signal | None:
    # The eval cases assume the built-in agents: an ability the bot created for the
    # user (a calendar) rightly takes messages the cases expect elsewhere.
    built_in = [a for a in agents.values() if a.created_by != "builder"]
    cases = load_cases("router")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda c: run_router_case(llm, c, built_in), cases))
    measured = [r for r in results if r.error not in ("auth", "bad_request", "timeout", "connection", "server", "rate_limit")]
    if len(measured) < len(results) / 2:
        return None  # the LLM is failing; llm_* signals already say so
    failed = [r for r in measured if not r.passed]
    if len(failed) < MIN_FAILURES:
        return None
    examples = [f"{r.case_id} \"{r.message}\": expected {r.expected}, got {r.actual}" for r in failed[:8]]
    return Signal(
        "eval_regression", WARNING, len(failed),
        f"router eval on the running agents: {len(measured) - len(failed)}/{len(measured)} passed "
        "(messages now go to a different agent than the eval expects)",
        examples,
    )
