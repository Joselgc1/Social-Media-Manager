from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.agents.base import AgentDefinition
from app.ai.agents.legacy import LEGACY_AGENT
from app.ai.providers.base import LLMResponse
from app.ai.registry import AgentRegistry, get_agent_registry
from app.ai.runner import AgentRunContext, AgentRunner


def _context() -> AgentRunContext:
    return AgentRunContext(
        customer={"id": "customer-1", "display_name": "Luisana Perez"},
        channel="whatsapp",
        payment_methods=[],
        latest_user_message="Hola",
    )


def _settings(**overrides) -> dict:
    settings = {
        "llm_provider": "openai",
        "llm_model": "gpt-5.6-luna",
        "llm_temperature": 0.2,
        "llm_max_tokens": 500,
        "auto_fallback": True,
        "fallback_provider": "anthropic",
        "fallback_model": "claude-haiku-4-5",
    }
    settings.update(overrides)
    return settings


def _provider(chat_response: LLMResponse | None = None):
    return SimpleNamespace(
        chat=AsyncMock(return_value=chat_response or LLMResponse(text="Hola", usage={"input_tokens": 1, "output_tokens": 2})),
        continue_after_tool=AsyncMock(return_value=LLMResponse(text="Listo", usage={"input_tokens": 3, "output_tokens": 4})),
    )


def _tool_call(name: str, arguments: dict | None = None, tool_id: str = "tool-1") -> dict:
    return {"id": tool_id, "name": name, "arguments": arguments or {}}


def test_agent_registration():
    registry = get_agent_registry()
    agent = registry.get("legacy")
    sales = registry.get("sales")
    checkout = registry.get("checkout")
    support = registry.get("support")

    assert agent.name == "legacy"
    assert agent.prompt_name == "legacy"
    assert "create_order" in agent.tool_names
    assert sales.prompt_name == "sales"
    assert "create_order" not in sales.tool_names
    assert checkout.prompt_name == "checkout"
    assert "finalize_checkout" in checkout.tool_names
    assert support.prompt_name == "support"
    assert "get_customer_order_status" in support.tool_names
    assert "create_order" not in support.tool_names


def test_unknown_agent_failure():
    registry = AgentRegistry()

    with pytest.raises(KeyError, match="Unknown agent: missing"):
        registry.get("missing")


@pytest.mark.asyncio
async def test_agent_tool_schema_selection(monkeypatch):
    provider = _provider()
    agent = AgentDefinition("limited", "legacy", ("check_inventory",), max_tool_rounds=1)
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)

    await AgentRunner().run(agent, "prompt", [], _settings(), _context())

    tools = provider.chat.await_args.kwargs["tools"]
    assert [tool["name"] for tool in tools] == ["check_inventory"]


@pytest.mark.asyncio
async def test_unauthorized_model_generated_tool_call(monkeypatch):
    provider = _provider(LLMResponse(tool_calls=[_tool_call("send_catalog_pdf", {"caption": "Catálogo"})]))
    provider.continue_after_tool.return_value = LLMResponse(text="No puedo hacer eso.")
    execute_tool = AsyncMock(return_value={"status": "ok"})
    agent = AgentDefinition("limited", "legacy", ("check_inventory",), max_tool_rounds=2)
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute_tool)

    result = await AgentRunner().run(agent, "prompt", [], _settings(), _context())

    assert result.catalog_pdf is None
    assert "not authorized" in result.tool_log[0]["result"]["message"]
    execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_response(monkeypatch):
    provider = _provider(LLMResponse(text="Hola bella", usage={"input_tokens": 5, "output_tokens": 6}))
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert result.text == "Hola bella"
    assert result.tool_log == []
    assert result.provider == "openai"
    assert result.model == "gpt-5.6-luna"
    assert result.usage == {"input_tokens": 5, "output_tokens": 6}


@pytest.mark.asyncio
async def test_multi_round_tool_execution(monkeypatch):
    provider = _provider(LLMResponse(tool_calls=[_tool_call("check_inventory", {"product_query": "pijama"})], usage={"input_tokens": 1, "output_tokens": 1}))
    provider.continue_after_tool.side_effect = [
        LLMResponse(tool_calls=[_tool_call("tag_customer", {"tags": ["interested:pajamas"]}, "tool-2")], usage={"input_tokens": 2, "output_tokens": 2}),
        LLMResponse(text="Sí tenemos pijamas.", usage={"input_tokens": 3, "output_tokens": 3}),
    ]
    execute_tool = AsyncMock(side_effect=[{"found": True}, {"status": "ok"}])
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute_tool)

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert result.text == "Sí tenemos pijamas."
    assert [entry["name"] for entry in result.tool_log] == ["check_inventory", "tag_customer"]
    assert result.usage == {"input_tokens": 6, "output_tokens": 6}
    second_history = provider.continue_after_tool.await_args_list[1].kwargs["tool_history"]
    assert [entry["name"] for entry in second_history] == ["check_inventory", "tag_customer"]
    assert second_history[0]["arguments"] == {"product_query": "pijama"}
    assert '"found": true' in second_history[0]["result"]


@pytest.mark.asyncio
async def test_maximum_tool_rounds(monkeypatch):
    agent = AgentDefinition("one-round", "legacy", ("check_inventory",), max_tool_rounds=1)
    provider = _provider(LLMResponse(tool_calls=[_tool_call("check_inventory", {"product_query": "pijama"})]))
    provider.continue_after_tool.return_value = LLMResponse(tool_calls=[_tool_call("check_inventory", {"product_query": "set"})])
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", AsyncMock(return_value={"found": True}))

    result = await AgentRunner().run(agent, "prompt", [], _settings(), _context())

    assert len(result.tool_log) == 1
    assert provider.continue_after_tool.await_args.kwargs["tools"] is None
    assert result.text == "Lo siento, no pude generar una respuesta. ¿Puedes repetir tu pregunta?"


@pytest.mark.asyncio
async def test_provider_fallback(monkeypatch):
    primary = _provider()
    primary.chat.side_effect = RuntimeError("boom")
    fallback = _provider(LLMResponse(text="Fallback", usage={"input_tokens": 7, "output_tokens": 8}))
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai", "anthropic"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: primary if name == "openai" else fallback)

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert result.text == "Fallback"
    assert result.provider == "anthropic"
    assert result.model == "claude-haiku-4-5"
    assert result.was_fallback is True
    assert result.usage == {"input_tokens": 7, "output_tokens": 8}


@pytest.mark.asyncio
async def test_provider_fallback_continues_after_committed_tool_without_reexecution(monkeypatch):
    primary = _provider(LLMResponse(tool_calls=[_tool_call("create_order", {"items": []})]))
    primary.continue_after_tool.side_effect = RuntimeError("continuation failed")
    fallback = _provider()
    fallback.continue_after_tool.return_value = LLMResponse(text="Tu pedido quedó registrado.")
    execute_tool = AsyncMock(return_value={"order_id": "order-1", "status": "pending"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai", "anthropic"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: primary if name == "openai" else fallback)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute_tool)

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert result.text == "Tu pedido quedó registrado."
    assert result.provider == "anthropic"
    assert result.was_fallback is True
    execute_tool.assert_awaited_once()
    fallback.chat.assert_not_awaited()
    assert fallback.continue_after_tool.await_args.kwargs["tool_history"][0]["name"] == "create_order"


@pytest.mark.asyncio
async def test_committed_tool_has_deterministic_reply_when_all_continuations_fail(monkeypatch):
    primary = _provider(LLMResponse(tool_calls=[_tool_call("escalate_to_human", {"reason": "Ayuda"})]))
    primary.continue_after_tool.side_effect = RuntimeError("continuation failed")
    fallback = _provider()
    fallback.continue_after_tool.side_effect = RuntimeError("fallback failed")
    execute_tool = AsyncMock(return_value={"status": "escalated"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai", "anthropic"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: primary if name == "openai" else fallback)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute_tool)

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert "persona del equipo" in result.text
    assert result.escalated is True
    assert result.was_fallback is True
    execute_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_payload_preservation(monkeypatch):
    provider = _provider(LLMResponse(tool_calls=[_tool_call("send_interactive_buttons", {"body_text": "¿MRW o Zoom?", "buttons": ["MRW", "Zoom"]})]))
    provider.continue_after_tool.return_value = LLMResponse(text=None)
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", AsyncMock(return_value={"type": "interactive_buttons", "body_text": "¿MRW o Zoom?", "buttons": ["MRW", "Zoom"]}))

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert result.text == "¿MRW o Zoom?"
    assert result.interactive == {"type": "interactive_buttons", "body_text": "¿MRW o Zoom?", "buttons": ["MRW", "Zoom"]}


@pytest.mark.asyncio
async def test_escalation_tool_sets_result_flags(monkeypatch):
    provider = _provider(LLMResponse(tool_calls=[_tool_call("escalate_to_human", {"reason": "Cliente pide humano"})]))
    provider.continue_after_tool.return_value = LLMResponse(text="Te paso con una persona del equipo.")
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", AsyncMock(return_value={"status": "escalated"}))

    result = await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _context())

    assert result.requested_handoff is True
    assert result.escalated is True
