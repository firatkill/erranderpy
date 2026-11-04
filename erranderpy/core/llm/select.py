"""Pick a provider from the environment: whichever API key is set wins.

Rules (see SPEC.md §5):
1. LLM_PROVIDER, if set, forces a provider (its key must exist).
2. Otherwise the first provider in PROVIDERS whose key is set.
3. LLM_MODEL, if set, overrides that provider's default model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from erranderpy.core.llm.anthropic_native import AnthropicProvider
from erranderpy.core.llm.base import LLMProvider
from erranderpy.core.llm.defaults import PROVIDERS, PROVIDERS_BY_NAME
from erranderpy.core.llm.openai_compat import OpenAICompatProvider


class ConfigError(Exception):
    """Configuration is wrong in a way only the user can fix (shown to them as-is)."""


def available_providers(env: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [p.name for p in PROVIDERS if env.get(p.env_key, "").strip()]


def build_provider(name: str, env: Mapping[str, str] | None = None, model: str | None = None) -> LLMProvider:
    env = os.environ if env is None else env
    spec = PROVIDERS_BY_NAME.get(name)
    if spec is None:
        known = ", ".join(PROVIDERS_BY_NAME)
        raise ConfigError(f"Unknown LLM provider '{name}'. Use one of: {known}.")
    key = env.get(spec.env_key, "").strip()
    if not key:
        raise ConfigError(f"{spec.env_key} is empty in .env, so the '{name}' provider can't be used.")
    if spec.adapter == "anthropic":
        return AnthropicProvider(spec, key, model)
    return OpenAICompatProvider(spec, key, model)


def select_provider(env: Mapping[str, str] | None = None) -> tuple[LLMProvider, str]:
    """Return the provider to use and a one-line explanation for the logs."""
    env = os.environ if env is None else env
    model = env.get("LLM_MODEL", "").strip() or None
    forced = env.get("LLM_PROVIDER", "").strip().lower()

    if forced:
        provider = build_provider(forced, env, model)
        return provider, f"LLM: {provider.name} / {provider.model} (forced by LLM_PROVIDER)"

    names = available_providers(env)
    if not names:
        keys = ", ".join(p.env_key for p in PROVIDERS)
        raise ConfigError(f"No LLM API key found. Fill in one of these in .env: {keys}.")
    provider = build_provider(names[0], env, model)
    spec = PROVIDERS_BY_NAME[names[0]]
    return provider, f"LLM: {provider.name} / {provider.model} ({spec.env_key} found)"
