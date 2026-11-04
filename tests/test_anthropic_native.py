import httpx2 as httpx
import anthropic
import pytest

from erranderpy.core.llm import LLMError, Message, ToolCall, ToolSpec
from erranderpy.core.llm.anthropic_native import AnthropicProvider
from erranderpy.core.llm.defaults import PROVIDERS_BY_NAME
from tests.fakes import FakeAnthropicClient, FakeBlock

TOOL = ToolSpec("add_items", "Add items.", {"type": "object", "properties": {}, "required": []})


def make(client: FakeAnthropicClient) -> AnthropicProvider:
    return AnthropicProvider(PROVIDERS_BY_NAME["anthropic"], "sk-ant-test", client=client)


def test_request_uses_anthropic_shapes():
    client = FakeAnthropicClient([FakeBlock(type="text", text="ok")])
    make(client).complete("be nice", [Message.user("selam")], tools=[TOOL])
    req = client.requests[0]
    assert req["system"] == "be nice"
    assert req["messages"] == [{"role": "user", "content": "selam"}]
    assert req["tools"][0] == {"name": "add_items", "description": "Add items.", "input_schema": TOOL.parameters}
    assert req["tool_choice"] == {"type": "auto"}
    assert req["output_config"] == {"effort": "low"}
    assert "temperature" not in req  # Haiku 5.5 rejects it


def test_thinking_blocks_are_skipped_and_tool_use_is_parsed():
    content = [
        FakeBlock(type="thinking", thinking="", signature="sig"),
        FakeBlock(type="tool_use", id="tu_1", name="add_items", input={"items": [{"name": "süt"}]}),
    ]
    result = make(FakeAnthropicClient(content, input_tokens=1000, output_tokens=200)).complete(
        "s", [Message.user("süt al")], tools=[TOOL]
    )
    assert result.text == ""
    assert result.tool_calls == [ToolCall("tu_1", "add_items", {"items": [{"name": "süt"}]})]
    assert result.message.raw_provider == "anthropic"
    assert result.message.raw[0]["type"] == "thinking"
    assert result.cost_usd == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1_000_000)


def test_tool_results_are_grouped_into_one_user_message():
    client = FakeAnthropicClient([FakeBlock(type="text", text="done")])
    history = [
        Message.user("süt ve ekmek al"),
        Message(
            role="assistant",
            tool_calls=[ToolCall("a", "add_items", {"n": 1}), ToolCall("b", "add_items", {"n": 2})],
        ),
        Message.tool_result("a", "ok a"),
        Message.tool_result("b", "ok b"),
    ]
    make(client).complete("s", history, tools=[TOOL])
    msgs = client.requests[0]["messages"]
    assert len(msgs) == 3
    assert [b["type"] for b in msgs[1]["content"]] == ["tool_use", "tool_use"]
    assert msgs[2]["role"] == "user"
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["a", "b"]


def test_own_raw_blocks_are_replayed_unchanged():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "x", "name": "t", "input": {}}]
    client = FakeAnthropicClient([FakeBlock(type="text", text="ok")])
    history = [
        Message.user("u"),
        Message(role="assistant", tool_calls=[ToolCall("x", "t", {})], raw=raw, raw_provider="anthropic"),
        Message.tool_result("x", "r"),
    ]
    make(client).complete("s", history)
    assert client.requests[0]["messages"][1]["content"] == raw


def test_raw_blocks_from_another_provider_are_ignored():
    client = FakeAnthropicClient([FakeBlock(type="text", text="ok")])
    history = [
        Message.user("u"),
        Message(role="assistant", text="hi", raw={"something": "else"}, raw_provider="openai"),
        Message.user("again"),
    ]
    make(client).complete("s", history)
    assert client.requests[0]["messages"][1]["content"] == [{"type": "text", "text": "hi"}]


def test_rate_limit_is_mapped():
    request = httpx.Request("POST", "https://example.test")
    exc = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
    with pytest.raises(LLMError) as err:
        make(FakeAnthropicClient([], error=exc)).complete("s", [Message.user("u")])
    assert err.value.kind == "rate_limit"
