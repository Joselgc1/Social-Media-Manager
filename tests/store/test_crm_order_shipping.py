from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_shipping_status_update_preserves_existing_tracking_number():
    from app.crm import orders

    execute = AsyncMock()
    with (
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "order-1"})),
        patch.object(orders.db, "execute", execute),
        patch.object(
            orders,
            "get_order",
            AsyncMock(
                return_value={
                    "id": "order-1",
                    "shipping_status": "shipped",
                    "tracking_number": "TRACK-123",
                }
            ),
        ),
    ):
        result = await orders.update_order_shipping("order-1", shipping_status="shipped")

    values = execute.await_args.args[1]
    assert values["update_tracking_number"] is False
    assert result["tracking_number"] == "TRACK-123"


@pytest.mark.asyncio
async def test_empty_tracking_number_explicitly_clears_it():
    from app.crm import orders

    execute = AsyncMock()
    with (
        patch.object(orders.db, "fetch_one", AsyncMock(return_value={"id": "order-1"})),
        patch.object(orders.db, "execute", execute),
        patch.object(
            orders,
            "get_order",
            AsyncMock(
                return_value={
                    "id": "order-1",
                    "shipping_status": "pending",
                    "tracking_number": None,
                }
            ),
        ),
    ):
        await orders.update_order_shipping("order-1", tracking_number="")

    values = execute.await_args.args[1]
    assert values["update_tracking_number"] is True
    assert values["tracking_number"] is None
