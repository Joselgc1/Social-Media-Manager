from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.ai import engine, prompts
from app.ai import runner as agent_runner
from app.ai.functions import TOOLS
from app.ai.providers.base import LLMResponse
from app.ai.tools import catalog as tool_catalog
from app.ai.tools import messaging as tool_messaging
from app.ai.tools import orders as tool_orders

EXPECTED_TOOL_ORDER = [
    "check_inventory",
    "tag_customer",
    "create_order",
    "update_payment_status",
    "escalate_to_human",
    "send_catalog_pdf",
    "send_product_image",
    "send_interactive_buttons",
    "request_agent_handoff",
    "update_checkout_draft",
    "finalize_checkout",
    "cancel_checkout",
    "get_customer_profile",
    "get_customer_order_status",
]

ACTIVE_RESPONSE_KEYS = {
    "text",
    "interactive",
    "catalog_pdf",
    "product_image",
    "customer_id",
    "escalated",
}


def _sample_settings(**overrides) -> dict:
    settings = {
        "ai_enabled": True,
        "llm_provider": "openai",
        "llm_model": "gpt-5.4-nano",
        "llm_temperature": 0.2,
        "llm_max_tokens": 500,
        "max_conversation_history": 20,
        "auto_fallback": False,
        "store_name": "Tienda Rosa",
        "payment_methods": [
            {
                "id": "pm-zelle",
                "name": "Zelle",
                "information": "Correo: pagos@example.com",
            }
        ],
        "accepted_exchange_rate": "40,25 Bs/USD",
        "exchange_rate_reference": "manual",
        "manual_exchange_rate": "40.25",
        "store_phone_number": "+58 412-1234567",
        "order_discount_percent": 10,
        "order_discount_threshold_usd": 350,
        "shipping_policy": {
            "currency": "USD",
            "home_delivery_cities": [],
            "home_delivery_zones": [],
            "courier_destination_rates": [
                {"city": "Caracas", "aliases": [], "mrw_fee_usd": 6.0, "zoom_fee_usd": 7.0},
            ],
        },
    }
    settings.update(overrides)
    return settings


def _sample_customer(**overrides) -> dict:
    customer = {
        "id": "customer-1",
        "channel": "whatsapp",
        "platform_id": "584121234567",
        "display_name": "Luisana Perez",
        "conversation_state": "active",
        "tags": [],
        "total_orders": 0,
        "total_spent": 0,
    }
    customer.update(overrides)
    return customer


def _sample_catalog() -> list[dict]:
    return [
        {
            "sku": "PJ-001-S",
            "parent_sku": "PJ-001",
            "product_name": "Pijama satén azul",
            "category": "Pijamas",
            "description": "Pijama suave azul",
            "size": "S",
            "sizes": "S",
            "price_usd": 28,
            "stock": 4,
            "image_url": "https://example.com/pijama.jpg",
        }
    ]


def _second_catalog_product(**overrides) -> dict:
    product = {
        "sku": "BODY-002-M",
        "parent_sku": "BODY-002",
        "product_name": "Body negro",
        "category": "Bodies",
        "description": "Body negro clásico",
        "size": "M",
        "sizes": "M",
        "price_usd": 25,
        "stock": 3,
        "image_url": "https://example.com/body-negro.jpg",
    }
    product.update(overrides)
    return product


def _pajama_catalog_product(**overrides) -> dict:
    product = {
        "sku": "PJ-002-S",
        "parent_sku": "PJ-002",
        "product_name": "Pijama amarilla",
        "category": "Pijamas",
        "description": "Pijama suave amarilla",
        "size": "S",
        "sizes": "S",
        "price_usd": 27,
        "stock": 3,
        "image_url": "https://example.com/pijama-amarilla.jpg",
    }
    product.update(overrides)
    return product


def _tool_call(name: str, arguments: dict, tool_id: str = "tool-1") -> dict:
    return {"id": tool_id, "name": name, "arguments": arguments}


def _assert_required_keys_declared(schema: dict) -> None:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    assert isinstance(required, list)
    assert isinstance(properties, dict)

    for key in required:
        assert key in properties

    for property_schema in properties.values():
        if not isinstance(property_schema, dict):
            continue
        if property_schema.get("type") == "object":
            _assert_required_keys_declared(property_schema)
        if property_schema.get("type") == "array" and isinstance(property_schema.get("items"), dict):
            _assert_required_keys_declared(property_schema["items"])


def _assert_active_response_contract(response: dict) -> None:
    assert set(response) == ACTIVE_RESPONSE_KEYS
    assert isinstance(response["text"], str)
    assert response["interactive"] is None or isinstance(response["interactive"], dict)
    assert response["catalog_pdf"] is None or isinstance(response["catalog_pdf"], dict)
    assert response["product_image"] is None or isinstance(response["product_image"], dict)
    assert isinstance(response["customer_id"], str)
    assert isinstance(response["escalated"], bool)


@pytest.fixture
def engine_harness(monkeypatch, tmp_path):
    settings = _sample_settings()
    customer = _sample_customer()
    catalog = _sample_catalog()
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Claro, sí tenemos disponible.")),
        continue_after_tool=AsyncMock(return_value=LLMResponse(text="Listo.")),
    )

    monkeypatch.setattr(engine.db, "get_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(engine.db, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.db, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.customers, "get_or_create_customer", AsyncMock(return_value=customer))
    monkeypatch.setattr(engine.customers, "add_tags", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.escalations, "escalate_customer_automatically", AsyncMock(return_value={"id": "customer-1"}))
    monkeypatch.setattr(engine.escalations, "reactivate_if_expired", AsyncMock(return_value=SimpleNamespace(status="skipped")))
    monkeypatch.setattr(engine.conversations, "get_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine.conversations, "get_recent_summary", AsyncMock(return_value="Resumen reciente"))
    monkeypatch.setattr(engine.conversations, "store_message", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.orders, "get_latest_open_order", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.orders, "get_unambiguous_open_order", AsyncMock(return_value=(None, False)))
    monkeypatch.setattr(engine.orders, "create_order", AsyncMock(return_value=_sample_order()))
    monkeypatch.setattr(engine.orders, "update_payment_status", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.orders, "update_order_payment_status", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.sessions, "get_session", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.analytics, "log_response", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.analytics, "log_ai_run", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_runner, "_list_providers", lambda: ["openai"])
    monkeypatch.setattr(agent_runner, "get_provider", lambda name: provider)
    monkeypatch.setattr(engine, "get_config", lambda: SimpleNamespace(store_name="Tienda Rosa"))
    monkeypatch.setattr(engine, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr(engine, "get_cached_reference_catalog", lambda: catalog)
    monkeypatch.setattr(engine, "ensure_fresh_catalog", AsyncMock(return_value=catalog))
    monkeypatch.setattr(tool_catalog, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr(tool_catalog, "ensure_fresh_catalog", AsyncMock(return_value=catalog))
    monkeypatch.setattr(tool_messaging, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr(engine, "notify_incoming_message", AsyncMock(return_value=None))
    monkeypatch.setattr(engine, "notify_escalation", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_messaging, "notify_escalation", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_orders, "notify_new_order", AsyncMock(return_value=None))
    monkeypatch.setattr(engine, "analyze_payment_screenshot", AsyncMock(return_value={"analyzed": False}))
    monkeypatch.setattr(tool_messaging, "ensure_catalog_pdf", MagicMock(return_value=tmp_path / "catalog.pdf"))

    return SimpleNamespace(
        settings=settings,
        customer=customer,
        catalog=catalog,
        provider=provider,
    )


def _sample_order(**overrides) -> dict:
    order = {
        "order_id": "order-1",
        "id": "order-1",
        "items": [
            {
                "product_name": "Pijama satén azul",
                "sku": "PJ-001-S",
                "size": "S",
                "quantity": 1,
                "unit_price": 28,
            }
        ],
        "total": 28.0,
        "subtotal": 28.0,
        "discount_applied": False,
        "discount_rate": 0.0,
        "discount_percent": 0.0,
        "discount_threshold_usd": 350.0,
        "discount_amount": 0.0,
        "payment_method": "Zelle",
        "shipping_city": "Caracas",
        "status": "pending",
        "payment_status": "pending",
        "created_new": True,
    }
    order.update(overrides)
    return order


def test_tool_contract_records_current_names_order_and_schema_shape():
    names = [tool["name"] for tool in TOOLS]

    assert names == EXPECTED_TOOL_ORDER
    assert len(names) == len(set(names))

    for tool in TOOLS:
        assert isinstance(tool["name"], str)
        assert isinstance(tool["description"], str)
        assert tool["parameters"]["type"] == "object"
        assert isinstance(tool["parameters"].get("properties"), dict)
        _assert_required_keys_declared(tool["parameters"])


def test_prompt_includes_dynamic_store_catalog_channel_customer_order_payment_and_pricing_context():
    prompts.reload_template()
    catalog_markdown = (
        "| Producto | Categoría | Tallas disponibles | Precio (USD) | Disponibilidad |\n"
        "| Pijama satén azul | Pijamas | S,M | $28.00 | Disponible |"
    )

    prompt = prompts.build_system_prompt(
        catalog_markdown=catalog_markdown,
        store_name="Tienda Rosa",
        channel="instagram",
        customer={
            "display_name": "Luisana Perez",
            "tags": ["interested:pajamas", "size:M", "city:caracas", "vip"],
            "last_shipping_address": "Av Principal, Casa 8",
            "last_shipping_city": "Caracas",
            "last_shipping_method": "mrw",
            "total_orders": 2,
            "total_spent": 120,
        },
        open_order={
            "items": [{"product_name": "Pijama satén azul", "quantity": 2}],
            "payment_status": "pending",
            "payment_method": "Zelle",
            "total": 56,
        },
        payment_methods=[
            {
                "id": "pm-zelle",
                "name": "Zelle",
                "information": "Correo: pagos@example.com",
            }
        ],
        accepted_exchange_rate="40,25 Bs/USD",
        order_discount_percent=10,
        order_discount_threshold_usd=350,
    )

    assert "Tienda Rosa" in prompt
    assert "Pijama satén azul" in prompt
    assert "Instagram DM" in prompt
    assert "Nombre confirmado para saludar: Luisana" in prompt
    assert "Intereses: pajamas" in prompt
    assert "Tallas: M" in prompt
    assert "Ciudad: caracas" in prompt
    assert "Última dirección de envío: Av Principal, Casa 8" in prompt
    assert "Pedido pendiente abierto" in prompt
    assert "Resumen pedido pendiente: Pijama satén azul x2" in prompt
    assert "**Zelle**: Correo: pagos@example.com" in prompt
    assert "Zelle" in prompt
    assert "40,25 Bs por USD" in prompt
    assert "10%" in prompt
    assert "$350" in prompt


@pytest.mark.asyncio
async def test_generate_response_contract_for_normal_text(engine_harness):
    engine_harness.provider.chat.return_value = LLMResponse(text="Hola, claro que sí tenemos pijamas.")

    response = await engine.generate_response("whatsapp", "584121234567", "Hola")

    _assert_active_response_contract(response)
    assert response["text"] == "Hola, claro que sí tenemos pijamas."
    assert response["interactive"] is None
    assert response["catalog_pdf"] is None
    assert response["product_image"] is None
    engine_harness.provider.chat.assert_awaited_once()
    engine.analytics.log_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivery_aware_user_history_survives_provider_failure(engine_harness):
    engine_harness.provider.chat.side_effect = RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await engine.generate_response(
            "whatsapp",
            "584121234567",
            "Quiero ver pijamas",
            persist_assistant_message=False,
            persist_user_before_response=True,
            message_source_id="meta-job-1",
        )

    persisted = engine.conversations.store_message.await_args_list
    assert len(persisted) == 1
    assert persisted[0].kwargs["role"] == "user"
    assert persisted[0].kwargs["content"] == "Quiero ver pijamas"
    assert persisted[0].kwargs["source_id"] == "meta-job-1"


@pytest.mark.asyncio
async def test_delivery_aware_user_history_precedes_vision_failure(engine_harness):
    engine.analyze_payment_screenshot.side_effect = RuntimeError("vision unavailable")

    with pytest.raises(RuntimeError, match="vision unavailable"):
        await engine.generate_response(
            "instagram",
            "ig-user",
            "Te envío el comprobante",
            media_url="https://cdn.test/proof.jpg",
            persist_assistant_message=False,
            persist_user_before_response=True,
            message_source_id="meta-job-vision",
        )

    engine.conversations.store_message.assert_awaited_once()
    persisted = engine.conversations.store_message.await_args.kwargs
    assert persisted["role"] == "user"
    assert persisted["content"] == "Te envío el comprobante"
    assert persisted["source_id"] == "meta-job-vision"


@pytest.mark.asyncio
async def test_exchange_rate_question_is_answered_without_llm(engine_harness):
    response = await engine.generate_response("whatsapp", "584121234567", "¿A qué tasa reciben?")

    _assert_active_response_contract(response)
    assert response["text"] == "La tasa que usamos actualmente es 40,25 Bs por USD."
    engine_harness.provider.chat.assert_not_awaited()
    engine.analytics.log_response.assert_not_awaited()
    engine.analytics.log_ai_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_question_with_discount_question_uses_llm(engine_harness):
    engine_harness.provider.chat.return_value = LLMResponse(
        text="La tasa es 40,25 Bs por USD. Los descuentos aplican según el subtotal del pedido."
    )

    response = await engine.generate_response(
        "whatsapp",
        "584121234567",
        "Solo para futuras compras\n¿A qué tasa reciben?\n¿Aceptan descuentos?",
    )

    assert response["text"] == "La tasa es 40,25 Bs por USD. Los descuentos aplican según el subtotal del pedido."
    engine_harness.provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_bs_total_question_is_not_mistaken_for_exchange_rate(engine_harness):
    engine_harness.provider.chat.return_value = LLMResponse(text="Claro, te ayudo a calcularlo según el producto.")

    response = await engine.generate_response("whatsapp", "584121234567", "¿A cuánto queda en Bs?")

    assert response["text"] == "Claro, te ayudo a calcularlo según el producto."
    engine_harness.provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_response_contract_for_interactive_buttons(engine_harness):
    engine_harness.provider.chat.return_value = LLMResponse(
        tool_calls=[
            _tool_call(
                "send_interactive_buttons",
                {"body_text": "¿Prefieres MRW o Zoom?", "buttons": ["MRW", "Zoom"]},
            )
        ]
    )
    engine_harness.provider.continue_after_tool.return_value = LLMResponse(text=None)

    response = await engine.generate_response("whatsapp", "584121234567", "Envío")

    _assert_active_response_contract(response)
    assert response["text"] == "¿Prefieres MRW o Zoom?"
    assert response["interactive"] == {
        "type": "interactive_buttons",
        "body_text": "¿Prefieres MRW o Zoom?",
        "buttons": ["MRW", "Zoom"],
    }
    assert response["catalog_pdf"] is None
    assert response["product_image"] is None


@pytest.mark.asyncio
async def test_generate_response_contract_for_catalog_pdf(engine_harness):
    engine_harness.provider.chat.return_value = LLMResponse(
        tool_calls=[_tool_call("send_catalog_pdf", {"caption": "Te envío el catálogo."})]
    )
    engine_harness.provider.continue_after_tool.return_value = LLMResponse(text="Te mandé el catálogo por aquí.")

    response = await engine.generate_response("whatsapp", "584121234567", "Quiero ver el catálogo")

    _assert_active_response_contract(response)
    assert response["text"] == "Te mandé el catálogo por aquí."
    assert response["catalog_pdf"] == {"type": "catalog_pdf", "caption": "Te envío el catálogo."}
    assert response["interactive"] is None
    assert response["product_image"] is None
    tool_messaging.ensure_catalog_pdf.assert_called_once_with(engine_harness.catalog)


@pytest.mark.asyncio
async def test_generate_response_contract_for_product_image(engine_harness):
    engine_harness.provider.chat.return_value = LLMResponse(
        tool_calls=[
            _tool_call(
                "send_product_image",
                {"product_query": "Pijama satén azul", "caption": "Aquí tienes la foto."},
            )
        ]
    )
    engine_harness.provider.continue_after_tool.return_value = LLMResponse(text=None)

    response = await engine.generate_response("instagram", "ig-123", "Me muestras la foto")

    _assert_active_response_contract(response)
    assert response["text"] == "Aquí tienes la foto."
    assert response["product_image"] == {
        "type": "product_image",
        "image_url": "https://example.com/pijama.jpg",
        "caption": "Aquí tienes la foto.",
        "product_name": "Pijama satén azul",
    }
    assert response["interactive"] is None
    assert response["catalog_pdf"] is None


@pytest.mark.asyncio
async def test_generate_response_contract_for_paused_ai(engine_harness):
    engine_harness.settings["ai_enabled"] = False

    response = await engine.generate_response("whatsapp", "584121234567", "Hola")

    assert set(response) == ACTIVE_RESPONSE_KEYS | {"paused"}
    assert response["text"] is None
    assert response["interactive"] is None
    assert response["catalog_pdf"] is None
    assert response["product_image"] is None
    assert response["customer_id"] == "customer-1"
    assert response["escalated"] is False
    assert response["paused"] is True
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_response_contract_for_escalated_customer(engine_harness):
    engine_harness.customer["conversation_state"] = "escalated"

    response = await engine.generate_response("whatsapp", "584121234567", "Hola")

    assert set(response) == ACTIVE_RESPONSE_KEYS | {"paused"}
    assert response["text"] is None
    assert response["interactive"] is None
    assert response["catalog_pdf"] is None
    assert response["product_image"] is None
    assert response["customer_id"] == "customer-1"
    assert response["escalated"] is True
    assert response["paused"] is False
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_customer_is_suppressed_without_owner_notification(engine_harness):
    engine_harness.customer["conversation_state"] = "blocked"
    engine_harness.customer["is_blocked"] = True

    response = await engine.generate_response("whatsapp", "584121234567", "Hola")

    assert response["text"] is None
    assert response["blocked"] is True
    assert response["paused"] is False
    assert response["escalated"] is False
    engine.notify_incoming_message.assert_not_awaited()
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_human_request_escalates_before_router_or_llm(engine_harness):
    response = await engine.generate_response("whatsapp", "584121234567", "Quiero hablar con una persona real")

    assert response["escalated"] is True
    assert "persona del equipo" in response["text"]
    engine.escalations.escalate_customer_automatically.assert_awaited_once_with("customer-1", settings=engine_harness.settings)
    engine.notify_escalation.assert_awaited_once()
    engine_harness.provider.chat.assert_not_awaited()
    engine.conversations.get_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_hostile_message_escalates_before_normal_llm_flow(engine_harness):
    response = await engine.generate_response("whatsapp", "584121234567", "Son unos ladrones, los voy a denunciar")

    assert response["escalated"] is True
    assert "persona del equipo" in response["text"]
    engine.escalations.escalate_customer_automatically.assert_awaited_once_with("customer-1", settings=engine_harness.settings)
    engine.notify_escalation.assert_awaited_once()
    engine_harness.provider.chat.assert_not_awaited()
    engine.conversations.get_history.assert_not_awaited()
    engine.orders.get_latest_open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_instagram_comment_unresolved_widget_sku_uses_fallback(engine_harness):
    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Precio?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {"product_sku": "PJ-001"},
        },
    )

    assert response["text"] == "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    assert response["interactive"] is None
    assert response["catalog_pdf"] is None
    assert response["product_image"] is None
    engine_harness.provider.chat.assert_not_awaited()
    engine.conversations.get_history.assert_awaited_once_with("customer-1", limit=2)
    engine.orders.get_latest_open_order.assert_not_awaited()
    assert engine.analytics.log_ai_run.await_args.kwargs["route_intent"] == "public_comment_private_invite"


@pytest.mark.asyncio
async def test_resolved_instagram_mapping_uses_injected_product_sku(engine_harness):
    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Precio?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_sku": "PJ-001",
            },
        },
    )

    assert response["text"] == "¡Hola! El precio de Pijama satén azul es $28."
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_instagram_mapping_ignores_caption_product_guess(engine_harness):
    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Disponible?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "timed_out",
                "post_caption": "Nueva Pijama satén azul disponible",
            },
        },
    )

    assert response["text"] == (
        "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    )
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_mapping_never_falls_back_to_callback_parent_sku(engine_harness):
    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Precio?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["UNKNOWN-MAPPED-SKU"],
                "parent_sku": "PJ-001",
            },
        },
    )

    assert response["text"] == (
        "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    )
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_instagram_comment_stock_uses_resolved_mapping_without_stock_count(engine_harness):
    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Disponible?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001"],
                "product_sku": "PJ-001",
                "post_caption": "Nueva Pijama satén azul disponible",
            },
        },
    )

    assert response["text"] == "¡Hola! Sí, Pijama satén azul está disponible."
    assert "4" not in response["text"]
    engine_harness.provider.chat.assert_not_awaited()
    assert engine.analytics.log_ai_run.await_args.kwargs["route_intent"] == "public_comment_stock"


@pytest.mark.asyncio
async def test_public_instagram_comment_resolved_product_out_of_stock(engine_harness):
    engine_harness.catalog[0]["stock"] = 0

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Queda disponible?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001"],
                "product_sku": "PJ-001",
            },
        },
    )

    assert response["text"] == "¡Hola! Por ahora Pijama satén azul no está disponible."
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_instagram_comment_multiple_current_prices_uses_fallback(engine_harness):
    engine_harness.catalog.append({
        **engine_harness.catalog[0],
        "sku": "PJ-001-M",
        "size": "M",
        "sizes": "M",
        "price_usd": 30,
    })

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Cuánto cuesta?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001"],
                "product_sku": "PJ-001",
            },
        },
    )

    assert response["text"] == "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Precio?", "Está disponible?"])
async def test_multi_product_generic_questions_request_clarification(engine_harness, message):
    engine_harness.catalog.append(_second_catalog_product())

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        message,
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001", "BODY-002"],
            },
        },
    )

    assert response["text"] == (
        "¿Cuál producto de la publicación te interesa? "
        "Dinos el nombre o escríbenos al DM y te ayudamos 😊"
    )
    assert engine.analytics.log_ai_run.await_args.kwargs["route_intent"] == (
        "public_comment_clarification"
    )
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_product_explicit_name_returns_mapped_product_price(engine_harness):
    engine_harness.catalog.append(_second_catalog_product())

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "¿Cuánto cuesta el body negro?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001", "BODY-002"],
            },
        },
    )

    assert response["text"] == "¡Hola! El precio de Body negro es $25."
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_product_partial_name_returns_the_uniquely_referenced_product_price(engine_harness):
    engine_harness.catalog.extend([
        _pajama_catalog_product(),
        _pajama_catalog_product(
            sku="PJ-003-S",
            parent_sku="PJ-003",
            product_name="Pijama rayas rosa",
            price_usd=29,
        ),
    ])

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Que bonita la pijama azul! Que precio tiene?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-002", "PJ-003", "PJ-001"],
            },
        },
    )

    assert response["text"] == "¡Hola! El precio de Pijama satén azul es $28."
    assert engine.analytics.log_ai_run.await_args.kwargs["route_intent"] == "public_comment_price"
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_product_follow_up_inherits_price_intent_after_clarification(engine_harness):
    engine_harness.catalog.extend([
        _pajama_catalog_product(
            sku="PJ-002-S",
            parent_sku="PJ-002",
            product_name="Pijama rayas rosa",
            price_usd=27,
        ),
        _second_catalog_product(
            sku="SET-003-S",
            parent_sku="SET-003",
            product_name="Set completo negro",
            description="Set completo negro de dos piezas",
            price_usd=35,
        ),
        _second_catalog_product(
            sku="SET-004-S",
            parent_sku="SET-004",
            product_name="Set completo rojo",
            description="Set completo rojo de dos piezas",
            price_usd=36,
        ),
    ])
    engine.conversations.get_history.return_value = [
        {"role": "user", "content": "Precio?"},
        {"role": "assistant", "content": engine._PUBLIC_COMMENT_CLARIFICATION},
    ]

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "El negro",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-002", "SET-003", "SET-004"],
            },
        },
    )

    assert response["text"] == "¡Hola! El precio de Set completo negro es $35."
    assert engine.analytics.log_ai_run.await_args.kwargs["route_intent"] == "public_comment_price"
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_product_description_reference_uses_llm_matcher(engine_harness):
    engine_harness.catalog.extend([
        _pajama_catalog_product(description="Pijama fresca amarilla para descansar"),
        _pajama_catalog_product(
            sku="PJ-003-S",
            parent_sku="PJ-003",
            product_name="Pijama rayas rosa",
            description="Pijama con rayas rosas",
            price_usd=29,
        ),
    ])
    engine_harness.provider.chat.return_value = LLMResponse(
        text='{"sku":"PJ-001","confidence":0.94}',
        usage={"input_tokens": 80, "output_tokens": 12},
    )

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "La de seda para dormir, que precio tiene?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-002", "PJ-003", "PJ-001"],
            },
        },
    )

    assert response["text"] == "¡Hola! El precio de Pijama satén azul es $28."
    assert engine.analytics.log_ai_run.await_args.kwargs["route_source"] == "public_comment_llm_matcher"
    engine_harness.provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_product_explicit_name_returns_mapped_product_availability(engine_harness):
    engine_harness.catalog.append(_second_catalog_product())

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "¿Está disponible el pijama satén azul?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001", "BODY-002"],
            },
        },
    )

    assert response["text"] == "¡Hola! Sí, Pijama satén azul está disponible."
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_product_does_not_resolve_unmapped_global_catalog_product(engine_harness):
    engine_harness.catalog.extend([
        _second_catalog_product(),
        _second_catalog_product(
            sku="SET-003-S",
            parent_sku="SET-003",
            product_name="Conjunto verde",
            price_usd=35,
        ),
    ])

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "¿Cuánto cuesta el conjunto verde?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001", "BODY-002"],
            },
        },
    )

    assert response["text"].startswith("¿Cuál producto de la publicación")
    assert "$35" not in response["text"]
    engine_harness.provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_product_invalid_mapped_sku_fails_closed(engine_harness):
    engine_harness.catalog.append(_second_catalog_product())

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "¿Cuánto cuesta el body negro?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["BODY-002", "INACTIVE-999"],
            },
        },
    )

    assert response["text"] == (
        "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    )
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_product_zero_stock_product_returns_unavailable(engine_harness):
    engine_harness.catalog.append(_second_catalog_product(stock=0))

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "¿Está disponible el body negro?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001", "BODY-002"],
            },
        },
    )

    assert response["text"] == "¡Hola! Por ahora Body negro no está disponible."
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "¿Está disponible BODY-002-S?",
        "¿Cuánto cuesta BODY-002-S?",
        "¿Está disponible el body negro BODY-002-S?",
        "¿Cuánto cuesta el body negro BODY-002-S?",
    ],
)
async def test_multi_product_exact_variant_sku_does_not_return_grouped_answer(
    engine_harness,
    message,
):
    engine_harness.catalog.extend([
        _second_catalog_product(sku="BODY-002-S", size="S", sizes="S", stock=0),
        _second_catalog_product(sku="BODY-002-M", size="M", sizes="M", stock=3),
    ])

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        message,
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {
                "mapping_status": "resolved",
                "product_skus": ["PJ-001", "BODY-002"],
            },
        },
    )

    assert response["text"] == (
        "¿Cuál producto de la publicación te interesa? "
        "Dinos el nombre o escríbenos al DM y te ayudamos 😊"
    )
    assert "está disponible" not in response["text"]
    assert "$25" not in response["text"]
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_instagram_comment_other_uses_exact_phone_fallback(engine_harness):
    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Talla M?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {"product_sku": "PJ-001"},
        },
    )

    assert response["text"] == "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    engine_harness.provider.chat.assert_not_awaited()
    assert engine.analytics.log_ai_run.await_args.kwargs["route_intent"] == "public_comment_private_invite"


@pytest.mark.asyncio
async def test_public_instagram_comment_price_without_context_uses_dm_only_fallback(engine_harness):
    engine_harness.settings["store_phone_number"] = ""

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Cuánto cuesta?",
        integration_context={"provider": "kommo", "interaction_type": "instagram_comment"},
    )

    assert response["text"] == "Hola! Para más info escríbenos al DM! :)"
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_instagram_comment_ambiguous_post_context_falls_back(engine_harness):
    engine_harness.catalog.append({
        "sku": "PJ-002-S",
        "parent_sku": "PJ-002",
        "product_name": "Pijama satén rosada",
        "category": "Pijamas",
        "description": "Pijama suave rosada",
        "size": "S",
        "sizes": "S",
        "price_usd": 30,
        "stock": 2,
        "image_url": "https://example.com/pijama-rosada.jpg",
    })

    response = await engine.generate_response(
        "instagram",
        "ig-commenter",
        "Precio?",
        integration_context={
            "provider": "kommo",
            "interaction_type": "instagram_comment",
            "public_comment_context": {"post_caption": "Nuevas pijamas de satén"},
        },
    )

    assert response["text"] == "Hola! Para más info escríbenos al DM o por WhatsApp al +58 412-1234567! :)"
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_proof_without_existing_order_does_not_create_order(engine_harness):
    engine.analyze_payment_screenshot.return_value = {
        "analyzed": True,
        "payment_method": "zelle",
        "amount": "28.00",
        "currency": "USD",
        "status": "completed",
        "confidence": "high",
        "reference": "TXN-ENGINE-123",
        "date": datetime.now(UTC).isoformat(),
        "proof_hash": "b" * 64,
        "summary": "Comprobante Zelle completado por $28",
    }

    response = await engine.generate_response(
        "whatsapp",
        "584121234567",
        "Te envío el comprobante del pago",
        media_url="media-1",
    )

    assert "todavía no tengo el pedido registrado" in response["text"]
    assert response["interactive"] is None
    assert response["catalog_pdf"] is None
    assert response["product_image"] is None
    assert response["escalated"] is False
    engine.orders.create_order.assert_not_awaited()
    engine.orders.update_payment_status.assert_not_awaited()
    engine.orders.update_order_payment_status.assert_not_awaited()
    engine_harness.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_payment_proof_short_circuits_llm_and_updates_existing_order(engine_harness, monkeypatch):
    engine.orders.get_latest_open_order.return_value = _sample_order(total=28.0, payment_method="Zelle")
    engine.orders.get_unambiguous_open_order.return_value = (
        _sample_order(total=28.0, payment_method="Zelle"),
        False,
    )
    engine.orders.update_order_payment_status.return_value = {"order_id": "order-1", "payment_status": "proof_received"}
    set_current_order = AsyncMock(return_value=None)
    monkeypatch.setattr(engine.sessions, "set_current_order", set_current_order)
    engine.analyze_payment_screenshot.return_value = {
        "analyzed": True,
        "payment_method": "zelle",
        "amount": "28.00",
        "currency": "USD",
        "status": "completed",
        "confidence": "high",
        "reference": "TXN-ENGINE-125",
        "date": datetime.now(UTC).isoformat(),
        "proof_hash": "2" * 64,
        "recipient_identifier": "pagos@example.com",
        "summary": "Pago Zelle a pagos@example.com por $28",
    }

    response = await engine.generate_response(
        "whatsapp",
        "584121234567",
        "Te envío el comprobante del pago",
        media_url="media-1",
    )

    assert "recibí el comprobante" in response["text"]
    engine_harness.provider.chat.assert_not_awaited()
    engine.orders.create_order.assert_not_awaited()
    engine.orders.update_payment_status.assert_not_awaited()
    engine.orders.update_order_payment_status.assert_awaited_once()
    payment_update = engine.orders.update_order_payment_status.await_args
    assert payment_update.args == ("order-1",)
    assert payment_update.kwargs["status"] == "proof_received"
    assert payment_update.kwargs["note"] == "Pago Zelle a pagos@example.com por $28"
    set_current_order.assert_awaited_once_with(
        "customer-1",
        None,
        workflow_stage="completed",
        active_agent="payment",
    )


@pytest.mark.asyncio
async def test_unreadable_payment_proof_image_short_circuits_llm_without_update(engine_harness):
    engine.orders.get_latest_open_order.return_value = _sample_order(total=28.0, payment_method="Zelle")
    engine.orders.get_unambiguous_open_order.return_value = (
        _sample_order(total=28.0, payment_method="Zelle"),
        False,
    )
    engine.analyze_payment_screenshot.return_value = {"analyzed": False}

    response = await engine.generate_response(
        "whatsapp",
        "584121234567",
        "Te envío el comprobante del pago",
        media_url="media-1",
    )

    assert "no pude leer bien el comprobante" in response["text"]
    engine_harness.provider.chat.assert_not_awaited()
    engine.orders.update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_payment_validation_does_not_mark_payment_as_confirmed(engine_harness):
    engine.orders.get_latest_open_order.return_value = _sample_order(total=28.0, payment_method="Zelle")
    engine.orders.get_unambiguous_open_order.return_value = (
        _sample_order(total=28.0, payment_method="Zelle"),
        False,
    )

    result = await engine._execute_tool(
        "update_payment_status",
        {"confirmation_note": "Comprobante recibido"},
        engine_harness.customer,
        "whatsapp",
        payment_methods=engine_harness.settings["payment_methods"],
        vision_result={
            "analyzed": True,
            "payment_method": "zelle",
            "amount": "20.00",
            "currency": "USD",
            "status": "completed",
            "confidence": "high",
            "reference": "TXN-ENGINE-124",
            "date": datetime.now(UTC).isoformat(),
            "proof_hash": "c" * 64,
            "recipient_identifier": "pagos@example.com",
            "summary": "Pago Zelle a pagos@example.com por $20",
        },
        payment_proof_attempt=True,
    )

    assert result["status"] == "error"
    assert "no coincide con el monto esperado" in result["message"]
    engine.orders.update_order_payment_status.assert_not_awaited()
    engine.orders.update_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_create_order_tool_call_does_not_create_duplicate_side_effects(engine_harness):
    create_args = {
        "items": [
            {
                "product_name": "Pijama satén azul",
                "sku": "PJ-001-S",
                "size": "S",
                "quantity": 1,
                "unit_price": 28,
            }
        ],
        "payment_method": "Zelle",
        "shipping_city": "Caracas",
        "shipping_method": "mrw",
        "pickup_agency": "MRW Chacao",
    }
    engine_harness.provider.chat.return_value = LLMResponse(
        tool_calls=[_tool_call("create_order", create_args, tool_id="create-1")]
    )
    engine_harness.provider.continue_after_tool.side_effect = [
        LLMResponse(tool_calls=[_tool_call("create_order", create_args, tool_id="create-2")]),
        LLMResponse(text="Listo, tu pedido quedó registrado."),
    ]

    response = await engine.generate_response("whatsapp", "584121234567", "Quiero pagar con Zelle")

    assert response["text"] == "Listo, tu pedido quedó registrado."
    engine.orders.create_order.assert_awaited_once()
    tool_orders.notify_new_order.assert_awaited_once()
    assert engine_harness.provider.continue_after_tool.await_count == 2
    second_tool_result = engine_harness.provider.continue_after_tool.await_args_list[1].kwargs["tool_result"]
    assert "duplicate_ignored" in second_tool_result
    persisted_tool_log = engine.conversations.store_message.await_args_list[-1].kwargs["function_calls"]
    assert persisted_tool_log == [{"name": "create_order", "status": "pending"}, {"name": "create_order", "status": "pending", "duplicate_ignored": True}]
    assert "shipping_address" not in str(persisted_tool_log)
    assert "args" not in str(persisted_tool_log)
    assert "result" not in str(persisted_tool_log)


@pytest.mark.asyncio
async def test_shadow_mode_does_not_create_or_mutate_workflow_session(engine_harness, monkeypatch):
    engine_harness.settings["ai_orchestration_mode"] = "shadow"
    get_session = AsyncMock(return_value=None)
    get_or_create_session = AsyncMock(return_value=None)
    set_active_agent = AsyncMock(return_value=None)
    monkeypatch.setattr(engine.sessions, "get_session", get_session)
    monkeypatch.setattr(engine.sessions, "get_or_create_session", get_or_create_session)
    monkeypatch.setattr(engine.sessions, "set_active_agent", set_active_agent)

    response = await engine.generate_response("whatsapp", "584121234567", "Tienen pijamas?")

    assert response["text"] == "Claro, sí tenemos disponible."
    get_session.assert_awaited_once_with("customer-1")
    get_or_create_session.assert_not_awaited()
    set_active_agent.assert_not_awaited()
    assert engine.analytics.log_ai_run.await_args.kwargs["shadow_evaluation"] is True


@pytest.mark.asyncio
async def test_product_unavailable_inquiry_is_not_escalated_merely_for_availability(engine_harness):
    result = await engine._execute_tool(
        "escalate_to_human",
        {
            "reason": "Producto no está disponible en el catálogo",
            "urgency": "medium",
        },
        engine_harness.customer,
        "whatsapp",
        latest_user_message="¿Tienes panty azul en talla XS disponible?",
    )

    assert result["status"] == "error"
    assert "No escales preguntas normales de productos" in result["message"]
    tool_messaging.notify_escalation.assert_not_awaited()
    engine.escalations.escalate_customer_automatically.assert_not_awaited()
