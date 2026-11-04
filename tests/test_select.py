import pytest

from erranderpy.core.llm import ConfigError, available_providers, select_provider


def test_no_key_gives_a_helpful_error():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        select_provider({})


def test_blank_keys_count_as_missing():
    assert available_providers({"OPENAI_API_KEY": "   "}) == []


def test_single_key_selects_that_provider():
    provider, reason = select_provider({"DEEPSEEK_API_KEY": "sk-test"})
    assert provider.name == "deepseek"
    assert provider.model == "deepseek-flash"
    assert "DEEPSEEK_API_KEY" in reason


def test_priority_order_when_several_keys_are_set():
    env = {"OPENAI_API_KEY": "a", "DEEPSEEK_API_KEY": "b", "GEMINI_API_KEY": "c"}
    provider, _ = select_provider(env)
    assert provider.name == "gemini"
    assert available_providers(env) == ["gemini", "deepseek", "openai"]


def test_anthropic_wins_over_everything():
    env = {"OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b"}
    provider, _ = select_provider(env)
    assert provider.name == "anthropic"


def test_llm_provider_overrides_priority():
    env = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b", "LLM_PROVIDER": "OpenAI"}
    provider, reason = select_provider(env)
    assert provider.name == "openai"
    assert "forced" in reason


def test_forced_provider_without_key_fails():
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        select_provider({"OPENAI_API_KEY": "a", "LLM_PROVIDER": "deepseek"})


def test_unknown_forced_provider_fails():
    with pytest.raises(ConfigError, match="Unknown"):
        select_provider({"OPENAI_API_KEY": "a", "LLM_PROVIDER": "mistral"})


def test_llm_model_overrides_default_model():
    provider, _ = select_provider({"OPENAI_API_KEY": "a", "LLM_MODEL": "gpt-5-nano"})
    assert provider.model == "gpt-5-nano"
