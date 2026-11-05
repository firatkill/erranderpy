"""Provider-neutral types for talking to an LLM.

Everything above this layer (router, agents, evals) only sees the types in this
file. Adapters (`openai_compat.py`, `anthropic_native.py`) translate them to and
from each provider's wire format, so swapping providers never touches agent code.
See ADR-0002 in AGENTS.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]

# We deliberately expose only "auto" and "none". Forcing a tool call works
# differently (or not at all) across providers and reasoning modes, so callers
# ask for a tool in the prompt and validate the result instead.
# See ADR-0006 in AGENTS.md.
ToolChoice = Literal["auto", "none"]


@dataclass(frozen=True)
class ToolSpec:
    """A function the model may ask us to call. `parameters` is a JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    """One turn of a conversation.

    `raw` / `raw_provider` carry the provider's own representation of an
    assistant turn. Some providers (Anthropic with thinking on) require their
    exact blocks to be sent back in a tool loop; adapters replay `raw` only
    when it came from the same provider and ignore it otherwise.
    """

    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages
    raw: Any = None
    raw_provider: str | None = None

    @staticmethod
    def user(text: str) -> Message:
        return Message(role="user", text=text)

    @staticmethod
    def tool_result(tool_call_id: str, content: str) -> Message:
        return Message(role="tool", text=content, tool_call_id=tool_call_id)


@dataclass
class LLMResult:
    message: Message  # the assistant turn, ready to append to the history
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    cost_usd: float | None  # None when we don't know the model's price

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


ErrorKind = Literal[
    "auth",  # wrong or missing key: retrying won't help
    "rate_limit",  # too many requests (SDK already retried)
    "timeout",
    "connection",
    "server",  # provider-side 5xx / overloaded
    "bad_request",  # our request is invalid (unsupported parameter, unknown model, ...)
    "invalid_output",  # the model answered, but not in a shape we can use
]


class LLMError(Exception):
    def __init__(self, kind: ErrorKind, message: str, provider: str = "") -> None:
        super().__init__(f"[{provider or 'llm'}:{kind}] {message}")
        self.kind = kind
        self.provider = provider


class LLMProvider(ABC):
    """The one interface the rest of the app depends on."""

    name: str  # "anthropic", "gemini", "deepseek", "openai"
    model: str

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        max_tokens: int = 2048,
    ) -> LLMResult:
        """Send one request and return the assistant's turn.

        Note there is no `temperature`: several current models reject it, and
        reasoning models ignore it. Consistency comes from clear prompts and
        structured output instead.
        """
