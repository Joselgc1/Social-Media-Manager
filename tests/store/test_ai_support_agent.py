from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.agents.support import SUPPORT_AGENT
from app.ai.prompts import PromptContext, build_agent_prompt
from app.ai.providers.base import LLMResponse
from app.ai.runner import AgentRunContext, AgentRunner


def _settings() -> dict:
    return {
        "llm_provider": "openai",
        "llm_model": "gpt-5.4-nano",
        "llm_temperature": 0.2,
        "llm_max_tokens": 500,
        "auto_fallback": False,
    }


def _context() -> AgentRunContext:
    return AgentRunContext(customer={"id": "customer-1", "display_name": "Luisana Perez"}, channel="whatsapp")


def _provider(tool_name: str, arguments: dict | None = None, final_text: str = "Te ayudo con eso."):
    return SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(tool_calls=[{"id": "tool-1", "name": tool_name, "arguments": arguments or {}}])),
        continue_after_tool=AsyncMock(return_value=LLMResponse(text=final_text)),
    )


def test_support_prompt_is_specialized():
    prompt = build_agent_prompt(
        "support",
        PromptContext(
            catalog_markdown="| Pijama satén azul |",
            store_name="Tienda Rosa",
            payment_methods=[{"name": "Zelle", "information": "Correo: pagos@example.com"}],
        ),
    )

    assert "# Support Agent scope" in prompt
    assert "get_customer_order_status" in prompt
    assert "Do not create orders" in prompt
    assert "Pijama satén azul" in prompt
    assert "Prices do NOT include shipping" in prompt


def test_support_tool_allowlist_is_read_only_except_escalation():
    assert set(SUPPORT_AGENT.tool_names) == {
        "get_customer_profile",
        "get_customer_order_status",
        "escalate_to_human",
    }
    assert "create_order" not in SUPPORT_AGENT.tool_names
    assert "update_payment_status" not in SUPPORT_AGENT.tool_names
    assert "finalize_checkout" not in SUPPORT_AGENT.tool_names


@pytest.mark.asyncio
async def test_support_agent_cannot_create_order(monkeypatch):
    provider = _provider("create_order", {"items": []})
    execute = AsyncMock(return_value={"order_id": "order-1"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute)

    result = await AgentRunner().run(SUPPORT_AGENT, "prompt", [], _settings(), _context())

    assert "not authorized" in result.tool_log[0]["result"]["message"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_support_agent_can_escalate_to_human(monkeypatch):
    provider = _provider(
        "escalate_to_human",
        {"reason": "Cliente pide humano"},
        final_text="Te paso con una persona del equipo.",
    )
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", AsyncMock(return_value={"status": "escalated"}))

    result = await AgentRunner().run(SUPPORT_AGENT, "prompt", [], _settings(), _context())

    assert result.escalated is True
    assert result.requested_handoff is True
