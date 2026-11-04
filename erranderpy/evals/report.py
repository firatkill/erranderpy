"""Summarize eval results into the four numbers we compare models on:
accuracy, error rate, latency (p50/p95) and cost."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from erranderpy.evals.runner import CaseResult
from erranderpy.evals.scoring import percentile


@dataclass
class Summary:
    label: str  # "provider / model"
    total: int
    router_acc: float | None
    tools_acc: float | None
    error_rate: float
    errors: Counter
    p50_ms: float | None
    p95_ms: float | None
    avg_cost: float | None
    total_cost: float | None


def _acc(results: list[CaseResult], suite: str) -> float | None:
    rows = [r for r in results if r.suite == suite]
    return sum(r.passed for r in rows) / len(rows) if rows else None


def summarize(label: str, results: list[CaseResult]) -> Summary:
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    errors = Counter(r.error for r in results if r.error)
    return Summary(
        label=label,
        total=len(results),
        router_acc=_acc(results, "router"),
        tools_acc=_acc(results, "tools"),
        error_rate=sum(errors.values()) / len(results) if results else 0.0,
        errors=errors,
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        avg_cost=sum(costs) / len(costs) if costs else None,
        total_cost=sum(costs) if costs else None,
    )


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def _ms(x: float | None) -> str:
    return "–" if x is None else f"{x / 1000:.2f} s"


def _usd(x: float | None, digits: int = 6) -> str:
    return "–" if x is None else f"${x:.{digits}f}"


def summary_table(summaries: list[Summary]) -> str:
    lines = [
        "| Model | Router | Tools | Errors | p50 | p95 | Avg cost / call | Run cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        err = _pct(s.error_rate)
        if s.errors:
            err += " (" + ", ".join(f"{k} {v}" for k, v in s.errors.most_common()) + ")"
        lines.append(
            f"| {s.label} | {_pct(s.router_acc)} | {_pct(s.tools_acc)} | {err} | "
            f"{_ms(s.p50_ms)} | {_ms(s.p95_ms)} | {_usd(s.avg_cost)} | {_usd(s.total_cost, 4)} |"
        )
    return "\n".join(lines)


def failures_section(label: str, results: list[CaseResult]) -> str:
    failed = [r for r in results if not r.passed]
    if not failed:
        return f"### {label}\n\nAll cases passed.\n"
    lines = [f"### {label}", "", "| Case | Message | Expected | Got |", "|---|---|---|---|"]
    for r in failed:
        got = r.error if r.actual is None else f"{r.actual}" + (f" ({r.error})" if r.error else "")
        lines.append(f"| {r.case_id} | {r.message} | `{r.expected}` | `{got}` |")
    return "\n".join(lines) + "\n"


def markdown_report(runs: list[tuple[Summary, list[CaseResult]]], suites: list[str], repeat: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        f"# Model comparison — {now}",
        "",
        f"Suites: {', '.join(suites)} · runs per case: {repeat}",
        "",
        summary_table([s for s, _ in runs]),
        "",
        "Router / Tools = share of cases fully correct. Errors = calls that failed or returned no usable "
        "decision. Costs use list prices in `erranderpy/core/llm/pricing.py` (DeepSeek at peak price).",
        "",
        "## Failed cases",
        "",
    ]
    parts += [failures_section(s.label, results) for s, results in runs]
    return "\n".join(parts)
