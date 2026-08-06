from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest


@pytest.mark.asyncio
async def test_order_list_keeps_orders_whose_customer_was_deleted():
    from app.admin import settings

    orphaned_order = {
        "id": "order-1",
        "display_name": "Cliente eliminado",
        "platform_id": None,
        "channel": None,
    }
    fetch_all = AsyncMock(return_value=[orphaned_order])
    with patch.object(settings.db, "fetch_all", fetch_all):
        result = await settings.list_orders()

    query = fetch_all.await_args.args[0]
    assert "LEFT JOIN customers" in query
    assert result == [orphaned_order]


@pytest.mark.asyncio
async def test_bulk_payment_status_updates_only_after_all_orders_exist():
    from app.admin import settings

    first_id = UUID("11111111-1111-1111-1111-111111111111")
    second_id = UUID("22222222-2222-2222-2222-222222222222")
    fetch_all = AsyncMock(return_value=[{"id": str(first_id)}, {"id": str(second_id)}])
    update_status = AsyncMock()
    with (
        patch.object(settings.db, "fetch_all", fetch_all),
        patch.object(settings.orders, "update_order_payment_status", update_status),
    ):
        result = await settings.bulk_update_order_payment_status(
            settings.BulkOrderPaymentStatusUpdate(
                order_ids=[first_id, second_id],
                payment_status="confirmed",
            )
        )

    assert result["payment_status"] == "confirmed"
    assert result["order_ids"] == [str(first_id), str(second_id)]
    assert update_status.await_count == 2
    assert "ANY(CAST(:order_ids AS uuid[]))" in fetch_all.await_args.args[0]


@pytest.mark.asyncio
async def test_bulk_customer_state_uses_existing_safe_escalation_flow():
    from app.admin import settings

    customer_id = UUID("33333333-3333-3333-3333-333333333333")
    fetch_all = AsyncMock(return_value=[{
        "id": str(customer_id),
        "channel": "whatsapp",
        "conversation_state": "active",
    }])
    escalate = AsyncMock()
    with (
        patch.object(settings.db, "fetch_all", fetch_all),
        patch.object(settings.escalations, "escalate_customer_manually", escalate),
    ):
        result = await settings.bulk_update_customer_state(
            settings.BulkCustomerStateUpdate(
                customer_ids=[customer_id],
                conversation_state="escalated",
            )
        )

    assert result["conversation_state"] == "escalated"
    escalate.assert_awaited_once_with(str(customer_id), channel="whatsapp")
