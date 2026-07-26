from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.ai.tools import catalog as tool_catalog
from app.ai.tools import checkout as tool_checkout
from app.ai.tools import customers as tool_customers
from app.ai.tools import messaging as tool_messaging
from app.ai.tools import orders as tool_orders
from app.ai.tools import payments as tool_payments
from app.ai.tools import support as tool_support
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool


def _context(**overrides) -> ToolExecutionContext:
    values = {
        "customer": {
            "id": "customer-1",
            "channel": "whatsapp",
            "platform_id": "584121234567",
            "display_name": "Luisana Perez",
        },
        "channel": "whatsapp",
        "payment_methods": [
            {
                "id": "pm-zelle",
                "name": "Zelle",
                "information": "Correo: pagos@example.com",
            }
        ],
        "vision_result": None,
        "payment_proof_attempt": False,
        "latest_user_message": "Hola",
    }
    values.update(overrides)
    return ToolExecutionContext(**values)


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
            "sku": "PJ-001-M",
            "parent_sku": "PJ-001",
            "product_name": "Pijama satén azul",
            "category": "Pijamas",
            "description": "Pijama suave azul",
            "size": "M",
            "sizes": "M",
            "price_usd": 28,
            "stock": 0,
            "image_url": "",
        },
    ]


def _order(**overrides) -> dict:
    order = {
        "id": "order-1",
        "order_id": "order-1",
        "total": 28.0,
        "payment_method": "Zelle",
        "payment_status": "pending",
        "created_new": True,
    }
    order.update(overrides)
    return order


@pytest.mark.asyncio
async def test_executor_dispatches_customer_tag_tool_with_context(monkeypatch):
    add_tags = AsyncMock(return_value=None)
    monkeypatch.setattr(tool_customers.customers, "add_tags", add_tags)

    result = await execute_tool("tag_customer", {"tags": ["interested:pajamas"]}, _context())

    assert result == {"status": "ok", "message": "Tags added silently."}
    add_tags.assert_awaited_once_with("customer-1", ["interested:pajamas"])


@pytest.mark.asyncio
async def test_executor_unknown_tool_returns_legacy_error_shape():
    result = await execute_tool("missing_tool", {}, _context())

    assert result == {"status": "error", "message": "Unknown tool: missing_tool"}


@pytest.mark.asyncio
async def test_executor_catalog_search_preserves_inventory_result_shape(monkeypatch):
    monkeypatch.setattr(tool_catalog, "get_cached_catalog", _catalog)
    monkeypatch.setattr(tool_catalog, "ensure_fresh_catalog", AsyncMock(return_value=_catalog()))

    result = await execute_tool("check_inventory", {"product_query": "pijama satén"}, _context())

    assert result["found"] is True
    assert result["count"] == 1
    assert result["products"][0]["product_name"] == "Pijama satén azul"
    assert result["products"][0]["in_stock"] is True
    assert "stock" not in result["products"][0]


@pytest.mark.asyncio
async def test_executor_create_order_uses_server_owned_customer_context(monkeypatch):
    create_order = AsyncMock(return_value=_order())
    notify_new_order = AsyncMock(return_value=None)
    add_tags = AsyncMock(return_value=None)
    db_execute = AsyncMock(return_value=None)
    monkeypatch.setattr(tool_orders.orders, "create_order", create_order)
    monkeypatch.setattr(tool_orders, "notify_new_order", notify_new_order)
    monkeypatch.setattr(tool_orders.customers, "add_tags", add_tags)
    monkeypatch.setattr(tool_orders.db, "execute", db_execute)
    monkeypatch.setattr(tool_orders.checkout_service, "validate_legacy_delivery", AsyncMock(return_value={
        "status": "ok",
        "quote": {
            "fulfillment_type": "courier_agency_pickup",
            "shipping_city": "Caracas",
            "shipping_method": "mrw",
            "shipping_fee": 6.0,
            "shipping_currency": "USD",
        },
    }))

    args = {
        "customer_id": "attacker-controlled",
        "items": [{"product_name": "Pijama satén azul", "sku": "PJ-001-S", "size": "S", "quantity": 1, "unit_price": 28}],
        "payment_method": "Zelle",
        "shipping_city": "Caracas",
        "shipping_method": "mrw",
        "pickup_agency": "MRW Chacao",
    }

    result = await execute_tool("create_order", args, _context())

    assert result["order_id"] == "order-1"
    create_order.assert_awaited_once()
    assert create_order.await_args.kwargs["customer_id"] == "customer-1"
    notify_new_order.assert_awaited_once()
    add_tags.assert_awaited_once_with("customer-1", ["payment:zelle"])
    db_execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_executor_create_order_returns_catalog_validation_error(monkeypatch):
    create_order = AsyncMock(side_effect=ValueError("Product or size was not found in the current catalog."))
    notify_new_order = AsyncMock(return_value=None)
    add_tags = AsyncMock(return_value=None)
    monkeypatch.setattr(tool_orders.orders, "create_order", create_order)
    monkeypatch.setattr(tool_orders, "notify_new_order", notify_new_order)
    monkeypatch.setattr(tool_orders.customers, "add_tags", add_tags)
    monkeypatch.setattr(tool_orders.checkout_service, "validate_legacy_delivery", AsyncMock(return_value={
        "status": "ok",
        "quote": {
            "fulfillment_type": "courier_agency_pickup",
            "shipping_city": "Caracas",
            "shipping_method": "mrw",
            "shipping_fee": 6.0,
            "shipping_currency": "USD",
        },
    }))

    result = await execute_tool(
        "create_order",
        {
            "items": [{"product_name": "Inventado", "sku": "fake", "size": "M", "quantity": 1, "unit_price": 1}],
            "payment_method": "Zelle",
            "shipping_city": "Caracas",
            "shipping_method": "mrw",
            "pickup_agency": "MRW Chacao",
        },
        _context(),
    )

    assert result == {"status": "error", "message": "Product or size was not found in the current catalog."}
    notify_new_order.assert_not_awaited()
    add_tags.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_args",
    [
        {
            "items": [],
            "payment_method": "Zelle",
            "shipping_city": "Caracas",
            "shipping_address": "Av Principal",
            "shipping_method": "mrw",
        },
        {
            "items": [{"product_name": "Pijama", "sku": "PJ-001-S", "size": "S", "quantity": 1, "unit_price": 28}],
            "payment_method": "Zelle",
            "shipping_address": "Av Principal",
            "shipping_method": "mrw",
        },
    ],
)
async def test_executor_rejects_invalid_create_order_arguments_before_dispatch(monkeypatch, invalid_args):
    create_order = AsyncMock()
    monkeypatch.setattr(tool_orders.orders, "create_order", create_order)

    result = await execute_tool("create_order", invalid_args, _context())

    assert result["status"] == "error"
    assert result["message"].startswith("Invalid arguments for create_order:")
    create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_rejects_unconfigured_create_order_payment_method(monkeypatch):
    create_order = AsyncMock(side_effect=ValueError("Payment method is not configured for this store."))
    monkeypatch.setattr(tool_orders.orders, "create_order", create_order)
    monkeypatch.setattr(tool_orders.checkout_service, "validate_legacy_delivery", AsyncMock(return_value={
        "status": "ok",
        "quote": {
            "fulfillment_type": "courier_agency_pickup",
            "shipping_city": "Caracas",
            "shipping_method": "mrw",
            "shipping_fee": 6.0,
            "shipping_currency": "USD",
        },
    }))

    result = await execute_tool(
        "create_order",
        {
            "items": [{"product_name": "Pijama", "sku": "PJ-001-S", "size": "S", "quantity": 1, "unit_price": 28}],
            "payment_method": "Inventado",
            "shipping_city": "Caracas",
            "shipping_method": "mrw",
            "pickup_agency": "MRW Chacao",
        },
        _context(),
    )

    assert result == {"status": "error", "message": "Payment method is not configured for this store."}
    create_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_executor_escalation_notifies_owner_and_sets_state(monkeypatch):
    get_recent_summary = AsyncMock(return_value="Resumen")
    notify_escalation = AsyncMock(return_value=None)
    escalate = AsyncMock(return_value={"id": "customer-1"})
    monkeypatch.setattr(tool_messaging.conversations, "get_recent_summary", get_recent_summary)
    monkeypatch.setattr(tool_messaging, "notify_escalation", notify_escalation)
    monkeypatch.setattr(tool_messaging.escalations, "escalate_customer_automatically", escalate)

    result = await execute_tool(
        "escalate_to_human",
        {"reason": "Quiere hablar con una persona", "urgency": "high"},
        _context(latest_user_message="Quiero hablar con una persona"),
    )

    assert result["status"] == "escalated"
    notify_escalation.assert_awaited_once()
    assert notify_escalation.await_args.kwargs["customer_platform_id"] == "584121234567"
    escalate.assert_awaited_once_with("customer-1")


@pytest.mark.asyncio
async def test_executor_payment_validation_success(monkeypatch):
    get_unambiguous_open_order = AsyncMock(return_value=(_order(), False))
    get_customer_open_order_by_id = AsyncMock(return_value=None)
    update_order_payment_status = AsyncMock(return_value={"order_id": "order-1", "payment_status": "proof_received"})
    set_current_order = AsyncMock(return_value=None)
    monkeypatch.setattr(
        tool_payments.payment_verifier.orders,
        "get_unambiguous_open_order",
        get_unambiguous_open_order,
    )
    monkeypatch.setattr(tool_payments.payment_verifier.orders, "get_customer_open_order_by_id", get_customer_open_order_by_id)
    monkeypatch.setattr(tool_payments.payment_verifier.sessions, "get_session", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_payments.payment_verifier.orders, "update_order_payment_status", update_order_payment_status)
    monkeypatch.setattr(tool_payments.payment_verifier.sessions, "set_current_order", set_current_order)

    context = _context(
        payment_proof_attempt=True,
        vision_result={
            "analyzed": True,
            "payment_method": "zelle",
            "amount": "28.00",
            "currency": "USD",
            "status": "completed",
            "confidence": "high",
            "reference": "TXN-TOOL-123",
            "date": datetime.now(UTC).isoformat(),
            "proof_hash": "f" * 64,
            "recipient_identifier": "pagos@example.com",
            "summary": "Pago Zelle a pagos@example.com por $28",
        },
    )

    result = await execute_tool("update_payment_status", {"confirmation_note": "Comprobante Zelle"}, context)

    assert result["payment_status"] == "proof_received"
    assert result["validated_amount"] == 28.0
    assert result["validated_payment_method"] == "Zelle"
    update_order_payment_status.assert_awaited_once()
    payment_update = update_order_payment_status.await_args
    assert payment_update.args == ("order-1",)
    assert payment_update.kwargs["status"] == "proof_received"
    assert payment_update.kwargs["note"] == "Comprobante Zelle"
    set_current_order.assert_awaited_once_with(
        "customer-1",
        None,
        workflow_stage="completed",
        active_agent="payment",
    )


@pytest.mark.asyncio
async def test_executor_payment_validation_failure_does_not_update_order(monkeypatch):
    get_unambiguous_open_order = AsyncMock(return_value=(_order(), False))
    get_customer_open_order_by_id = AsyncMock(return_value=None)
    update_order_payment_status = AsyncMock(return_value={"order_id": "order-1"})
    monkeypatch.setattr(
        tool_payments.payment_verifier.orders,
        "get_unambiguous_open_order",
        get_unambiguous_open_order,
    )
    monkeypatch.setattr(tool_payments.payment_verifier.orders, "get_customer_open_order_by_id", get_customer_open_order_by_id)
    monkeypatch.setattr(tool_payments.payment_verifier.sessions, "get_session", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_payments.payment_verifier.orders, "update_order_payment_status", update_order_payment_status)

    context = _context(
        payment_proof_attempt=True,
        vision_result={
            "analyzed": True,
            "payment_method": "zelle",
            "amount": "20.00",
            "currency": "USD",
            "status": "completed",
            "confidence": "high",
            "reference": "TXN-TOOL-124",
            "date": datetime.now(UTC).isoformat(),
            "proof_hash": "1" * 64,
            "recipient_identifier": "pagos@example.com",
            "summary": "Pago Zelle a pagos@example.com por $20",
        },
    )

    result = await execute_tool("update_payment_status", {"confirmation_note": "Comprobante Zelle"}, context)

    assert result["status"] == "error"
    assert "no coincide con el monto esperado" in result["message"]
    update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_rejects_payment_update_without_deterministic_proof():
    result = await execute_tool(
        "update_payment_status",
        {"confirmation_note": "Cliente dice que pagó"},
        _context(payment_proof_attempt=False, vision_result=None),
    )

    assert result == {
        "status": "error",
        "message": "Payment status can only be updated after deterministic payment-proof validation.",
    }


@pytest.mark.asyncio
async def test_executor_interactive_payload():
    result = await execute_tool(
        "send_interactive_buttons",
        {"body_text": "¿Prefieres MRW o Zoom?", "buttons": ["MRW", "Zoom"]},
        _context(),
    )

    assert result == {
        "type": "interactive_buttons",
        "body_text": "¿Prefieres MRW o Zoom?",
        "buttons": ["MRW", "Zoom"],
    }


@pytest.mark.asyncio
async def test_executor_handoff_payload():
    result = await execute_tool(
        "request_agent_handoff",
        {"target_agent": "checkout", "intent": "purchase_intent", "reason": "Quiere comprar"},
        _context(),
    )

    assert result == {
        "type": "agent_handoff",
        "target_agent": "checkout",
        "intent": "purchase_intent",
        "reason": "Quiere comprar",
    }


@pytest.mark.asyncio
async def test_executor_checkout_tools_dispatch_with_context(monkeypatch):
    update = AsyncMock(return_value={"status": "ok"})
    finalize = AsyncMock(return_value={"status": "created"})
    cancel = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(tool_checkout.service, "update_checkout_draft", update)
    monkeypatch.setattr(tool_checkout.service, "finalize_checkout", finalize)
    monkeypatch.setattr(tool_checkout.service, "cancel_checkout", cancel)

    context = _context()
    assert await execute_tool("update_checkout_draft", {"quantity": 1}, context) == {"status": "ok"}
    assert await execute_tool("finalize_checkout", {"start_new_order": True}, context) == {"status": "created"}
    assert await execute_tool("cancel_checkout", {"reason": "stop"}, context) == {"status": "cancelled"}

    update.assert_awaited_once_with(customer=context.customer, partial_update={"quantity": 1}, payment_methods=context.payment_methods)
    finalize.assert_awaited_once_with(customer=context.customer, payment_methods=context.payment_methods, start_new_order=True)
    cancel.assert_awaited_once_with("customer-1", reason="stop")


@pytest.mark.asyncio
async def test_executor_product_image_payload(monkeypatch):
    monkeypatch.setattr(tool_catalog, "get_cached_catalog", _catalog)
    monkeypatch.setattr(tool_catalog, "ensure_fresh_catalog", AsyncMock(return_value=_catalog()))

    result = await execute_tool(
        "send_product_image",
        {"product_query": "Pijama satén azul", "caption": "Foto del producto"},
        _context(),
    )

    assert result == {
        "type": "product_image",
        "image_url": "https://example.com/pijama.jpg",
        "caption": "Foto del producto",
        "product_name": "Pijama satén azul",
    }


@pytest.mark.asyncio
async def test_executor_support_order_status_is_scoped_to_current_customer(monkeypatch):
    get_customer_orders = AsyncMock(return_value=[
        {
            "id": "secret-order-id",
            "items": [{"product_name": "Pijama satén azul", "sku": "PJ-001-S", "size": "S", "quantity": 1}],
            "total": 28.0,
            "payment_method": "Zelle",
            "payment_status": "pending",
            "shipping_status": "pending",
            "tracking_number": None,
            "created_at": "2026-07-15",
        }
    ])
    monkeypatch.setattr(tool_support.orders, "get_customer_orders", get_customer_orders)

    result = await execute_tool(
        "get_customer_order_status",
        {"customer_id": "attacker-controlled", "limit": 5},
        _context(),
    )

    get_customer_orders.assert_awaited_once_with("customer-1", limit=5)
    assert result["status"] == "ok"
    assert result["orders"][0]["items_summary"] == "Pijama satén azul talla S x1"
    assert "secret-order-id" not in str(result)


@pytest.mark.asyncio
async def test_executor_catalog_pdf_generation_failure(monkeypatch):
    monkeypatch.setattr(tool_messaging, "get_cached_catalog", _catalog)
    monkeypatch.setattr(tool_messaging, "ensure_catalog_pdf", MagicMock(side_effect=RuntimeError("boom")))

    result = await execute_tool("send_catalog_pdf", {"caption": "Catálogo"}, _context())

    assert result == {"status": "error", "message": "Could not generate catalog PDF."}


def test_tool_execution_context_defaults_are_server_owned():
    context = ToolExecutionContext(customer={"id": "customer-1"}, channel="instagram")

    assert context.payment_methods == []
    assert context.vision_result is None
    assert context.payment_proof_attempt is False
    assert context.latest_user_message == ""
    assert context.session is None
