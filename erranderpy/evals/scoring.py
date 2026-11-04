"""Decide whether a model's decision matches what we expected.

Because the LLM only *decides* (which agent, which tool, which arguments) and
never produces free text we have to judge, almost everything here is an exact
comparison after light normalization. See docs/decisions/0003-llm-karar-verir-kod-yapar.md.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from erranderpy.core.text import normalize, tr_lower

__all__ = ["match", "normalize", "percentile", "same_agents", "tr_lower"]


def match(expected: Any, actual: Any) -> bool:
    """Recursively check `actual` against `expected` (see evals/tools_tr.yaml for the rules)."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and match(value, actual[key]) for key, value in expected.items())

    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        remaining = list(actual)
        for exp_item in expected:  # order-insensitive, each actual item used once
            hit = next((i for i, act in enumerate(remaining) if match(exp_item, act)), None)
            if hit is None:
                return False
            remaining.pop(hit)
        return True

    if isinstance(expected, str):
        if not isinstance(actual, str):
            return False
        if expected.startswith("~"):
            return normalize(expected[1:]) in normalize(actual)
        return normalize(expected) == normalize(actual)

    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual

    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and float(expected) == float(actual)

    return expected == actual


def same_agents(expected: list[str], actual: list[str]) -> bool:
    """Router check: the same agents, as a multiset (order doesn't matter)."""
    return Counter(expected) == Counter(actual)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile: p50 is the median, p95 is the 'bad day' latency."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]
