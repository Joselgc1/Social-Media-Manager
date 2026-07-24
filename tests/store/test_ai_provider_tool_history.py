from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.providers.base import LLMResponse
from app.ai.providers.openai_provider import OpenAIProvider


def _history():
    return [
        {"id": "tool-1", "name": "check_inventory", "arguments": {"product_query": "pijama"}, "result": "available"},
        {"id": "tool-2", "name": "tag_customer", "arguments": {"tags": ["interested:pajamas"]}, "result": "tagged"},
    ]


@pytest.mark.asyncio
async def test_openai_continuation_translates_full_tool_history(monkeypatch):
    create = AsyncMock(return_value=object())
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(provider, "_normalize", lambda response: LLMResponse(text="ok"))

    await provider.continue_after_tool(
        model="model",
        system_prompt="prompt",
        messages=[{"role": "user", "content": "hola"}],
        tool_call_id="tool-2",
        tool_name="tag_customer",
        tool_result="tagged",
        tool_history=_history(),
    )

    messages = create.await_args.kwargs["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert '"product_query": "pijama"' in messages[2]["tool_calls"][0]["function"]["arguments"]
    assert messages[5]["content"] == "tagged"


@pytest.mark.asyncio
async def test_anthropic_continuation_translates_full_tool_history(monkeypatch):
    create = AsyncMock(return_value=object())
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(provider, "_normalize", lambda response: LLMResponse(text="ok"))

    await provider.continue_after_tool(
        model="model",
        system_prompt="prompt",
        messages=[{"role": "user", "content": "hola"}],
        tool_call_id="tool-2",
        tool_name="tag_customer",
        tool_result="tagged",
        tool_history=_history(),
    )

    messages = create.await_args.kwargs["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"][0]["input"] == {"product_query": "pijama"}
    assert messages[4]["content"][0]["content"] == "tagged"
