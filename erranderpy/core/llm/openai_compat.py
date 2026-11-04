"""Adapter for the OpenAI Chat Completions format.

OpenAI, DeepSeek and Gemini (through its OpenAI-compatible endpoint) all speak
this format, so one adapter serves three providers; only `base_url`, the key
and a few quirks in `ProviderSpec` differ.
"""

from __future__ import annotations

import json
import time
from typing import Any

import openai

from erranderpy.core.llm.base import (
    LLMError,
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


class OpenAICompatProvider(LLMProvider):
    def __init__(self, spec: ProviderSpec, api_key: str, model: str | None = None, client: Any = None):
        self.spec = spec
        self.name = spec.name
        self.model = model or spec.default_model
        # `client` is injectable so tests can pass a fake instead of hitting the network.
        self._client = client or openai.OpenAI(
            api_key=api_key,
            base_url=spec.base_url,
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
            "messages": to_openai_messages(system, messages),
            self.spec.max_tokens_param: max_tokens,
            **self.spec.extra_params,
        }
        if tools:
            request["tools"] = [to_openai_tool(t) for t in tools]
            request["tool_choice"] = tool_choice
        if self.spec.extra_body:
            request["extra_body"] = self.spec.extra_body

        started = time.perf_counter()
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise to_llm_error(exc, openai, self.name) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        if not response.choices:
            raise LLMError("invalid_output", "response has no choices", self.name)
        message = from_openai_message(response.choices[0].message, self.name)

        usage = response.usage
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        return LLMResult(
            message=message,
            provider=self.name,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost_usd(self.model, tokens_in, tokens_out),
        )


def to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    # In this format the system prompt is just the first message.
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        # Arguments travel as a JSON *string* in this format.
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                    }
                    for c in m.tool_calls
                ]
            out.append(entry)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
    return out


def from_openai_message(msg: Any, provider: str) -> Message:
    calls: list[ToolCall] = []
    for c in getattr(msg, "tool_calls", None) or []:
        raw_args = c.function.arguments or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            # The model produced broken JSON. Callers may retry once with the error.
            raise LLMError("invalid_output", f"tool arguments are not valid JSON: {raw_args!r}", provider) from exc
        if not isinstance(args, dict):
            raise LLMError("invalid_output", f"tool arguments are not an object: {raw_args!r}", provider)
        calls.append(ToolCall(id=c.id, name=c.function.name, arguments=args))
    return Message(role="assistant", text=getattr(msg, "content", None) or "", tool_calls=calls)
