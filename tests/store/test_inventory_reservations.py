from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _sheet(records):
    worksheet = MagicMock()
    worksheet.get_all_records.return_value = records
    worksheet.row_values.return_value = ["SKU", "Product name", "Stock"]
    client = MagicMock()
    client.open_by_key.return_value = SimpleNamespace(sheet1=worksheet)
    return client, worksheet


def _database():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


def _catalog():
    return [{
        "sku": "SKU-S",
        "parent_sku": "SKU",
        "product_name": "Pijama",
        "size": "S",
        "sizes": "S",
        "price_usd": 28,
        "stock": 2,
    }]


def _items():
    return [{"sku": "SKU-S", "product_name": "Pijama", "size": "S", "quantity": 1}]


def test_sheet_inventory_uses_one_batch_after_validating_every_item():
    from app.catalog import sheets

    client, worksheet = _sheet([
        {"SKU": "SKU-S", "Stock": 3},
        {"SKU": "SKU-M", "Stock": 2},
    ])
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        patch.object(sheets, "refresh_catalog"),
    ):
        result = sheets.deduct_stock([
            {"sku": "SKU-S", "quantity": 2},
            {"sku": "SKU-M", "quantity": 1},
        ])

    assert result == {"SKU-S": 1, "SKU-M": 1}
    worksheet.batch_update.assert_called_once_with(
        [
            {"range": "C2", "values": [[1]]},
            {"range": "C3", "values": [[1]]},
        ],
        raw=True,
    )
    worksheet.update_cell.assert_not_called()


@pytest.mark.parametrize(
    "items",
    [
        [{"sku": "MISSING", "quantity": 1}],
        [{"sku": "SKU-S", "quantity": 3}],
    ],
)
def test_sheet_inventory_failure_never_partially_updates(items):
    from app.catalog import sheets

    client, worksheet = _sheet([{"SKU": "SKU-S", "Stock": 2}])
    with (
        patch.object(sheets, "_get_gspread_client", return_value=client),
        patch.object(sheets, "get_config", return_value=SimpleNamespace(product_sheet_id="sheet")),
        pytest.raises(sheets.InventoryUpdateError),
    ):
        sheets.deduct_stock(items)

    worksheet.batch_update.assert_not_called()


@pytest.mark.asyncio
async def test_order_reserves_inventory_before_database_insert():
    from app.crm import orders

    events = []
    fetch_one = AsyncMock(side_effect=[None, None, None])
    execute = AsyncMock(side_effect=lambda *args, **kwargs: events.append("insert") or "order-1")
    deduct = MagicMock(side_effect=lambda items: events.append("deduct"))

    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders.db, "get_settings", AsyncMock(return_value={})),
        patch.object(orders.db, "get_db", return_value=_database()),
        patch.object(orders.db, "fetch_one", fetch_one),
        patch.object(orders.db, "execute", execute),
        patch.object(orders, "deduct_stock", deduct),
    ):
        result = await orders.create_order("customer-1", _items(), "Zelle")

    assert events == ["deduct", "insert"]
    assert result["created_new"] is True
    assert "inventory_status, inventory_reserved_at" in execute.await_args.args[0]


@pytest.mark.asyncio
async def test_failed_order_insert_compensates_inventory_reservation():
    from app.crm import orders

    deduct = MagicMock()
    restore = MagicMock()
    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders.db, "get_settings", AsyncMock(return_value={})),
        patch.object(orders.db, "get_db", return_value=_database()),
        patch.object(orders.db, "fetch_one", AsyncMock(side_effect=[None, None, None])),
        patch.object(orders.db, "execute", AsyncMock(side_effect=RuntimeError("insert failed"))),
        patch.object(orders, "deduct_stock", deduct),
        patch.object(orders, "restore_stock", restore),
        pytest.raises(RuntimeError, match="insert failed"),
    ):
        await orders.create_order("customer-1", _items(), "Zelle")

    deduct.assert_called_once()
    restore.assert_called_once()


@pytest.mark.asyncio
async def test_failed_compensation_is_propagated_for_manual_reconciliation():
    from app.crm import orders

    with (
        patch.object(orders, "get_cached_catalog", return_value=_catalog()),
        patch.object(orders.db, "get_settings", AsyncMock(return_value={})),
        patch.object(orders.db, "get_db", return_value=_database()),
        patch.object(orders.db, "fetch_one", AsyncMock(side_effect=[None, None, None])),
        patch.object(orders.db, "execute", AsyncMock(side_effect=RuntimeError("insert failed"))),
        patch.object(orders, "deduct_stock", MagicMock()),
        patch.object(orders, "restore_stock", MagicMock(side_effect=RuntimeError("restore failed"))),
        pytest.raises(RuntimeError, match="manual reconciliation"),
    ):
        await orders.create_order("customer-1", _items(), "Zelle")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inventory_status", "expected_restored"),
    [("reserved", 1), ("legacy_unknown", 0), ("released", 0)],
)
async def test_delete_restores_only_confirmed_reservations(inventory_status, expected_restored):
    from app.crm import orders

    row = {
        "id": "order-1",
        "customer_id": None,
        "items": _items(),
        "total": 28,
        "customer_totals_applied": False,
        "inventory_status": inventory_status,
    }
    restore = MagicMock()
    with (
        patch.object(orders.db, "get_db", return_value=_database()),
        patch.object(orders.db, "fetch_one", AsyncMock(side_effect=[None, row])),
        patch.object(orders.db, "execute", AsyncMock()),
        patch.object(orders, "restore_stock", restore),
    ):
        result = await orders.delete_order("order-1")

    assert result["restored_items"] == expected_restored
    assert restore.call_count == expected_restored


@pytest.mark.asyncio
async def test_expired_pending_reservation_is_restored_and_rejected():
    from app.crm import orders

    row = {
        "id": "order-1",
        "items": _items(),
        "inventory_status": "reserved",
        "payment_status": "pending",
    }
    execute = AsyncMock()
    restore = MagicMock()
    with (
        patch.object(orders.db, "get_db", return_value=_database()),
        patch.object(orders.db, "fetch_one", AsyncMock(side_effect=[None, row])),
        patch.object(orders.db, "execute", execute),
        patch.object(orders, "restore_stock", restore),
    ):
        released = await orders._release_inventory_reservation("order-1")

    assert released is True
    restore.assert_called_once()
    assert "inventory_status = 'released'" in execute.await_args.args[0]
    assert "THEN 'rejected'" in execute.await_args.args[0]
