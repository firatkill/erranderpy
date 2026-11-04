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
