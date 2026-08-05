from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai import engine
from app.ai import runner as agent_runner
from app.ai.providers.base import LLMResponse
from app.crm import sessions

ACTIVE_RESPONSE_KEYS = {
    "text",
    "interactive",
    "catalog_pdf",
    "product_image",
    "whatsapp_handoff",
    "customer_id",
    "escalated",
}


def _settings(**overrides) -> dict:
    values = {
        "ai_enabled": True,
        "ai_orchestration_mode": "multi_agent",
        "llm_provider": "openai",
        "llm_model": "gpt-5.6-luna",
        "llm_temperature": 0.2,
        "llm_max_tokens": 500,
        "max_conversation_history": 20,
        "auto_fallback": False,
        "fallback_provider": "anthropic",
        "fallback_model": "claude-haiku-4-5",
        "store_name": "Tienda Rosa",
        "store_phone_number": "+58 412-1234567",
        "payment_methods": [{"id": "pm-zelle", "name": "Zelle", "information": "Correo: pagos@example.com"}],
        "accepted_exchange_rate": "40 Bs/USD",
        "exchange_rate_reference": "manual",
        "manual_exchange_rate": "40",
        "order_discount_percent": 10,
        "order_discount_threshold_usd": 350,
    }
    values.update(overrides)
    return values


def _customer(**overrides) -> dict:
    values = {
        "id": "customer-1",
        "channel": "whatsapp",
        "platform_id": "584121234567",
        "display_name": "Luisana Perez",
        "conversation_state": "active",
        "tags": [],
        "total_orders": 0,
        "total_spent": 0,
        "last_shipping_address": "Av Principal, Casa 8",
        "last_shipping_city": "Caracas",
        "last_shipping_method": "mrw",
    }
    values.update(overrides)
    return values


def _order(**overrides) -> dict:
    values = {
        "id": "order-1",
        "order_id": "order-1",
        "items": [{"product_name": "Pijama satén azul", "sku": "PJ-001-S", "size": "S", "quantity": 1, "unit_price": 28}],
        "total": 28.0,
        "payment_method": "Zelle",
        "payment_status": "pending",
        "shipping_status": "pending",
        "tracking_number": None,
    }
    values.update(overrides)
    return values


def _catalog() -> list[dict]:
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
        },
        {
            "sku": "PJ-002-M",
            "parent_sku": "PJ-002",
            "product_name": "Pijama satén rosada",
            "category": "Pijamas",
            "description": "Pijama rosada",
            "size": "M",
            "sizes": "M",
            "price_usd": 30,
            "stock": 3,
            "image_url": "https://example.com/pijama-rosada.jpg",
        },
    ]


def _tool_call(name: str, arguments: dict | None = None, tool_id: str = "tool-1") -> dict:
    return {"id": tool_id, "name": name, "arguments": arguments or {}}


def _session(active_agent: str = "legacy", workflow_stage: str = "idle", **overrides) -> sessions.ConversationSession:
    values = {"customer_id": "customer-1", "active_agent": active_agent, "workflow_stage": workflow_stage}
    values.update(overrides)
    return sessions.ConversationSession.model_validate(values)


class TranscriptHarness:
    def __init__(self, monkeypatch, *, settings: dict | None = None, customer: dict | None = None, provider=None):
        self.settings = settings or _settings()
        self.customer = customer or _customer()
        self.provider = provider or SimpleNamespace(
            chat=AsyncMock(return_value=LLMResponse(text="Claro, te ayudo.")),
            continue_after_tool=AsyncMock(return_value=LLMResponse(text="Listo.")),
        )
        self.messages: list[dict] = []
        self.session = _session()
        self.execute_tool = AsyncMock(return_value={"status": "ok"})

        async def store_message(**kwargs):
            self.messages.append(kwargs)

        async def set_active_agent(customer_id, active_agent, **kwargs):
            self.session = _session(
                active_agent=active_agent,
                workflow_stage=kwargs.get("workflow_stage") or self.session.workflow_stage,
                active_intent=kwargs.get("active_intent"),
                last_route_confidence=kwargs.get("last_route_confidence"),
            )
            return self.session

        monkeypatch.setattr(engine.db, "get_settings", AsyncMock(return_value=self.settings))
        monkeypatch.setattr(engine.db, "fetch_one", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.db, "execute", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.customers, "get_or_create_customer", AsyncMock(return_value=self.customer))
        monkeypatch.setattr(engine.escalations, "escalate_customer_automatically", AsyncMock(return_value={"id": "customer-1"}))
        monkeypatch.setattr(engine.escalations, "reactivate_if_expired", AsyncMock(return_value=SimpleNamespace(status="skipped")))
        monkeypatch.setattr(engine.customers, "add_tags", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.conversations, "get_history", AsyncMock(side_effect=lambda customer_id, limit=20: list(self.messages)))
        monkeypatch.setattr(engine.conversations, "get_recent_summary", AsyncMock(return_value="Resumen reciente"))
        monkeypatch.setattr(engine.conversations, "store_message", AsyncMock(side_effect=store_message))
        monkeypatch.setattr(engine.orders, "get_latest_open_order", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.orders, "get_unambiguous_open_order", AsyncMock(return_value=(None, False)))
        monkeypatch.setattr(engine.orders, "create_order", AsyncMock(return_value=_order()))
        monkeypatch.setattr(engine.orders, "update_payment_status", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.orders, "update_order_payment_status", AsyncMock(return_value={"order_id": "order-1", "payment_status": "proof_received"}))
        monkeypatch.setattr(engine.orders, "get_customer_open_order_by_id", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.sessions, "get_session", AsyncMock(side_effect=lambda customer_id: self.session))
        monkeypatch.setattr(engine.sessions, "get_or_create_session", AsyncMock(side_effect=lambda customer_id: self.session))
        monkeypatch.setattr(engine.sessions, "set_active_agent", AsyncMock(side_effect=set_active_agent))
        monkeypatch.setattr(engine.sessions, "set_current_order", AsyncMock(return_value=_session("payment", "completed", current_order_id="order-1")))
        monkeypatch.setattr(engine, "notify_incoming_message", AsyncMock(return_value=None))
        monkeypatch.setattr(engine, "notify_escalation", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.analytics, "log_response", AsyncMock(return_value=None))
        monkeypatch.setattr(engine.analytics, "log_ai_run", AsyncMock(return_value=None))
        monkeypatch.setattr(engine, "analyze_payment_screenshot", AsyncMock(return_value={"analyzed": False}))
        monkeypatch.setattr(engine, "get_config", lambda: SimpleNamespace(store_name="Tienda Rosa", ai_orchestration_mode="legacy"))
        monkeypatch.setattr(engine, "get_cached_catalog", lambda: _catalog())
        monkeypatch.setattr(agent_runner, "_list_providers", lambda: ["openai"])
        monkeypatch.setattr(agent_runner, "get_provider", lambda name: self.provider)
        monkeypatch.setattr(agent_runner, "execute_tool", self.execute_tool)

    async def run(self, text: str, *, channel: str = "whatsapp", media_url: str | None = None) -> dict:
        return await engine.generate_response(channel, "584121234567", text, media_url=media_url)

    def selected_agent(self) -> str:
        return engine.analytics.log_ai_run.await_args.kwargs["selected_agent"]

    def route_source(self) -> str:
        return engine.analytics.log_ai_run.await_args.kwargs["route_source"]

    def tool_names(self) -> list[str]:
        return engine.analytics.log_ai_run.await_args.kwargs["tool_names"]


def _assert_response_contract(response: dict) -> None:
    assert set(response) == ACTIVE_RESPONSE_KEYS
    assert isinstance(response["text"], str)
    assert response["interactive"] is None or isinstance(response["interactive"], dict)
    assert response["catalog_pdf"] is None or isinstance(response["catalog_pdf"], dict)
    assert response["product_image"] is None or isinstance(response["product_image"], dict)
    assert response["whatsapp_handoff"] is None or isinstance(response["whatsapp_handoff"], dict)
    assert response["customer_id"] == "customer-1"
    assert isinstance(response["escalated"], bool)


def _provider_with_tool(tool_name: str, args: dict | None, final_text: str):
    return SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(tool_calls=[_tool_call(tool_name, args)])),
        continue_after_tool=AsyncMock(return_value=LLMResponse(text=final_text)),
    )


def _handoff_payload(message: str = "Hola Instagram") -> dict:
    url = f"https://wa.me/584121234567?text={message.replace(' ', '%20')}"
    return {
        "type": "whatsapp_handoff",
        "url": url,
        "customer_text": (
            "Para ayudarte mejor con el pedido, el pago y el envío, continuamos las compras por WhatsApp:\n"
            f"{url}"
        ),
        "prefilled_message": message,
    }


@pytest.mark.asyncio
async def test_greeting_and_product_recommendation(monkeypatch):
    provider = _provider_with_tool("check_inventory", {"product_query": "pijamas"}, "Sí, tengo pijamas de satén disponibles.")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.execute_tool.return_value = {"found": True, "products": [{"product_name": "Pijama satén azul", "in_stock": True}]}

    response = await h.run("Hola, qué me recomiendas?")

    _assert_response_contract(response)
    assert h.selected_agent() == "sales"
    assert h.tool_names() == ["check_inventory"]
    assert provider.chat.await_args.kwargs["tools"][0]["name"] == "check_inventory"
    h.execute_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_catalog_request_on_whatsapp(monkeypatch):
    provider = _provider_with_tool("send_catalog_pdf", {"caption": "Te envío el catálogo."}, "Te mandé el catálogo por aquí.")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.execute_tool.return_value = {"type": "catalog_pdf", "caption": "Te envío el catálogo."}

    response = await h.run("Quiero ver el catálogo")

    assert h.selected_agent() == "sales"
    assert response["catalog_pdf"] == {"type": "catalog_pdf", "caption": "Te envío el catálogo."}


@pytest.mark.asyncio
async def test_product_discovery_on_instagram_stays_in_channel(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Por aquí te cuento las categorías: pijamas y sets.")),
        continue_after_tool=AsyncMock(),
    )
    h = TranscriptHarness(monkeypatch, provider=provider)

    response = await h.run("¿Qué tienen disponible?", channel="instagram")

    assert h.selected_agent() == "sales"
    assert response["catalog_pdf"] is None
    h.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_instagram_catalog_request_uses_whatsapp_handoff(monkeypatch):
    provider = _provider_with_tool(
        "send_whatsapp_handoff",
        {"handoff_reason": "catalog_pdf"},
        "Claro, te lo enviamos por WhatsApp.",
    )
    h = TranscriptHarness(monkeypatch, provider=provider)
    payload = _handoff_payload("Hola Instagram catalogo PDF")
    h.execute_tool.return_value = payload

    response = await h.run("Quiero ver el catálogo", channel="instagram")

    assert h.selected_agent() == "sales"
    assert h.tool_names() == ["send_whatsapp_handoff"]
    assert response["catalog_pdf"] is None
    assert response["whatsapp_handoff"] == payload
    assert response["text"].count(payload["url"]) == 1


@pytest.mark.asyncio
async def test_instagram_purchase_uses_handoff_in_multi_agent_mode(monkeypatch):
    provider = _provider_with_tool(
        "send_whatsapp_handoff",
        {
            "handoff_reason": "purchase",
            "product_name": "Pijama satén azul",
            "size": "S",
            "quantity": 1,
        },
        "Claro, te ayudamos a continuar.",
    )
    h = TranscriptHarness(monkeypatch, provider=provider)
    payload = _handoff_payload()
    h.execute_tool.return_value = payload

    response = await h.run("Quiero comprar la pijama azul talla S, una", channel="instagram")

    assert h.selected_agent() == "sales"
    assert response["whatsapp_handoff"] == payload
    assert response["text"].count(payload["url"]) == 1
    assert h.tool_names() == ["send_whatsapp_handoff"]


@pytest.mark.asyncio
async def test_instagram_purchase_uses_backend_handoff_in_legacy_mode(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Claro, te ayudamos a continuar.")),
        continue_after_tool=AsyncMock(),
    )
    h = TranscriptHarness(
        monkeypatch,
        settings=_settings(ai_orchestration_mode="legacy"),
        provider=provider,
    )
    payload = _handoff_payload()
    h.execute_tool.return_value = payload

    response = await h.run("Quiero comprar la pijama azul", channel="instagram")

    assert h.selected_agent() == "legacy"
    assert response["whatsapp_handoff"] == payload
    assert response["text"].count(payload["url"]) == 1
    assert h.tool_names() == ["send_whatsapp_handoff"]


@pytest.mark.asyncio
async def test_instagram_informational_question_does_not_handoff(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Cuesta $28 y está disponible en talla S.")),
        continue_after_tool=AsyncMock(),
    )
    h = TranscriptHarness(monkeypatch, provider=provider)

    response = await h.run("¿Cuánto cuesta y qué tallas hay?", channel="instagram")

    assert h.selected_agent() == "sales"
    assert response["whatsapp_handoff"] is None
    assert "wa.me" not in response["text"]
    h.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_unavailable_with_alternatives(monkeypatch):
    provider = _provider_with_tool("check_inventory", {"product_query": "pijama negra", "size": "M"}, "Esa está agotada, pero tengo una rosada similar.")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.execute_tool.return_value = {"found": True, "products": [{"product_name": "Pijama satén rosada", "in_stock": True}]}

    response = await h.run("Tienes pijama negra en talla M?")

    assert h.selected_agent() == "sales"
    assert "rosada" in response["text"]
    h.execute_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_product_image_request(monkeypatch):
    provider = _provider_with_tool("send_product_image", {"product_query": "Pijama satén azul", "caption": "Aquí tienes la foto."}, "")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.execute_tool.return_value = {"type": "product_image", "image_url": "https://example.com/pijama.jpg", "caption": "Aquí tienes la foto."}

    response = await h.run("Me muestras la foto del pijama azul?", channel="instagram")
    assert h.selected_agent() == "sales"
    assert response["product_image"]["image_url"] == "https://example.com/pijama.jpg"
    assert response["whatsapp_handoff"] is None


@pytest.mark.asyncio
async def test_complete_purchase_flow_no_duplicate_side_effects(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(tool_calls=[_tool_call("update_checkout_draft", {"items": [{"product_query": "Pijama satén azul", "size": "S", "quantity": 1}], "shipping_method": "mrw", "shipping_city": "Caracas", "shipping_address": "Av Principal", "payment_method": "Zelle"}, "draft-1")])),
        continue_after_tool=AsyncMock(side_effect=[
            LLMResponse(tool_calls=[_tool_call("finalize_checkout", {}, "finalize-1")]),
            LLMResponse(tool_calls=[_tool_call("finalize_checkout", {}, "finalize-2")]),
            LLMResponse(text="Listo, tu pedido quedó creado. Te paso los datos de pago."),
        ]),
    )
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.execute_tool.side_effect = [
        {"status": "ok", "missing_fields": [], "errors": []},
        {"status": "created", "order_id": "order-1", "total": 28.0, "payment_method": "Zelle"},
    ]

    response = await h.run("Quiero comprar la pijama azul S, 1 unidad, MRW Caracas Av Principal, pago Zelle")

    assert h.selected_agent() == "checkout"
    assert response["text"].startswith("Listo")
    assert [call.args[0] for call in h.execute_tool.await_args_list] == ["update_checkout_draft", "finalize_checkout"]
    assert "duplicate_ignored" in provider.continue_after_tool.await_args_list[2].kwargs["tool_result"]


@pytest.mark.asyncio
async def test_purchase_flow_using_saved_address(monkeypatch):
    provider = _provider_with_tool("update_checkout_draft", {"use_saved_address": True, "payment_method": "Zelle"}, "Perfecto, uso tu dirección guardada.")
    h = TranscriptHarness(monkeypatch, provider=provider)

    await h.run("Quiero comprar la pijama azul, usa mi misma dirección y pago por Zelle")

    assert h.selected_agent() == "checkout"
    assert h.execute_tool.await_args.args[1]["use_saved_address"] is True


@pytest.mark.asyncio
async def test_customer_changes_product_during_checkout(monkeypatch):
    provider = _provider_with_tool("update_checkout_draft", {"items": [{"product_query": "Pijama satén rosada", "size": "M", "quantity": 1}]}, "Cambié el producto a la pijama rosada.")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.session = _session("checkout", "checkout_collecting")

    response = await h.run("Mejor quiero la rosada en M")

    assert h.selected_agent() == "checkout"
    assert "rosada" in str(h.execute_tool.await_args.args[1])
    assert "rosada" in response["text"]


@pytest.mark.asyncio
async def test_customer_cancels_checkout(monkeypatch):
    provider = _provider_with_tool("cancel_checkout", {"reason": "Cliente canceló"}, "Listo, cancelé el checkout.")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.session = _session("checkout", "checkout_collecting")

    response = await h.run("Cancela el pedido")

    assert h.selected_agent() == "checkout"
    assert h.tool_names() == ["cancel_checkout"]
    assert "cancelé" in response["text"]


@pytest.mark.asyncio
async def test_valid_payment_proof(monkeypatch):
    h = TranscriptHarness(monkeypatch)
    engine.orders.get_latest_open_order.return_value = _order()
    engine.orders.get_unambiguous_open_order.return_value = (_order(), False)
    engine.analyze_payment_screenshot.return_value = {"analyzed": True, "payment_method": "zelle", "amount": "28.00", "currency": "USD", "status": "completed", "confidence": "high", "reference": "TXN-TRANSCRIPT-123", "date": datetime.now(UTC).isoformat(), "proof_hash": "d" * 64, "recipient_identifier": "pagos@example.com", "summary": "Pago Zelle a pagos@example.com"}

    response = await h.run("Te mando el comprobante", media_url="media-1")

    assert h.selected_agent() == "payment"
    assert "recibí el comprobante" in response["text"]
    engine.orders.update_order_payment_status.assert_awaited_once()
    h.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_payment_proof(monkeypatch):
    h = TranscriptHarness(monkeypatch)
    engine.orders.get_latest_open_order.return_value = _order(total=28.0)
    engine.orders.get_unambiguous_open_order.return_value = (_order(total=28.0), False)
    engine.analyze_payment_screenshot.return_value = {"analyzed": True, "payment_method": "zelle", "amount": "20.00", "currency": "USD", "status": "completed", "confidence": "high", "reference": "TXN-TRANSCRIPT-124", "date": datetime.now(UTC).isoformat(), "proof_hash": "e" * 64, "recipient_identifier": "pagos@example.com", "summary": "Pago Zelle por $20"}

    response = await h.run("Te mando el comprobante", media_url="media-1")

    assert h.selected_agent() == "payment"
    assert "monto" in response["text"]
    engine.orders.update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_proof_before_order_creation(monkeypatch):
    h = TranscriptHarness(monkeypatch)
    engine.orders.get_latest_open_order.return_value = None
    engine.orders.get_unambiguous_open_order.return_value = (None, False)
    engine.analyze_payment_screenshot.return_value = {"analyzed": True, "payment_method": "zelle", "amount": "28.00", "status": "completed", "summary": "Pago Zelle"}

    response = await h.run("Te mando el comprobante", media_url="media-1")

    assert h.selected_agent() == "payment"
    assert "todavía no tengo el pedido registrado" in response["text"]
    engine.orders.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_order_status(monkeypatch):
    provider = _provider_with_tool("get_customer_order_status", {"limit": 3}, "Tu pedido está pendiente de pago.")
    h = TranscriptHarness(monkeypatch, provider=provider)

    response = await h.run("Cuál es el estado de mi pedido?")

    assert h.selected_agent() == "support"
    assert h.tool_names() == ["get_customer_order_status"]
    assert "pendiente" in response["text"]


@pytest.mark.asyncio
async def test_tracking_question(monkeypatch):
    provider = _provider_with_tool("get_customer_order_status", {"limit": 1}, "Aún no tiene número de guía cargado.")
    h = TranscriptHarness(monkeypatch, provider=provider)

    response = await h.run("Me pasas el tracking?")
    assert h.selected_agent() == "support"
    assert "guía" in response["text"]


@pytest.mark.asyncio
async def test_complaint_and_escalation(monkeypatch):
    provider = _provider_with_tool("escalate_to_human", {"reason": "Cliente pide devolución", "urgency": "high"}, "Te paso con una persona del equipo.")
    h = TranscriptHarness(monkeypatch, provider=provider)
    h.execute_tool.return_value = {"status": "escalated"}

    response = await h.run("Quiero una devolución")
    assert h.selected_agent() == "support"
    assert response["escalated"] is True
    assert engine.analytics.log_ai_run.await_args.kwargs["escalation_occurred"] is True


@pytest.mark.asyncio
async def test_explicit_human_request(monkeypatch):
    h = TranscriptHarness(monkeypatch)

    response = await h.run("Quiero hablar con una persona real")
    assert h.selected_agent() == "support"
    assert response["escalated"] is True
    h.provider.chat.assert_not_awaited()
    h.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_hostile_message(monkeypatch):
    h = TranscriptHarness(monkeypatch)

    response = await h.run("Son unos ladrones, los voy a denunciar")
    assert h.selected_agent() == "support"
    assert response["escalated"] is True
    h.provider.chat.assert_not_awaited()
    engine.escalations.escalate_customer_automatically.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_purchase_while_unpaid_order_exists(monkeypatch):
    provider = _provider_with_tool("finalize_checkout", {}, "Tienes un pedido pendiente. ¿Quieres continuar ese o iniciar otro?")
    h = TranscriptHarness(monkeypatch, provider=provider)
    engine.orders.get_latest_open_order.return_value = _order()
    h.execute_tool.return_value = {"status": "existing_unpaid_order", "order_id": "order-1"}

    response = await h.run("Quiero comprar otra pijama")
    assert h.selected_agent() == "checkout"
    assert h.execute_tool.await_args.args[0] == "finalize_checkout"
    assert "pendiente" in response["text"]
    engine.orders.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_fallback(monkeypatch):
    primary = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("boom")), continue_after_tool=AsyncMock())
    fallback = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(text="Respuesta fallback", usage={"input_tokens": 7, "output_tokens": 8})), continue_after_tool=AsyncMock())
    h = TranscriptHarness(monkeypatch, settings=_settings(auto_fallback=True), provider=primary)
    monkeypatch.setattr(agent_runner, "_list_providers", lambda: ["openai", "anthropic"])
    monkeypatch.setattr(agent_runner, "get_provider", lambda name: primary if name == "openai" else fallback)

    response = await h.run("Hola")
    assert response["text"] == "Respuesta fallback"
    assert engine.analytics.log_ai_run.await_args.kwargs["fallback_occurred"] is True
    assert engine.analytics.log_ai_run.await_args.kwargs["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_invalid_llm_router_response_keeps_legacy(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(text="Respuesta legacy")),
        continue_after_tool=AsyncMock(),
    )
    router_provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(text="not-json")))
    h = TranscriptHarness(monkeypatch, provider=provider)
    monkeypatch.setattr("app.ai.llm_router.list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.llm_router.get_provider", lambda name: router_provider)

    response = await h.run("Ajá y entonces?")
    assert response["text"] == "Respuesta legacy"
    assert h.selected_agent() == "legacy"
    assert h.route_source() == "default"
    h.execute_tool.assert_not_awaited()
