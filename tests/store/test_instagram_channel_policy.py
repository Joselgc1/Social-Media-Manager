from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.agents.checkout import CHECKOUT_AGENT
from app.ai.agents.legacy import LEGACY_AGENT
from app.ai.agents.sales import SALES_AGENT
from app.ai.orchestrator import decide_orchestration
from app.ai.providers.base import LLMResponse
from app.ai.routing import decide_route
from app.ai.runner import AgentRunContext, AgentRunner
from app.ai.tools import catalog as tool_catalog
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool

INSTAGRAM_FORBIDDEN_TOOLS = {
    "create_order",
    "update_payment_status",
    "update_checkout_draft",
    "finalize_checkout",
    "cancel_checkout",
    "send_catalog_pdf",
}


def _settings() -> dict:
    return {
        "llm_provider": "openai",
        "llm_model": "gpt-5.6-luna",
        "llm_max_tokens": 500,
        "auto_fallback": False,
    }


def _run_context(channel: str) -> AgentRunContext:
    return AgentRunContext(
        customer={"id": "customer-1", "platform_id": "sender-1"},
        channel=channel,
        latest_user_message="Hola",
    )


def _tool_context(channel: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        customer={"id": "customer-1", "platform_id": "sender-1"},
        channel=channel,
    )


def test_instagram_purchase_intent_selects_sales_not_checkout():
    decision = decide_orchestration(
        mode="multi_agent",
        channel="instagram",
        message_text="Quiero comprar el pijama azul",
    )

    assert decision.route_decision.route == "sales"
    assert decision.route_decision.intent == "instagram_whatsapp_handoff"
    assert decision.agent.name == "sales"


def test_instagram_payment_proof_selects_sales_not_payment():
    decision = decide_route(
        "Te envío el comprobante de mi pedido",
        channel="instagram",
        payment_proof_attempt=True,
        vision_result={"analyzed": True, "amount": "28.00"},
    )

    assert decision.route == "sales"
    assert decision.intent == "instagram_whatsapp_handoff"


def test_instagram_sticky_checkout_session_cannot_select_checkout():
    decision = decide_route(
        "La talla es M",
        channel="instagram",
        session_state={"active_agent": "checkout", "workflow_stage": "checkout_collecting"},
    )

    assert decision.route == "sales"
    assert decision.intent == "instagram_whatsapp_handoff"
    assert decision.source == "channel_policy"


def test_instagram_pdf_catalog_request_selects_handoff_route():
    decision = decide_route("¿Me mandas el catálogo PDF?", channel="instagram")

    assert decision.route == "sales"
    assert decision.intent == "instagram_catalog_pdf_handoff"


def test_instagram_handoff_is_not_repeated_after_customer_declines():
    decision = decide_route(
        "No gracias",
        channel="instagram",
        session_state={"active_agent": "checkout", "workflow_stage": "checkout_collecting"},
    )

    assert decision.route == "sales"
    assert decision.intent == "conversation_close"


@pytest.mark.parametrize(
    "message",
    [
        "Gracias, lo quiero",
        "Gracias, quiero comprarlo",
        "Gracias, ¿cómo pago?",
        "No quiero Zelle, quiero pagar por Binance",
    ],
)
def test_instagram_transactional_intent_overrides_courtesy_or_decline_words(message):
    decision = decide_route(message, channel="instagram")

    assert decision.route == "sales"
    assert decision.intent == "instagram_whatsapp_handoff"


@pytest.mark.parametrize(
    ("message", "expected_route"),
    [
        ("Gracias, ¿qué tallas tienen?", "sales"),
        ("Gracias, ¿cuánto cuesta?", "sales"),
        ("Gracias, ¿tienes foto?", "sales"),
        ("Gracias, ¿cuál es el estado de mi pedido?", "support"),
    ],
)
def test_instagram_courtesy_does_not_override_informational_or_support_intent(message, expected_route):
    decision = decide_route(message, channel="instagram")

    assert decision.route == expected_route
    assert decision.intent != "conversation_close"
    assert decision.intent != "instagram_whatsapp_handoff"


@pytest.mark.parametrize(
    "message",
    ["No gracias", "Gracias", "Tranqui, gracias", "Ya no, gracias", "Déjalo"],
)
def test_instagram_standalone_decline_or_close_does_not_handoff(message):
    decision = decide_route(
        message,
        channel="instagram",
        session_state={"active_agent": "checkout", "workflow_stage": "checkout_collecting"},
    )

    assert decision.route == "sales"
    assert decision.intent == "conversation_close"


@pytest.mark.parametrize(
    "message",
    [
        "No quiero comprar, gracias",
        "Ya no quiero comprar",
        "No quiero ese, ¿qué otro tienes?",
        "No quiero esa, muéstrame otra",
        "No lo quiero",
        "No la quiero",
    ],
)
def test_instagram_negated_purchase_intent_does_not_handoff(message):
    decision = decide_route(message, channel="instagram")

    assert decision.route == "sales"
    assert decision.intent != "instagram_whatsapp_handoff"


@pytest.mark.parametrize(
    "message",
    [
        "Mándame el catálogo",
        "Pásame el catálogo",
        "Quiero ver el catálogo",
        "¿Me envías el catálogo completo?",
        "Quiero el PDF",
        "Mándame el inventario PDF",
    ],
)
def test_instagram_catalog_delivery_requests_select_handoff(message):
    decision = decide_route(message, channel="instagram")

    assert decision.route == "sales"
    assert decision.intent == "instagram_catalog_pdf_handoff"


@pytest.mark.parametrize(
    "message",
    [
        "¿Qué productos hay en el catálogo?",
        "Quiero ver qué productos hay en el catálogo",
        "Quiero ver qué tallas tienen en el catálogo",
        "¿Qué categorías tiene el catálogo?",
        "¿Tienen pijamas en el catálogo?",
        "¿Qué tallas aparecen en el catálogo?",
    ],
)
def test_instagram_informational_catalog_questions_stay_in_channel(message):
    decision = decide_route(message, channel="instagram")

    assert decision.route == "sales"
    assert decision.intent != "instagram_catalog_pdf_handoff"
    assert decision.intent != "instagram_whatsapp_handoff"


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("Quiero hablar con una persona real", "human_request"),
        ("Esto es una estafa, son unos ladrones", "hostile_message"),
        ("Quiero poner un reclamo", "complaint_or_refund"),
    ],
)
def test_instagram_support_and_escalation_routes_remain_available(message, intent):
    decision = decide_route(message, channel="instagram")

    assert decision.route == "support"
    assert decision.intent == intent


def test_whatsapp_transactional_routing_is_unchanged():
    purchase = decide_route("Quiero comprar el pijama azul", channel="whatsapp")
    proof = decide_route("Te envío el comprobante", channel="whatsapp", payment_proof_attempt=True)

    assert purchase.route == "checkout"
    assert proof.route == "payment"


@pytest.mark.asyncio
async def test_instagram_legacy_mode_filters_transactional_tool_schemas(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Te ayudo con información del producto.")),
        continue_after_tool=AsyncMock(),
    )
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda _name: provider)

    await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _run_context("instagram"))

    tool_names = {tool["name"] for tool in provider.chat.await_args.kwargs["tools"]}
    assert not tool_names & INSTAGRAM_FORBIDDEN_TOOLS
    assert "request_agent_handoff" not in tool_names
    assert {"check_inventory", "send_product_image", "send_whatsapp_handoff", "escalate_to_human"} <= tool_names


def test_whatsapp_handoff_tool_is_registered_only_for_sales_and_legacy_agents():
    assert "send_whatsapp_handoff" in LEGACY_AGENT.tool_names
    assert "send_whatsapp_handoff" in SALES_AGENT.tool_names
    assert "send_whatsapp_handoff" not in CHECKOUT_AGENT.tool_names


@pytest.mark.asyncio
async def test_whatsapp_legacy_tool_schemas_are_unchanged(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Claro.")),
        continue_after_tool=AsyncMock(),
    )
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda _name: provider)

    await AgentRunner().run(LEGACY_AGENT, "prompt", [], _settings(), _run_context("whatsapp"))

    expected_names = [name for name in LEGACY_AGENT.tool_names if name != "send_whatsapp_handoff"]
    assert [tool["name"] for tool in provider.chat.await_args.kwargs["tools"]] == expected_names


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(INSTAGRAM_FORBIDDEN_TOOLS))
async def test_executor_rejects_forbidden_instagram_tools(tool_name):
    result = await execute_tool(tool_name, {}, _tool_context("instagram"))

    assert result == {
        "status": "error",
        "message": f"Tool not allowed on channel 'instagram': {tool_name}",
    }


@pytest.mark.asyncio
async def test_executor_rejects_checkout_handoff_on_instagram():
    result = await execute_tool(
        "request_agent_handoff",
        {"target_agent": "checkout", "intent": "purchase_intent"},
        _tool_context("instagram"),
    )

    assert result["status"] == "error"
    assert "not allowed" in result["message"]


@pytest.mark.asyncio
async def test_instagram_keeps_inventory_and_product_image_tools(monkeypatch):
    catalog = [{
        "sku": "PJ-001-M",
        "parent_sku": "PJ-001",
        "product_name": "Pijama azul",
        "category": "Pijamas",
        "description": "Pijama azul",
        "size": "M",
        "sizes": "M",
        "price_usd": 28,
        "stock": 2,
        "image_url": "https://example.com/pijama.jpg",
    }]
    monkeypatch.setattr(tool_catalog, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr(tool_catalog, "ensure_fresh_catalog", AsyncMock(return_value=catalog))
    context = _tool_context("instagram")

    inventory = await execute_tool("check_inventory", {"product_query": "Pijama azul"}, context)
    image = await execute_tool("send_product_image", {"product_query": "Pijama azul"}, context)

    assert inventory["found"] is True
    assert inventory["products"][0]["in_stock"] is True
    assert image["type"] == "product_image"
    assert image["image_url"] == "https://example.com/pijama.jpg"
