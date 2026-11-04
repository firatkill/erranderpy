"""Contract tests: every provider must behave the same through our interface.

These call the real APIs, so they only run with `make test-live`, and each
provider is skipped when its key is missing from .env. A full run costs well
under one cent.
"""

import pytest
from dotenv import load_dotenv

from erranderpy.core.llm import Message, ToolSpec, available_providers, build_provider
from erranderpy.core.llm.defaults import PROVIDERS

load_dotenv()
pytestmark = pytest.mark.live

ADD = ToolSpec(
    "add_items",
    "Add items to the shopping list.",
    {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        "required": ["items"],
    },
)
SYSTEM = "You manage a shopping list. When the user mentions something to buy, call add_items."


@pytest.fixture(params=[p.name for p in PROVIDERS])
def provider(request):
    if request.param not in available_providers():
        pytest.skip(f"no API key for {request.param}")
    return build_provider(request.param)


def test_plain_text_answer(provider):
    result = provider.complete("Reply with one short word.", [Message.user("Merhaba!")])
    assert result.text.strip()
    assert result.tokens_in > 0 and result.tokens_out > 0
    assert result.cost_usd is not None


def test_tool_call(provider):
    result = provider.complete(SYSTEM, [Message.user("zeytinyağı almam lazım")], tools=[ADD])
    assert len(result.tool_calls) >= 1
    call = result.tool_calls[0]
    assert call.name == "add_items"
    assert any("zeytin" in item.lower() for item in call.arguments["items"])


def test_tool_result_round_trip(provider):
    history = [Message.user("zeytinyağı almam lazım")]
    first = provider.complete(SYSTEM, history, tools=[ADD])
    assert first.tool_calls
    history.append(first.message)
    for call in first.tool_calls:
        history.append(Message.tool_result(call.id, "Added. The list now has 1 item."))
    second = provider.complete(SYSTEM, history, tools=[ADD])
    assert second.text.strip()  # the model confirms in plain text
