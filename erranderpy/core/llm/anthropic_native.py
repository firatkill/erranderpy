"""Adapter for Anthropic's Messages API (via the official `anthropic` SDK).

Differences from the OpenAI format that this file hides:
- the system prompt is a separate `system` parameter, not a message;
- a response is a list of typed content blocks (`text`, `tool_use`, `thinking`);
- tool results go back inside a *user* message as `tool_result` blocks, and all
  results for one assistant turn must share a single user message;
- with thinking on, an assistant turn must be replayed with its exact blocks.

(Named `anthropic_native` so it isn't confused with the `anthropic` package.)
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from erranderpy.core.llm.base import (
    LLMProvider,
    LLMResult,
    Message,
    ToolCall,
    ToolChoice,
    ToolSpec,
)
from erranderpy.core.llm.defaults import MAX_RETRIES, REQUEST_TIMEOUT_S, ProviderSpec
from erranderpy.core.llm.errors import to_llm_error
from erranderpy.core.llm.pricing import cost_usd


class AnthropicProvider(LLMProvider):
    def __init__(self, spec: ProviderSpec, api_key: str, model: str | None = None, client: Any = None):
        self.spec = spec
        self.name = spec.name
        self.model = model or spec.default_model
        self._client = client or anthropic.Anthropic(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        max_tokens: int = 2048,
    ) -> LLMResult:
        request: dict[str, Any] = {
            "model": self.model,
            # Thinking tokens count toward max_tokens, so don't set it too low.
            "max_tokens": max_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages, self.name),
            **self.spec.extra_params,
        }
        if tools:
            request["tools"] = [to_anthropic_tool(t) for t in tools]
            request["tool_choice"] = {"type": tool_choice}

        started = time.perf_counter()
        try:
            response = self._client.messages.create(**request)
        except Exception as exc:
            raise to_llm_error(exc, anthropic, self.name) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        message = from_anthropic_content(response.content, self.name)
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens  # includes thinking tokens
        return LLMResult(
            message=message,
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost_usd(self.model, tokens_in, tokens_out),
        )


def to_anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def to_anthropic_messages(messages: list[Message], provider: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        # All tool results for one assistant turn go in ONE user message.
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "tool":
            pending_results.append({"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text})
            continue
        flush_results()
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            if m.raw_provider == provider and m.raw is not None:
                # Replay our own blocks exactly (thinking blocks included).
                out.append({"role": "assistant", "content": m.raw})
                continue
            blocks: list[dict[str, Any]] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            out.append({"role": "assistant", "content": blocks})
    flush_results()
    return out


def from_anthropic_content(content: list[Any], provider: str) -> Message:
    texts: list[str] = []
    calls: list[ToolCall] = []
    # Read blocks by type, never by position: a response may start with thinking blocks.
    for block in content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
    raw = [_block_to_dict(b) for b in content]
    return Message(role="assistant", text="".join(texts), tool_calls=calls, raw=raw, raw_provider=provider)


def _block_to_dict(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return block
