from unittest.mock import AsyncMock, patch

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
