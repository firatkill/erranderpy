"""Which providers we support, in which priority, and how to call each one.

This is the single place to edit when a provider ships a new cheap model or
changes a parameter. Each spec also encodes the provider's quirks: these
settings make every provider answer *fast and cheap*, which is what our
decision-making calls (routing, picking a tool) need.
See ADR-0007 in AGENTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    env_key: str
    default_model: str
    adapter: Literal["openai_compat", "anthropic"]
    base_url: str | None = None
    # OpenAI's reasoning models want `max_completion_tokens`; most others `max_tokens`.
    max_tokens_param: str = "max_tokens"
    # Extra top-level request parameters understood by the SDK.
    extra_params: dict[str, Any] = field(default_factory=dict)
    # Fields the OpenAI SDK doesn't know about, sent as-is in the JSON body.
    extra_body: dict[str, Any] = field(default_factory=dict)


# Order matters: when several keys are set, the first one wins.
PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(
        name="anthropic",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-haiku-5-5",
        adapter="anthropic",
        # Haiku 5.5 thinks by default (effort "medium"). "low" keeps routing
        # quick and cheap; thinking tokens are billed as output.
        extra_params={"output_config": {"effort": "low"}},
    ),
    ProviderSpec(
        name="gemini",
        env_key="GEMINI_API_KEY",
        default_model="gemini-3.1-flash-lite",
        adapter="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        # Gemini 3 models can't turn reasoning off; "low" is the cheapest
        # level documented for the OpenAI-compatible endpoint.
        extra_params={"reasoning_effort": "low"},
    ),
    ProviderSpec(
        name="deepseek",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-flash",
        adapter="openai_compat",
        base_url="https://api.deepseek.com",
        # Thinking is ON unless disabled; it also slows answers down and
        # rejects some tool settings.
        extra_body={"thinking": {"type": "disabled"}},
    ),
    ProviderSpec(
        name="openai",
        env_key="OPENAI_API_KEY",
        default_model="gpt-6-luna",
        adapter="openai_compat",
        max_tokens_param="max_completion_tokens",
        # gpt-6-luna supports function calling in Chat Completions only with
        # reasoning effort "none".
        extra_params={"reasoning_effort": "none"},
    ),
]

PROVIDERS_BY_NAME: dict[str, ProviderSpec] = {p.name: p for p in PROVIDERS}

REQUEST_TIMEOUT_S = 30.0
# Both SDKs retry 408/409/429/5xx and connection errors with exponential
# backoff on their own; we just make the number explicit.
MAX_RETRIES = 2
