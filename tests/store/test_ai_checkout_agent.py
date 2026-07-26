from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.ai.agents.checkout import CHECKOUT_AGENT
from app.ai.checkout import service
from app.ai.providers.base import LLMResponse
from app.ai.runner import AgentRunContext, AgentRunner
from app.crm import sessions


def _customer(**overrides) -> dict:
    customer = {
        "id": "customer-1",
        "display_name": "Luisana Perez",
        "last_shipping_address": "Av Principal, Casa 8",
        "last_shipping_city": "Caracas",
        "last_shipping_method": "mrw",
    }
    customer.update(overrides)
    return customer


def _payment_methods() -> list[dict]:
    return [{"id": "pm-zelle", "name": "Zelle", "information": "Correo: pagos@example.com"}]


def _catalog(stock: int = 5, price: float = 28.0) -> list[dict]:
    return [
        {
            "sku": "PJ-001-M",
            "parent_sku": "PJ-001",
            "product_name": "Pijama satén azul",
            "category": "Pijamas",
            "description": "Pijama azul",
            "size": "M",
            "sizes": "M",
            "price_usd": price,
            "stock": stock,
        }
    ]


def _session(draft: dict | None = None, **overrides) -> sessions.ConversationSession:
    values = {
        "customer_id": "customer-1",
        "active_agent": "checkout",
        "workflow_stage": "checkout_collecting",
        "checkout_draft": draft or {},
    }
    values.update(overrides)
    return sessions.ConversationSession.model_validate(values)


def _complete_draft(**overrides) -> dict:
    draft = {
        "items": [{"product_query": "Pijama satén azul", "size": "M", "quantity": 1}],
        "shipping_method": "mrw",
        "shipping_city": "Caracas",
        "pickup_agency": "MRW Chacao",
        "payment_method": "Zelle",
    }
    draft.update(overrides)
    return draft


@pytest.fixture(autouse=True)
def _shipping_policy(monkeypatch):
    async def get_policy():
        return {
            "currency": "USD",
            "home_delivery_cities": [{"name": "Valencia", "aliases": []}],
            "home_delivery_zones": [
                {"city": "Valencia", "name": "El Viñedo", "aliases": [], "fee_usd": 4.0},
            ],
            "courier_destination_rates": [
                {"city": "Caracas", "aliases": [], "mrw_fee_usd": 6.0, "zoom_fee_usd": 7.0},
            ],
        }

    monkeypatch.setattr(service, "_get_shipping_policy", get_policy)


def _patch_catalog(monkeypatch, catalog: list[dict]) -> None:
    monkeypatch.setattr(service, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr("app.ai.tools.catalog.get_cached_catalog", lambda: catalog)


@pytest.mark.asyncio
async def test_beginning_checkout_from_selected_product(monkeypatch):
    _patch_catalog(monkeypatch, _catalog())
    update_session = AsyncMock(return_value=_session({
        "items": [{"product_query": "Pijama satén azul", "size": "M", "quantity": 1}],
    }))
    monkeypatch.setattr(service.sessions, "update_checkout_draft", update_session)

    result = await service.update_checkout_draft(
        _customer(),
        {"items": [{"product_query": "Pijama satén azul", "size": "M", "quantity": 1, "unit_price": 1}]},
        payment_methods=_payment_methods(),
    )

    persisted_update = update_session.await_args.args[1]
    assert persisted_update["items"][0]["canonical_sku"] == "PJ-001-M"
    assert "unit_price" not in result["draft"]["items"][0]
    assert "shipping_address" in result["missing_fields"]


@pytest.mark.asyncio
async def test_progressive_field_collection_does_not_ask_for_known_fields(monkeypatch):
    _patch_catalog(monkeypatch, _catalog())
    update_session = AsyncMock(return_value=_session({
        "items": [{"product_query": "Pijama satén azul", "size": "M", "quantity": 1}],
        "shipping_method": "zoom",
        "shipping_city": "Valencia",
    }))
    monkeypatch.setattr(service.sessions, "update_checkout_draft", update_session)

    result = await service.update_checkout_draft(_customer(), {"shipping_city": "Valencia"})

    assert "items" not in result["missing_fields"]
    assert "shipping_method" not in result["missing_fields"]
    assert "shipping_zone" in result["missing_fields"]
    assert "shipping_address" in result["missing_fields"]


@pytest.mark.asyncio
async def test_missing_quantity_and_missing_pickup_agency(monkeypatch):
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session({
        "items": [{"product_query": "Pijama satén azul", "size": "M"}],
        "shipping_method": "mrw",
        "shipping_city": "Caracas",
        "payment_method": "Zelle",
    })))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["status"] == "missing_fields"
    assert "items[0].quantity" in result["missing_fields"]
    assert "pickup_agency" in result["missing_fields"]


@pytest.mark.asyncio
async def test_saved_address_confirmation(monkeypatch):
    update_session = AsyncMock(return_value=_session({
        "shipping_address": "Av Principal, Casa 8",
        "shipping_city": "Valencia",
        "shipping_zone": "El Viñedo",
        "fulfillment_type": "home_delivery",
    }))
    monkeypatch.setattr(service.sessions, "update_checkout_draft", update_session)

    await service.update_checkout_draft(_customer(
        last_shipping_city="Valencia",
        last_shipping_method="",
        last_fulfillment_type="home_delivery",
        last_shipping_zone="El Viñedo",
    ), {"use_saved_address": True})

    update = update_session.await_args.args[1]
    assert update["shipping_address"] == "Av Principal, Casa 8"
    assert update["shipping_city"] == "Valencia"
    assert update["shipping_zone"] == "El Viñedo"


@pytest.mark.asyncio
async def test_invalid_size(monkeypatch):
    _patch_catalog(monkeypatch, _catalog())
    monkeypatch.setattr(service.sessions, "update_checkout_draft", AsyncMock(return_value=_session({
        "items": [{"product_query": "Pijama satén azul", "size": "XL", "quantity": 1}],
    })))

    result = await service.update_checkout_draft(_customer(), {"items": [{"product_query": "Pijama satén azul", "size": "XL", "quantity": 1}]})

    assert result["status"] == "needs_more_info"
    assert any("talla" in error.lower() or "catálogo" in error.lower() for error in result["errors"])


@pytest.mark.asyncio
async def test_product_becoming_unavailable(monkeypatch):
    _patch_catalog(monkeypatch, _catalog(stock=0))
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft())))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["status"] == "error"
    assert "agotado" in result["message"]


@pytest.mark.asyncio
async def test_requested_quantity_cannot_exceed_current_stock(monkeypatch):
    _patch_catalog(monkeypatch, _catalog(stock=1))
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft(
        items=[{"product_query": "Pijama satén azul", "size": "M", "quantity": 2}],
    ))))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["status"] == "error"
    assert "suficientes unidades" in result["message"]
    assert result["missing_fields"] == []


@pytest.mark.asyncio
async def test_catalog_price_and_discount_come_from_backend(monkeypatch):
    _patch_catalog(monkeypatch, _catalog(price=40.0))
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft())))
    monkeypatch.setattr(service.sessions, "set_current_order", AsyncMock(return_value=_session(current_order_id="order-1")))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value=None))
    create_order = AsyncMock(return_value={
        "order_id": "order-1",
        "items": [{"product_name": "Pijama satén azul", "sku": "PJ-001-M", "size": "M", "quantity": 10, "unit_price": 40.0}],
        "total": 360.0,
        "subtotal": 400.0,
        "discount_applied": True,
        "discount_amount": 40.0,
        "payment_method": "Zelle",
        "created_new": True,
    })
    monkeypatch.setattr(service.orders, "create_order", create_order)
    monkeypatch.setattr(service, "_notify_checkout_order", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_save_shipping_address", AsyncMock(return_value=None))
    monkeypatch.setattr(service.customers, "add_tags", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert create_order.await_args.kwargs["items"][0]["unit_price"] == 40.0
    assert result["discount_applied"] is True
    assert result["discount_amount"] == 40.0
    assert result["payment_instructions"] == "Correo: pagos@example.com"


def test_payment_method_is_final_missing_field():
    draft = sessions.CheckoutDraft.model_validate({
        "items": [{"product_query": "Pijama satén azul", "size": "M", "quantity": 1}],
        "shipping_method": "mrw",
        "shipping_city": "Caracas",
        "shipping_address": "Av Principal, Casa 8",
    })

    assert draft.missing_fields()[-1] == "payment_method"


@pytest.mark.asyncio
async def test_successful_finalization_sets_session(monkeypatch):
    _patch_catalog(monkeypatch, _catalog())
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft())))
    set_current = AsyncMock(return_value=_session(current_order_id="order-1"))
    monkeypatch.setattr(service.sessions, "set_current_order", set_current)
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value=None))
    monkeypatch.setattr(service.orders, "create_order", AsyncMock(return_value={
        "order_id": "order-1",
        "items": [],
        "total": 28.0,
        "subtotal": 28.0,
        "payment_method": "Zelle",
        "created_new": True,
    }))
    monkeypatch.setattr(service, "_notify_checkout_order", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_save_shipping_address", AsyncMock(return_value=None))
    monkeypatch.setattr(service.customers, "add_tags", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["status"] == "created"
    set_current.assert_awaited_once_with("customer-1", "order-1", workflow_stage="waiting_for_payment")


@pytest.mark.asyncio
async def test_duplicate_finalization_reuses_current_order(monkeypatch):
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(
        current_order_id="order-1",
        workflow_stage="waiting_for_payment",
    )))
    monkeypatch.setattr(service.orders, "get_order", AsyncMock(return_value={
        "id": "order-1",
        "items": [],
        "total": 28.0,
        "payment_method": "Zelle",
    }))
    create_order = AsyncMock(return_value={})
    monkeypatch.setattr(service.orders, "create_order", create_order)

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["status"] == "already_finalized"
    create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_checkout_does_not_reuse_completed_session_order(monkeypatch):
    _patch_catalog(monkeypatch, _catalog())
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(
        _complete_draft(),
        current_order_id="order-completed",
        workflow_stage="checkout_ready",
    )))
    get_old_order = AsyncMock()
    monkeypatch.setattr(service.orders, "get_order", get_old_order)
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value=None))
    create_order = AsyncMock(return_value={
        "order_id": "order-new",
        "items": [],
        "total": 28.0,
        "payment_method": "Zelle",
        "created_new": True,
    })
    monkeypatch.setattr(service.orders, "create_order", create_order)
    monkeypatch.setattr(service.sessions, "set_current_order", AsyncMock())
    monkeypatch.setattr(service, "_notify_checkout_order", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_save_shipping_address", AsyncMock(return_value=None))
    monkeypatch.setattr(service.customers, "add_tags", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["order_id"] == "order-new"
    create_order.assert_awaited_once()
    get_old_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_resets_session(monkeypatch):
    reset = AsyncMock(return_value=_session(active_agent="legacy", workflow_stage="idle"))
    monkeypatch.setattr(service.sessions, "reset_session", reset)

    result = await service.cancel_checkout("customer-1", reason="Cliente canceló")

    assert result["status"] == "cancelled"
    reset.assert_awaited_once_with("customer-1")


@pytest.mark.asyncio
async def test_existing_unpaid_order_requires_explicit_choice(monkeypatch):
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft())))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value={"id": "order-open"}))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods())

    assert result["status"] == "existing_unpaid_order"
    assert result["order_id"] == "order-open"


@pytest.mark.asyncio
async def test_starting_separate_new_purchase(monkeypatch):
    _patch_catalog(monkeypatch, _catalog())
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft())))
    monkeypatch.setattr(service.sessions, "set_current_order", AsyncMock(return_value=_session(current_order_id="order-new")))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value={"id": "order-open"}))
    monkeypatch.setattr(service.orders, "get_active_unpaid_order_count", AsyncMock(return_value=1))
    monkeypatch.setattr(service.orders, "create_order", AsyncMock(return_value={
        "order_id": "order-new",
        "items": [],
        "total": 28.0,
        "payment_method": "Zelle",
        "created_new": True,
    }))
    monkeypatch.setattr(service, "_notify_checkout_order", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_save_shipping_address", AsyncMock(return_value=None))
    monkeypatch.setattr(service.customers, "add_tags", AsyncMock(return_value=None))

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods(), start_new_order=True)

    assert result["order_id"] == "order-new"


@pytest.mark.asyncio
async def test_fourth_unpaid_order_is_blocked_with_payment_reminder(monkeypatch):
    monkeypatch.setattr(service.sessions, "get_or_create_session", AsyncMock(return_value=_session(_complete_draft())))
    monkeypatch.setattr(service.orders, "get_latest_pending_order", AsyncMock(return_value={"id": "order-open"}))
    monkeypatch.setattr(service.orders, "get_active_unpaid_order_count", AsyncMock(return_value=3))
    create_order = AsyncMock()
    monkeypatch.setattr(service.orders, "create_order", create_order)

    result = await service.finalize_checkout(_customer(), payment_methods=_payment_methods(), start_new_order=True)

    assert result["status"] == "pending_order_limit_reached"
    assert result["pending_order_count"] == 3
    assert "Paga" in result["message"]
    create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_agent_cannot_update_payment_status(monkeypatch):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=LLMResponse(tool_calls=[{"id": "tool-1", "name": "update_payment_status", "arguments": {"confirmation_note": "x"}}])),
        continue_after_tool=AsyncMock(return_value=LLMResponse(text="No puedo validar pagos aquí.")),
    )
    execute = AsyncMock(return_value={"payment_status": "proof_received"})
    monkeypatch.setattr("app.ai.runner._list_providers", lambda: ["openai"])
    monkeypatch.setattr("app.ai.runner.get_provider", lambda name: provider)
    monkeypatch.setattr("app.ai.runner.execute_tool", execute)

    result = await AgentRunner().run(
        CHECKOUT_AGENT,
        "prompt",
        [],
        {"llm_provider": "openai", "llm_model": "gpt-5.4-nano", "auto_fallback": False},
        AgentRunContext(customer={"id": "customer-1"}, channel="whatsapp"),
    )

    assert "not authorized" in result.tool_log[0]["result"]["message"]
    execute.assert_not_awaited()
