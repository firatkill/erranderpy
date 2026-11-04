import httpx2 as httpx
import openai
import pytest

from erranderpy.core.llm import LLMError, Message, ToolCall, ToolSpec
from erranderpy.core.llm.defaults import PROVIDERS_BY_NAME
from erranderpy.core.llm.openai_compat import OpenAICompatProvider
from tests.fakes import FakeOpenAIClient, openai_message

TOOL = ToolSpec("add_items", "Add items.", {"type": "object", "properties": {}, "required": []})


def make(name: str, client: FakeOpenAIClient) -> OpenAICompatProvider:
    return OpenAICompatProvider(PROVIDERS_BY_NAME[name], "sk-test", client=client)


def test_system_prompt_becomes_first_message_and_tools_are_wrapped():
    client = FakeOpenAIClient(openai_message("ok"))
    make("deepseek", client).complete("be nice", [Message.user("selam")], tools=[TOOL])
    req = client.requests[0]
    assert req["messages"][0] == {"role": "system", "content": "be nice"}
    assert req["messages"][1] == {"role": "user", "content": "selam"}
    assert req["tools"][0]["type"] == "function"
    assert req["tools"][0]["function"]["name"] == "add_items"
    assert req["tool_choice"] == "auto"
    assert "temperature" not in req


def test_provider_quirks_are_applied():
    client = FakeOpenAIClient(openai_message("ok"))
    make("deepseek", client).complete("s", [Message.user("u")])
    assert client.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert client.requests[0]["max_tokens"] == 2048

    client = FakeOpenAIClient(openai_message("ok"))
    make("openai", client).complete("s", [Message.user("u")])
    req = client.requests[0]
    assert req["reasoning_effort"] == "none"
    assert req["max_completion_tokens"] == 2048
    assert "max_tokens" not in req


def test_no_tools_means_no_tool_choice():
    client = FakeOpenAIClient(openai_message("ok"))
    make("gemini", client).complete("s", [Message.user("u")])
    assert "tools" not in client.requests[0]
    assert "tool_choice" not in client.requests[0]


def test_tool_call_is_parsed_and_cost_is_computed():
    msg = openai_message(tool_calls=[("call_1", "add_items", '{"items": [{"name": "zeytinyağı"}]}')])
    client = FakeOpenAIClient(msg, prompt_tokens=1000, completion_tokens=100)
    result = make("deepseek", client).complete("s", [Message.user("zeytinyağı al")], tools=[TOOL])
    assert result.tool_calls == [ToolCall("call_1", "add_items", {"items": [{"name": "zeytinyağı"}]})]
    assert result.tokens_in == 1000 and result.tokens_out == 100
    # deepseek-flash at peak: 0.30 in / 1.20 out per MTok
    assert result.cost_usd == pytest.approx((1000 * 0.30 + 100 * 1.20) / 1_000_000)


def test_broken_tool_json_is_invalid_output():
    msg = openai_message(tool_calls=[("c", "add_items", '{"items": [')])
    with pytest.raises(LLMError) as err:
        make("openai", FakeOpenAIClient(msg)).complete("s", [Message.user("u")], tools=[TOOL])
    assert err.value.kind == "invalid_output"


def test_tool_loop_history_uses_openai_format():
    client = FakeOpenAIClient(openai_message("Ekledim!"))
    history = [
        Message.user("zeytinyağı al"),
        Message(role="assistant", tool_calls=[ToolCall("c1", "add_items", {"items": [{"name": "zeytinyağı"}]})]),
        Message.tool_result("c1", "added 1 item"),
    ]
    make("openai", client).complete("s", history, tools=[TOOL])
    msgs = client.requests[0]["messages"]
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == '{"items": [{"name": "zeytinyağı"}]}'
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "added 1 item"}


def _status_error(cls, status: int):
    request = httpx.Request("POST", "https://example.test")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (_status_error(openai.AuthenticationError, 401), "auth"),
        (_status_error(openai.RateLimitError, 429), "rate_limit"),
        (_status_error(openai.BadRequestError, 400), "bad_request"),
        (_status_error(openai.InternalServerError, 500), "server"),
        (openai.APITimeoutError(request=httpx.Request("POST", "https://example.test")), "timeout"),
    ],
)
def test_sdk_errors_are_mapped(exc, kind):
    client = FakeOpenAIClient(openai_message("x"), error=exc)
    with pytest.raises(LLMError) as err:
        make("openai", client).complete("s", [Message.user("u")])
    assert err.value.kind == kind
    assert err.value.provider == "openai"
