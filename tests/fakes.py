"""Test doubles that look like the SDK clients, so adapter tests never hit the network.

They record the request we built (to check we speak the provider's format) and
return a canned response shaped like the real SDK objects.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeOpenAIClient:
    def __init__(self, message: Any, prompt_tokens: int = 100, completion_tokens: int = 20, error: Exception | None = None):
        self.requests: list[dict[str, Any]] = []
        self._message = message
        self._usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self._error:
            raise self._error
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)], usage=self._usage)


def openai_message(content: str | None = None, tool_calls: list[tuple[str, str, str]] | None = None) -> Any:
    """tool_calls: list of (id, name, arguments_json_string)."""
    calls = [
        SimpleNamespace(id=i, type="function", function=SimpleNamespace(name=n, arguments=a))
        for i, n, a in (tool_calls or [])
    ]
    return SimpleNamespace(content=content, tool_calls=calls or None)


class FakeBlock(SimpleNamespace):
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(vars(self))


class FakeAnthropicClient:
    def __init__(self, content: list[FakeBlock], input_tokens: int = 100, output_tokens: int = 20, error: Exception | None = None):
        self.requests: list[dict[str, Any]] = []
        self._content = content
        self._usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        self._error = error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._content, usage=self._usage)


class ScriptedLLM:
    """An LLMProvider that plays back a list of answers, one per call.

    Each item is an LLMResult, an Exception (raised), or a callable
    (system, messages, tools) -> LLMResult. Every call is recorded.
    """

    name = "scripted"
    model = "scripted-1"

    def __init__(self, answers: list[Any]):
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system, messages, tools=None, tool_choice="auto", max_tokens=2048):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.answers:
            raise AssertionError("ScriptedLLM: no more answers scripted")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(system, messages, tools)
        return answer


def tool_answer(*calls: tuple[str, dict], text: str = ""):
    """An LLMResult in which the model calls the given tools: tool_answer(("add_items", {...}))."""
    from erranderpy.core.llm import LLMResult, Message, ToolCall

    tool_calls = [ToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(calls)]
    return LLMResult(Message(role="assistant", text=text, tool_calls=tool_calls), "scripted", "scripted-1", 100, 10, 5.0, 0.00001)


def text_answer(text: str):
    from erranderpy.core.llm import LLMResult, Message

    return LLMResult(Message(role="assistant", text=text), "scripted", "scripted-1", 100, 10, 5.0, 0.00001)


def route_answer(*pairs: tuple[str, str]):
    return tool_answer(("route", {"routes": [{"agent": a, "text": t} for a, t in pairs]}))
