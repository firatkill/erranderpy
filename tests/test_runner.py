"""End-to-end check of the eval pipeline with a scripted fake provider (no API calls)."""

from erranderpy.core.llm import LLMError, LLMProvider, LLMResult, Message, ToolCall
from erranderpy.evals.report import markdown_report, summarize
from erranderpy.evals.runner import load_cases, run_suite


class ScriptedProvider(LLMProvider):
    """Answers every router case correctly from the dataset, fails one case on purpose."""

    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.answers = {c["message"]: c for c in load_cases("router")}

    def complete(self, system, messages, tools=None, tool_choice="auto", max_tokens=2048):
        text = messages[-1].text
        if text == "hmm":
            raise LLMError("timeout", "simulated", self.name)
        case = self.answers[text]
        routes = [{"agent": a, "text": text} for a in case["expect"]]
        # `requests: N` cases: N requests, the extra ones for the first agent.
        routes += [{"agent": case["expect"][0], "text": text}] * (case.get("requests", len(routes)) - len(routes))
        if text == "selam":
            routes = [{"agent": "shopping", "text": text}]  # a wrong decision
        call = ToolCall("c1", "route", {"routes": routes})
        return LLMResult(Message(role="assistant", tool_calls=[call]), self.name, self.model, 10, 5, 120.0, 0.00001)


def test_router_suite_end_to_end():
    results = run_suite(ScriptedProvider(), "router")
    by_id = {r.case_id: r for r in results}
    assert by_id["r27"].passed is False  # "selam" routed to shopping
    assert by_id["r29"].error == "timeout"  # "hmm"
    assert by_id["r23"].passed  # multi-request message

    summary = summarize("fake / fake-1", results)
    n = len(load_cases("router"))  # the set grows with real use; don't hard-code its size
    assert summary.total == n
    assert summary.router_acc == (n - 2) / n
    assert summary.errors["timeout"] == 1
    assert summary.p50_ms == 120.0

    report = markdown_report([(summary, results)], ["router"], 1)
    assert f"| fake / fake-1 | {(n - 2) / n * 100:.0f}% |" in report
    assert "| r27 | selam |" in report
