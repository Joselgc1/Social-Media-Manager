from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.agents.sales import SALES_AGENT
from app.ai.orchestrator import decide_orchestration
from app.ai.prompts import PromptContext, build_agent_prompt
from app.ai.providers.base import LLMResponse
from app.ai.routing import decide_route
from app.ai.runner import AgentRunContext, AgentRunner
from app.ai.tools import catalog as tool_catalog
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool


def _provider(tool_name: str, arguments: dict | None = None, final_text: str = "Listo"):
    return SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(tool_calls=[{"id": "tool-1", "name": tool_name, "arguments": arguments or {}}])),
        continue_after_tool=AsyncMock(return_value=LLMResponse(text=final_text)),
    )


def _run_context(channel: str = "whatsapp") -> AgentRunContext:
    return AgentRunContext(
        customer={"id": "customer-1", "display_name": "Luisana Perez"},
        channel=channel,
        payment_methods=[],
        latest_user_message="Hola",
    )


def _settings() -> dict:
    return {
        "llm_provider": "openai",
        "llm_model": "gpt-5.6-luna",
        "llm_temperature": 0.2,
        "llm_max_tokens": 500,
        "auto_fallback": False,
    }


def test_sales_routes_for_core_intents():
    assert decide_route("Hola").intent == "greeting"
    assert decide_route("¿Qué tienen disponible?").intent == "catalog_request"
    assert decide_route("Busco pijama en talla M").intent == "size_question"
    assert decide_route("¿Me recomiendas un set?").intent == "recommendation_request"
    assert decide_route("¿Tienes foto del pijama azul?").intent == "product_photo_request"


def test_sales_prompt_is_specialized_and_uses_catalog_context():
    prompt = build_agent_prompt(
        "sales",
        PromptContext(
            catalog_markdown="| Pijama satén azul |",
            store_name="Tienda Rosa",
            payment_methods=[{"name": "Zelle", "information": "Correo: pagos@example.com"}],
        ),
    )

    assert "# Sales Agent scope" in prompt
    assert "Pijama satén azul" in prompt
    assert "request_agent_handoff" in prompt
    assert "cannot create orders" in prompt


def test_sales_tool_allowlist_is_smaller_than_legacy_checkout_tools():
    assert set(SALES_AGENT.tool_names) == {
        "check_inventory",
        "tag_customer",
        "send_catalog_pdf",
        "send_product_image",
        "send_interactive_buttons",
        "request_agent_handoff",
    }
    assert "create_order" not in SALES_AGENT.tool_names
    assert "update_payment_status" not in SALES_AGENT.tool_names
    assert "escalate_to_human" not in SALES_AGENT.tool_names


@pytest.mark.asyncio
async def test_sales_agent_cannot_create_order(monkeypatch):
    provider = _provider("create_order", {"items": []})
    execute = AsyncMock(return_value={"order_id": "order-1"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute)

    result = await AgentRunner().run(SALES_AGENT, "prompt", [], _settings(), _run_context())

    assert "not authorized" in result.tool_log[0]["result"]["message"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sales_agent_cannot_update_payment(monkeypatch):
    provider = _provider("update_payment_status", {"confirmation_note": "x"})
    execute = AsyncMock(return_value={"payment_status": "proof_received"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute)

    result = await AgentRunner().run(SALES_AGENT, "prompt", [], _settings(), _run_context())

    assert "not authorized" in result.tool_log[0]["result"]["message"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sales_product_inquiry_and_out_of_stock(monkeypatch):
    monkeypatch.setattr(tool_catalog, "get_cached_catalog", lambda: [
        {
            "sku": "PJ-001-M",
            "parent_sku": "PJ-001",
            "product_name": "Pijama satén azul",
            "category": "Pijamas",
            "description": "Pijama azul",
            "size": "M",
            "sizes": "M",
            "price_usd": 28,
            "stock": 0,
        }
    ])
    monkeypatch.setattr(tool_catalog, "ensure_fresh_catalog", AsyncMock())

    result = await execute_tool("check_inventory", {"product_query": "pijama satén", "size": "M"}, ToolExecutionContext(customer={"id": "customer-1"}, channel="whatsapp"))

    assert result["found"] is True
    assert result["products"][0]["in_stock"] is False
    assert "stock" not in result["products"][0]


@pytest.mark.asyncio
async def test_sales_handoff_returns_structured_internal_data(monkeypatch):
    provider = _provider("request_agent_handoff", {"target_agent": "checkout", "intent": "purchase_intent"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)

    result = await AgentRunner().run(SALES_AGENT, "prompt", [], _settings(), _run_context())

    assert result.requested_handoff is True
    assert result.handoff_target == "checkout"
    assert result.tool_log[0]["result"]["type"] == "agent_handoff"


@pytest.mark.asyncio
async def test_sales_whatsapp_buttons(monkeypatch):
    provider = _provider("send_interactive_buttons", {"body_text": "¿Qué buscas?", "buttons": ["Pijamas", "Panties"]}, final_text="")
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)

    result = await AgentRunner().run(SALES_AGENT, "prompt", [], _settings(), _run_context("whatsapp"))

    assert result.interactive == {"type": "interactive_buttons", "body_text": "¿Qué buscas?", "buttons": ["Pijamas", "Panties"]}


@pytest.mark.asyncio
async def test_sales_instagram_returns_quick_reply_payload(monkeypatch):
    provider = _provider("send_interactive_buttons", {"body_text": "¿Qué buscas?", "buttons": ["Pijamas", "Panties"]}, final_text="Te doy opciones por aquí.")
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)

    result = await AgentRunner().run(SALES_AGENT, "prompt", [], _settings(), _run_context("instagram"))

    assert result.interactive == {
        "type": "interactive_buttons",
        "body_text": "¿Qué buscas?",
        "buttons": ["Pijamas", "Panties"],
    }
    assert result.text == "Te doy opciones por aquí."


def test_shadow_mode_routes_to_sales_but_executes_legacy():
    decision = decide_orchestration(mode="shadow", message_text="¿Tienes pijamas?")

    assert decision.route_decision.route == "sales"
    assert decision.route_agent.name == "sales"
    assert decision.agent.name == "legacy"
